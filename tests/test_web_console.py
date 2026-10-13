"""Console delivery and API behavior used by the browser UI."""

from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def web_client(monkeypatch, mock_k8s, mock_litellm, reconciler):
    monkeypatch.setenv("CONTROL_PLANE_MOCK", "1")
    monkeypatch.setenv("CONTROL_PLANE_API_TOKEN", "test-token")
    from api import main

    monkeypatch.setattr(main, "k8s_client", mock_k8s)
    monkeypatch.setattr(main, "litellm_sync", mock_litellm)
    monkeypatch.setattr(main, "reconciler", reconciler)
    with TestClient(main.app) as client:
        yield client


def test_console_and_assets_are_public_but_inventory_requires_auth(web_client):
    response = web_client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert 'id="deploy-form"' in response.text
    for asset, content_type in [("styles.css", "text/css"), ("app.js", "javascript")]:
        response = web_client.get(f"/assets/{asset}")
        assert response.status_code == 200
        assert content_type in response.headers["content-type"]
    assert web_client.get("/assets/missing.js").status_code == 404
    assert web_client.get("/api/v1/cluster/gpus").status_code == 401
    assert web_client.get("/api/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert web_client.get("/health").json()["mode"] == "mock"


@pytest.mark.parametrize("invalid_field", [
    {"model_id": ""},
    {"model_id": "model with spaces"},
    {"profile": "unsupported"},
    {"dtype": "fp8"},
    {"quantization": "bf16"},
    {"max_model_len": 0},
    {"target_concurrency": -1},
    {"gpu_memory_utilization": 0},
    {"gpu_memory_utilization": 1.01},
    {"deployment_name": "Invalid_Name"},
])
def test_invalid_deployment_fields_are_rejected_before_scheduling(web_client, monkeypatch, invalid_field):
    from api import main

    planner = Mock()
    monkeypatch.setattr(main.sla_planner, "plan", planner)
    response = web_client.post(
        "/api/v1/models/deploy",
        json={"model_id": "meta-llama/Meta-Llama-3-8B-Instruct", **invalid_field},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 422
    planner.assert_not_called()


def test_console_receives_serving_mode_and_deployment_plan(web_client):
    headers = {"Authorization": "Bearer test-token"}
    response = web_client.post("/api/v1/models/deploy", headers=headers, json={
        "model_id": "meta-llama/Meta-Llama-3-8B-Instruct",
        "deployment_name": "console-test",
        "max_model_len": 8192,
        "target_concurrency": 4,
    })
    assert response.status_code == 200
    deployments = web_client.get("/api/v1/models", headers=headers).json()
    assert deployments[0]["serving_mode"] == "aggregated"
    detail = web_client.get("/api/v1/models/console-test/status", headers=headers).json()
    assert detail["serving_mode"] == "aggregated"
    assert detail["namespace"] == "dynamo-workloads"
    assert detail["plan"] == response.json()["plan"]
