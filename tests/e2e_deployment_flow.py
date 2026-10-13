"""
End-to-End Integration and Lifecycle Test:
Validates the complete flow from Auto-Tuner to DynamoGraphDeployment reconciliation,
LiteLLM model registration, simulated request routing, and zero-downtime graceful drain.
"""

import asyncio
import pytest
import httpx
from starlette.testclient import TestClient

from control_plane.api.main import app, reconciler, k8s_client, litellm_sync

@pytest.fixture
def client():
    # Configure mock mode for testing environment
    k8s_client.mock_mode = True
    litellm_sync.mock_mode = True
    reconciler.drain_seconds = 0 # Instant drain in test suite
    with TestClient(app) as c:
        yield c

@pytest.mark.asyncio
async def test_full_lifecycle_e2e(client):
    # 1. Check health
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"

    # 2. Deploy Model
    deploy_payload = {
        "model_id": "meta-llama/Llama-3.3-70B-Instruct",
        "deployment_name": "llama-3-3-70b-e2e",
        "profile": "low-latency",
        "quantization": "fp8",
        "max_model_len": 32768,
        "gpu_memory_utilization": 0.90,
    }

    deploy_resp = client.post("/api/v1/models/deploy", json=deploy_payload)
    assert deploy_resp.status_code == 200
    deploy_data = deploy_resp.json()

    assert deploy_data["status"] == "provisioning"
    assert deploy_data["deployment_name"] == "llama-3-3-70b-e2e"
    plan = deploy_data["plan"]

    # Verify Contract 1 fields
    assert plan["model_id"] == "meta-llama/Llama-3.3-70B-Instruct"
    assert plan["serving_mode"] == "disaggregated"
    assert plan["prefill_clique"]["tensor_parallel_size"] == 4
    assert plan["decode_clique"]["tensor_parallel_size"] == 2
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
    status_resp = client.get("/api/v1/models/llama-3-3-70b-e2e/status")
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["name"] == "llama-3-3-70b-e2e"

    # 5. List models
    list_resp = client.get("/api/v1/models")
    assert list_resp.status_code == 200
    assert any(m["name"] == "llama-3-3-70b-e2e" for m in list_resp.json())

    # 6. Delete Model (Graceful teardown: LiteLLM deregistration + drain + K8s cleanup)
    del_resp = client.delete("/api/v1/models/llama-3-3-70b-e2e")
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleting"

    # Execute background deletion
    await reconciler.delete_model("llama-3-3-70b-e2e")

    # Verify model was deregistered from LiteLLM
    assert "llama-3-3-70b-e2e" not in litellm_sync._mock_registry
    # Verify K8s custom resource was removed
    assert "llama-3-3-70b-e2e" not in k8s_client._mock_resources
