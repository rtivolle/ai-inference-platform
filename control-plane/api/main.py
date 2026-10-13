"""
Control Plane REST API built with FastAPI.
Adheres to R1, R2, R3 specifications:
- Authentication required on all /api/v1/* endpoints (Bearer token / Cloudflare Access headers) (D7).
- Persistent state backed by Kubernetes CRDs, reconciled on startup (D8).
- Capacity constraint errors mapped to HTTP 409 (D4).
- Model not found mapped to HTTP 404 (D9).
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from autotuner.hf_inspector import ModelNotFoundError
from autotuner.sla_planner import ClusterInventory, DeployRequest, SLAPlanner
from autotuner.vram_calculator import InsufficientCapacityError
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from kubernetes_asyncio.client.rest import ApiException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api.auth import router as auth_router
from api.auth import verify_auth
from api.data_routes import router as data_router
from api.data_store import DataError
from api.dynamo_reconciler import DynamoReconciler
from api.k8s_client import K8sClient
from api.k8s_controls import ControlError, KubernetesControls
from api.litellm_sync import LiteLLMSync
from api.metrics import UsageMetrics

# Shared clients
k8s_client = K8sClient()
litellm_sync = LiteLLMSync()
reconciler = DynamoReconciler(k8s_client=k8s_client, litellm_sync=litellm_sync)
sla_planner = SLAPlanner()
usage_metrics = UsageMetrics()
logger = logging.getLogger("control_plane")


class ScaleRequest(BaseModel):
    replicas: int = Field(ge=1, le=64)
    resource_version: str | None = Field(default=None, max_length=128)


class EvictionRequest(BaseModel):
    uid: str = Field(min_length=1, max_length=128)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await k8s_client.initialize()
    # Reconcile existing deployments from cluster at startup (D8 fix)
    await reconciler.reconcile_existing()
    yield


app = FastAPI(
    title="AI Inference Control Plane API",
    version="1.0.0",
    description="Automated VRAM sizing, NVIDIA Dynamo disaggregated workload management, and LiteLLM gateway synchronization.",
    lifespan=lifespan,
)
app.include_router(auth_router)
app.include_router(data_router)


@app.exception_handler(DataError)
async def data_error(request, error: DataError):
    return JSONResponse(status_code=error.status, content={"detail": str(error)}, headers={"Cache-Control": "no-store"})


@app.exception_handler(ControlError)
async def control_error(request, error: ControlError):
    return JSONResponse(status_code=error.status, content={"detail": str(error)})


@app.exception_handler(ApiException)
async def kubernetes_error(request, error: ApiException):
    messages = {403: "Kubernetes permissions denied this operation.", 404: "The Kubernetes resource was not found.", 409: "The Kubernetes resource changed. Refresh and try again.", 429: "Kubernetes blocked this operation; a disruption budget or rate limit may be active."}
    code = 409 if error.status == 429 else error.status if error.status in (403, 404, 409) else 503
    return JSONResponse(status_code=code, content={"detail": messages.get(error.status, "Kubernetes is unavailable. Try again after the cluster connection recovers.")})

WEB_DIR = Path(__file__).resolve().parent / "web"
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="web-assets")


@app.get("/", include_in_schema=False)
async def web_console():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/health", tags=["Monitoring"])
async def health():
    return {"status": "ok", "service": "control-plane-api", "mode": "mock" if k8s_client.mock_mode else "live"}


@app.get("/health/readiness", tags=["Monitoring"])
async def health_readiness():
    return {"status": "ready", "k8s_connected": not k8s_client.mock_mode, "litellm_endpoint": litellm_sync.base_url}


@app.get("/api/v1/cluster/gpus", tags=["Inventory"], dependencies=[Depends(verify_auth)])
async def get_cluster_gpus():
    inventory = await k8s_client.get_cluster_gpu_inventory()
    return inventory


@app.get("/api/v1/metrics/usage", tags=["Monitoring"], dependencies=[Depends(verify_auth)])
async def get_usage_metrics(window_minutes: int = Query(15, ge=5, le=60)):
    if window_minutes not in (5, 15, 60):
        raise HTTPException(422, "Metrics window must be 5, 15, or 60 minutes.")
    return await usage_metrics.snapshot(window_minutes)


@app.get("/api/v1/cluster/nodes", tags=["Kubernetes"], dependencies=[Depends(verify_auth)])
async def get_cluster_nodes():
    return await KubernetesControls(k8s_client).nodes()


@app.get("/api/v1/models/{model_name}/workload", tags=["Kubernetes"], dependencies=[Depends(verify_auth)])
async def get_model_workload(model_name: str):
    controls = KubernetesControls(k8s_client)
    return {"namespace": "dynamo-workloads", "pods": await controls.pods(model_name), "components": await controls.components(model_name)}


@app.post("/api/v1/models/{model_name}/components/{component_name}/scale", tags=["Kubernetes"], dependencies=[Depends(verify_auth)])
async def scale_model_component(model_name: str, component_name: str, payload: ScaleRequest):
    return await KubernetesControls(k8s_client).scale(model_name, component_name, payload.replicas, payload.resource_version)


@app.get("/api/v1/models/{model_name}/pods/{pod_name}/logs", tags=["Kubernetes"], dependencies=[Depends(verify_auth)])
async def get_model_pod_logs(model_name: str, pod_name: str, container: str | None = Query(None, max_length=128), tail_lines: int = Query(100, ge=1, le=500)):
    return await KubernetesControls(k8s_client).pod_logs(model_name, pod_name, container, tail_lines)


@app.post("/api/v1/models/{model_name}/pods/{pod_name}/evict", tags=["Kubernetes"], dependencies=[Depends(verify_auth)])
async def replace_model_pod(model_name: str, pod_name: str, payload: EvictionRequest):
    return await KubernetesControls(k8s_client).evict_pod(model_name, pod_name, payload.uid)


async def delete_in_background(model_name: str):
    try:
        await reconciler.delete_model(model_name)
    except Exception:
        logger.exception("Deletion failed for deployment %s", model_name)


@app.post(
    "/api/v1/models/deploy", tags=["Deployments"], status_code=status.HTTP_200_OK, dependencies=[Depends(verify_auth)]
)
async def deploy_model(request: DeployRequest):
    try:
        # 1. Fetch live cluster inventory
        inventory_dict = await k8s_client.get_cluster_gpu_inventory()
        inventory = ClusterInventory(**inventory_dict)

        # 2. Compute Auto-Tuned deployment plan (Contract 1)
        plan = await run_in_threadpool(sla_planner.plan, request, inventory)

        # 3. Reconcile deployment: warmer Job -> DGD -> EndpointSlice watcher -> LiteLLM sync
        reconcile_result = await reconciler.deploy_model(plan)

        return {
            "status": "provisioning",
            "deployment_name": plan["deployment_name"],
            "model_id": plan["model_id"],
            "plan": reconcile_result["plan"],
        }
    except InsufficientCapacityError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ModelNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to schedule deployment: {e!s}")


@app.delete("/api/v1/models/{model_name}", tags=["Deployments"], dependencies=[Depends(verify_auth)])
async def delete_model(model_name: str, background_tasks: BackgroundTasks):
    existing = await reconciler.get_deployment_status(model_name)
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Deployment '{model_name}' not found.")

    # Initiate graceful dynamic drain and teardown
    background_tasks.add_task(delete_in_background, model_name)
    return {
        "status": "deleting",
        "model_name": model_name,
        "message": f"Model {model_name} deregistered from gateway; draining active requests before K8s cleanup.",
    }


@app.get("/api/v1/models", tags=["Deployments"], dependencies=[Depends(verify_auth)])
async def list_models():
    return await reconciler.list_deployments()


@app.get("/api/v1/models/{model_name}/status", tags=["Deployments"], dependencies=[Depends(verify_auth)])
async def get_model_status(model_name: str):
    info = await reconciler.get_deployment_status(model_name)
    if not info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Deployment '{model_name}' not found.")
    return info
