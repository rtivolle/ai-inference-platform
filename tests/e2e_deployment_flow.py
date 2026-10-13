"""
End-to-End Integration and Lifecycle Tests:
Validates the complete flow adhering to R1, R2, R3, R4:
- Authentication enforcement (HTTP 401 on missing auth) (D7)
- Overcapacity rejection (HTTP 409 on insufficient GPU VRAM) (D4)
- Unknown model rejection (HTTP 404) (D9)
- Warmer Job execution, DGD creation, and LiteLLM registration (D6, D3, Contrat 3)
- No silent mock fallback error verification (D1)
- Graceful drain and LiteLLM deregistration (D1, D8)
"""

import asyncio
import os

import pytest
from starlette.testclient import TestClient

# Ensure mock mode and testing token are set for this test module
os.environ["CONTROL_PLANE_MOCK"] = "1"
os.environ["CONTROL_PLANE_API_TOKEN"] = "test-token"

from api.k8s_client import K8sClient
from api.main import app, k8s_client, litellm_sync, reconciler

AUTH_HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client():
    k8s_client.mock_mode = True
    litellm_sync.mock_mode = True
    reconciler.drain_timeout_seconds = 0  # Instant drain in test suite
    reconciler.readiness_timeout_seconds = 2  # Short readiness wait
    with TestClient(app) as c:
        yield c


def test_unauthenticated_request_rejected(client):
    """Verifies D7: All /api/v1/* routes return 401 Unauthorized without auth."""
    resp = client.get("/api/v1/cluster/gpus")
    assert resp.status_code == 401
    assert "Unauthorized" in resp.json()["detail"]


def test_unknown_model_raises_404(client):
    """Verifies D9: Unknown models return 404 without silent fallback."""
    deploy_payload = {
        "model_id": "non-existent-org/unknown-model-12345",
        "profile": "low-latency",
    }
    resp = client.post("/api/v1/models/deploy", json=deploy_payload, headers=AUTH_HEADERS)
    assert resp.status_code == 404
    assert "could not be resolved" in resp.json()["detail"]


def test_overcapacity_deployment_raises_409(client):
    """Verifies D4: Sizing exceeding cluster GPU capacity returns 409 Conflict."""
    deploy_payload = {
        "model_id": "meta-llama/Llama-3.1-405B-Instruct",
        "dtype": "bfloat16",
        "quantization": None,  # 405B in bf16 cannot fit in 8x80GB GPUs
    }
    resp = client.post("/api/v1/models/deploy", json=deploy_payload, headers=AUTH_HEADERS)
    assert resp.status_code == 409
    assert "Cannot schedule model" in resp.json()["detail"]


def test_no_silent_mock_fallback_on_missing_kubeconfig():
    """Verifies D1: Non-mock mode raises RuntimeError instead of falling back to mock."""
    with pytest.raises(RuntimeError, match="Kubernetes cluster connection failed"):
        client_live = K8sClient(mock_mode=False)
        # Attempting live initialization without cluster/kubeconfig must fail loudly
        asyncio.run(client_live.initialize())


@pytest.mark.asyncio
async def test_full_lifecycle_e2e(client):
    """Verifies complete end-to-end deployment lifecycle."""
    # 1. Health check (public endpoint)
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"

    # 2. Deploy Model
    deploy_payload = {
        "model_id": "meta-llama/Llama-3.3-70B-Instruct",
        "deployment_name": "llama-3-3-70b-e2e",
        "profile": "low-latency",
        "quantization": "fp8",
        "dtype": "bfloat16",
        "max_model_len": 32768,
        "gpu_memory_utilization": 0.90,
    }

    deploy_resp = client.post("/api/v1/models/deploy", json=deploy_payload, headers=AUTH_HEADERS)
    assert deploy_resp.status_code == 200
    deploy_data = deploy_resp.json()

    assert deploy_data["status"] == "provisioning"
    assert deploy_data["deployment_name"] == "llama-3-3-70b-e2e"
    plan = deploy_data["plan"]

    # Verify Contract 1 fields
    assert plan["model_id"] == "meta-llama/Llama-3.3-70B-Instruct"
    assert plan["serving_mode"] == "disaggregated"
    assert plan["dtype"] == "bfloat16"
    assert plan["quantization"] == "fp8"
    assert plan["prefill_clique"]["tensor_parallel_size"] == 4
    assert plan["decode_clique"]["tensor_parallel_size"] in (2, 4)
    assert plan["kv_cache_transfer"]["protocol"] == "rdma"

    # Allow background watcher to execute and register into LiteLLM
    await asyncio.sleep(0.1)

    # 3. Verify LiteLLM Registration (Contract 3)
    registered_record = litellm_sync._mock_registry.get("llama-3-3-70b-e2e")
    assert registered_record is not None
    assert registered_record["model_name"] == "llama-3-3-70b-e2e"
    assert registered_record["litellm_params"]["model"] == "openai/meta-llama/Llama-3.3-70B-Instruct"
    assert "llama-3-3-70b-e2e-frontend" in registered_record["litellm_params"]["api_base"]
    assert registered_record["model_info"]["id"] == "dgd-llama-3-3-70b-e2e"

    # 4. Query status
    status_resp = client.get("/api/v1/models/llama-3-3-70b-e2e/status", headers=AUTH_HEADERS)
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["name"] == "llama-3-3-70b-e2e"

    # 5. List models
    list_resp = client.get("/api/v1/models", headers=AUTH_HEADERS)
    assert list_resp.status_code == 200
    assert any(m["name"] == "llama-3-3-70b-e2e" for m in list_resp.json())

    # 6. Delete Model (Graceful teardown: LiteLLM deregistration + dynamic drain + K8s cleanup)
    del_resp = client.delete("/api/v1/models/llama-3-3-70b-e2e", headers=AUTH_HEADERS)
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleting"

    # Execute background deletion
    await reconciler.delete_model("llama-3-3-70b-e2e")

    # Verify model was deregistered from LiteLLM
    assert "llama-3-3-70b-e2e" not in litellm_sync._mock_registry
    # Verify K8s custom resource was removed
    assert "llama-3-3-70b-e2e" not in k8s_client._mock_resources
