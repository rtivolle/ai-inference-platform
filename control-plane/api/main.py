"""
Control Plane REST API built with FastAPI.
Adheres to R1, R2, R3 specifications:
- Authentication required on all /api/v1/* endpoints (Bearer token / Cloudflare Access headers) (D7).
- Persistent state backed by Kubernetes CRDs, reconciled on startup (D8).
- Capacity constraint errors mapped to HTTP 409 (D4).
- Model not found mapped to HTTP 404 (D9).
"""

from contextlib import asynccontextmanager
from pathlib import Path

from autotuner.hf_inspector import ModelNotFoundError
from autotuner.sla_planner import ClusterInventory, DeployRequest, SLAPlanner
from autotuner.vram_calculator import InsufficientCapacityError
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from api.auth import router as auth_router
from api.auth import verify_auth
from api.dynamo_reconciler import DynamoReconciler
from api.k8s_client import K8sClient
from api.litellm_sync import LiteLLMSync
from api.metrics import UsageMetrics

# Shared clients
k8s_client = K8sClient()
litellm_sync = LiteLLMSync()
reconciler = DynamoReconciler(k8s_client=k8s_client, litellm_sync=litellm_sync)
sla_planner = SLAPlanner()
usage_metrics = UsageMetrics()


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
    background_tasks.add_task(reconciler.delete_model, model_name)
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
