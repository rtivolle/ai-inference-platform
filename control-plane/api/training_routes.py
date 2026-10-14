"""Administrative training and explicit promotion into the inference reconciler."""

from functools import lru_cache
from typing import Annotated

from autotuner.sla_planner import ClusterInventory, DeployRequest
from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from .auth import verify_auth
from .data_models import PromotionRequest, TrainingRequest
from .data_routes import data_store
from .data_store import DataError
from .training import TrainingService

router = APIRouter(prefix="/api/v1/training", tags=["Training"])


@lru_cache(maxsize=1)
def training_service():
    from .main import k8s_client

    return TrainingService(data_store(), k8s_client)


@router.post("/runs", status_code=202)
async def launch_run(payload: TrainingRequest, identity: Annotated[dict, Depends(verify_auth)], service: Annotated[TrainingService, Depends(training_service)]):
    inventory = await service.k8s.get_cluster_gpu_inventory()
    return await service.launch(payload, inventory, identity["username"])


@router.get("/runs", dependencies=[Depends(verify_auth)])
async def list_runs(service: Annotated[TrainingService, Depends(training_service)], limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    return await run_in_threadpool(service.store.list, "run", limit, offset)


@router.get("/runs/{run_id}", dependencies=[Depends(verify_auth)])
async def get_run(run_id: str, service: Annotated[TrainingService, Depends(training_service)]):
    async with service.lock(run_id):
        return await service.status(run_id)


@router.post("/runs/{run_id}/cancel", dependencies=[Depends(verify_auth)])
async def cancel_run(run_id: str, service: Annotated[TrainingService, Depends(training_service)]):
    async with service.lock(run_id):
        return await service.cancel(run_id)


@router.get("/runs/{run_id}/logs", dependencies=[Depends(verify_auth)])
async def get_run_logs(run_id: str, service: Annotated[TrainingService, Depends(training_service)], tail_lines: int = Query(100, ge=1, le=500)):
    return await service.logs(run_id, tail_lines)


@router.post("/runs/{run_id}/promote", status_code=202)
async def promote_run(run_id: str, payload: PromotionRequest, identity: Annotated[dict, Depends(verify_auth)], service: Annotated[TrainingService, Depends(training_service)]):
    from .main import reconciler, sla_planner

    async with service.lock(run_id):
        run = await service.status(run_id)
        if run["status"] == "promoted":
            if run["promotion"]["deployment_name"] != payload.deployment_name or run["promotion"]["max_model_len"] != payload.max_model_len:
                raise DataError(409, "Run was already promoted to another deployment")
            return run
        service.validate_promotion(run)
        result = run["result"]
        existing = await reconciler.get_deployment_status(payload.deployment_name)
        if existing:
            promotion = run.get("promotion", {})
            existing_revision = (existing.get("plan") or {}).get("model_revision")
            if not existing_revision:
                resource = await service.k8s.get_dynamo_deployment(payload.deployment_name)
                existing_revision = (resource or {}).get("metadata", {}).get("annotations", {}).get("inference.platform/model-revision")
            if promotion.get("deployment_name") == payload.deployment_name and promotion.get("max_model_len") == payload.max_model_len and existing.get("model_id") == result["model_id"] and existing_revision == result["model_revision"]:
                run["status"] = "promoted"
                await run_in_threadpool(service.store.save, "run", run_id, run)
                return run
            raise DataError(409, "Use a new deployment name; existing deployments are retained for rollback")
        request = DeployRequest(model_id=result["model_id"], model_revision=result["model_revision"], deployment_name=payload.deployment_name,
                                max_model_len=payload.max_model_len, target_concurrency=1)
        inventory = ClusterInventory(**await service.k8s.get_cluster_gpu_inventory())
        plan = await run_in_threadpool(sla_planner.plan, request, inventory)
        # Save the intent before provisioning so an interrupted promotion is inspectable.
        run["promotion"] = {"deployment_name": payload.deployment_name, "max_model_len": payload.max_model_len, "model_id": result["model_id"], "model_revision": result["model_revision"], "reviewer": identity["username"], "status": "provisioning"}
        await run_in_threadpool(service.store.save, "run", run_id, run)
        await reconciler.deploy_model(plan)
        run["status"] = "promoted"
        await run_in_threadpool(service.store.save, "run", run_id, run)
        return run
