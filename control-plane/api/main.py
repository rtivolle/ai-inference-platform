"""
Control Plane REST API built with FastAPI.
Orchestrates model auto-tuning, Dynamo deployment reconciliation, and LiteLLM synchronization.
"""

import os
import sys
import asyncio
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, status
from fastapi.responses import JSONResponse

# Ensure repository root and control-plane directory are in python path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
CONTROL_PLANE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for p in (ROOT_DIR, CONTROL_PLANE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from autotuner.sla_planner import SLAPlanner, DeployRequest, ClusterInventory
    from api.k8s_client import K8sClient
    from api.litellm_sync import LiteLLMSync
    from api.dynamo_reconciler import DynamoReconciler
except ImportError:
    from control_plane.autotuner.sla_planner import SLAPlanner, DeployRequest, ClusterInventory
    from control_plane.api.k8s_client import K8sClient
    from control_plane.api.litellm_sync import LiteLLMSync
    from control_plane.api.dynamo_reconciler import DynamoReconciler

from contextlib import asynccontextmanager

# Shared instances
k8s_client = K8sClient()
litellm_sync = LiteLLMSync()
reconciler = DynamoReconciler(k8s_client=k8s_client, litellm_sync=litellm_sync)
sla_planner = SLAPlanner()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await k8s_client.initialize()
    yield

app = FastAPI(
    title="AI Inference Control Plane API",
    version="1.0.0",
    description="Automated VRAM sizing, NVIDIA Dynamo disaggregated workload management, and LiteLLM gateway synchronization.",
    lifespan=lifespan,
)

@app.get("/health", tags=["Monitoring"])
async def health():
    return {"status": "ok", "service": "control-plane-api"}

@app.get("/health/readiness", tags=["Monitoring"])
async def health_readiness():
    return {
        "status": "ready",
        "k8s_connected": not k8s_client.mock_mode,
        "litellm_endpoint": litellm_sync.base_url
    }

@app.get("/api/v1/cluster/gpus", tags=["Inventory"])
async def get_cluster_gpus():
    inventory = await k8s_client.get_cluster_gpu_inventory()
    return inventory

@app.post("/api/v1/models/deploy", tags=["Deployments"], status_code=status.HTTP_200_OK)
async def deploy_model(request: DeployRequest):
    try:
        # 1. Fetch cluster inventory
        inventory_dict = await k8s_client.get_cluster_gpu_inventory()
        inventory = ClusterInventory(**inventory_dict)

        # 2. Compute Auto-Tuned deployment plan (Contract 1)
        plan = sla_planner.plan(request, inventory)

        # 3. Reconcile deployment (Contract 2 & 3)
        reconcile_result = await reconciler.deploy_model(plan)

        return {
            "status": "provisioning",
            "deployment_name": plan["deployment_name"],
            "model_id": plan["model_id"],
            "plan": plan,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to schedule deployment: {str(e)}")

@app.delete("/api/v1/models/{model_name}", tags=["Deployments"])
async def delete_model(model_name: str, background_tasks: BackgroundTasks):
    existing = reconciler.get_deployment_status(model_name)
    if not existing and model_name not in reconciler.active_deployments:
        # Allow idempotent deletion even if not tracked in-memory
        pass

    # Initiate graceful drain and teardown
    background_tasks.add_task(reconciler.delete_model, model_name)
    return {
        "status": "deleting",
        "model_name": model_name,
        "message": f"Model {model_name} deregistered from gateway; draining active requests for 15s before CR cleanup."
    }

@app.get("/api/v1/models", tags=["Deployments"])
async def list_models():
    return reconciler.list_deployments()

@app.get("/api/v1/models/{model_name}/status", tags=["Deployments"])
async def get_model_status(model_name: str):
    info = reconciler.get_deployment_status(model_name)
    if not info:
        raise HTTPException(status_code=404, detail=f"Deployment '{model_name}' not found.")
    return info
