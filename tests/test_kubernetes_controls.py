import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from api.k8s_controls import ControlError, KubernetesControls

HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def deployed(api_client):
    response = api_client.post("/api/v1/models/deploy", headers=HEADERS, json={
        "model_id": "meta-llama/Meta-Llama-3-8B-Instruct", "deployment_name": "operated-model",
        "max_model_len": 8192, "target_concurrency": 4,
    })
    assert response.status_code == 200
    return api_client


def test_workload_controls_scale_capacity_logs_and_pod_uid(deployed):
    path = "/api/v1/models/operated-model"
    workload = deployed.get(f"{path}/workload", headers=HEADERS).json()
    worker = next(component for component in workload["components"] if component["gpu_per_replica"] > 0)
    assert worker["desired_replicas"] == 8
    target = f"{path}/components/{worker['name']}/scale"
    assert deployed.post(target, headers=HEADERS, json={"replicas": 2}).status_code == 200
    inventory = deployed.get("/api/v1/cluster/gpus", headers=HEADERS).json()
    assert inventory["gpu_pools"][0]["available_count"] == 6
    assert deployed.post(target, headers=HEADERS, json={"replicas": 10}).status_code == 409
    assert deployed.post(target, headers=HEADERS, json={"replicas": 0}).status_code == 422
    pod = workload["pods"][0]
    assert deployed.get(f"{path}/pods/{pod['name']}/logs", headers=HEADERS).status_code == 200
    assert deployed.get(f"{path}/pods/unrelated-pod/logs", headers=HEADERS).status_code == 404
    assert deployed.post(f"{path}/pods/{pod['name']}/evict", headers=HEADERS, json={"uid": "stale"}).status_code == 409
    assert deployed.post(f"{path}/pods/{pod['name']}/evict", headers=HEADERS, json={"uid": pod["uid"]}).status_code == 200


def test_controls_require_auth_and_managed_ownership(deployed, mock_k8s):
    path = "/api/v1/models/operated-model/workload"
    assert deployed.get(path).status_code == 401
    assert deployed.get("/api/v1/cluster/nodes").status_code == 401
    mock_k8s._mock_resources["operated-model"]["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "another-platform"
    assert deployed.get(path, headers=HEADERS).status_code == 403


@pytest.mark.asyncio
async def test_live_scale_uses_adapter_and_resource_version(monkeypatch):
    from api import k8s_controls

    k8s = SimpleNamespace(mock_mode=False, _control_lock=asyncio.Lock(), get_cluster_gpu_inventory=AsyncMock(return_value={"gpu_pools": [{"available_count": 8}]}))
    controls = KubernetesControls(k8s)
    target = {"name": "decode", "adapter": "model-decode", "target_kind": "adapter", "api_version": "nvidia.com/v1beta1", "can_scale": True, "minimum_replicas": 1, "resource_version": "42", "desired_replicas": 1, "gpu_per_replica": 2}
    monkeypatch.setattr(controls, "components", AsyncMock(return_value=[target]))
    api_context = MagicMock()
    api_context.__aenter__ = AsyncMock(return_value=api_context)
    api_context.__aexit__ = AsyncMock()
    custom = SimpleNamespace(patch_namespaced_custom_object_scale=AsyncMock())
    monkeypatch.setattr(k8s_controls.client, "ApiClient", lambda: api_context)
    monkeypatch.setattr(k8s_controls.client, "CustomObjectsApi", lambda api: custom)
    with pytest.raises(ControlError, match="settings changed"):
        await controls.scale("model", "decode", 2, "41")
    await controls.scale("model", "decode", 2, "42")
    args = custom.patch_namespaced_custom_object_scale.call_args
    assert args.args == ("nvidia.com", "v1beta1", "dynamo-workloads", "dynamographdeploymentscalingadapters", "model-decode")
    assert args.kwargs["body"]["metadata"]["resourceVersion"] == "42"
    target["can_scale"] = False
    target["blocked_reason"] = "Managed by autoscaling."
    with pytest.raises(ControlError, match="autoscaling"):
        await controls.scale("model", "decode", 2, "42")


@pytest.mark.asyncio
async def test_pod_replacement_uses_eviction_and_uid_precondition(monkeypatch):
    from api import k8s_controls

    controls = KubernetesControls(SimpleNamespace(mock_mode=False, _control_lock=asyncio.Lock()))
    monkeypatch.setattr(controls, "pods", AsyncMock(return_value=[{"name": "managed-pod", "uid": "current-uid"}]))
    api_context = MagicMock()
    api_context.__aenter__ = AsyncMock(return_value=api_context)
    api_context.__aexit__ = AsyncMock()
    core = SimpleNamespace(create_namespaced_pod_eviction=AsyncMock())
    monkeypatch.setattr(k8s_controls.client, "ApiClient", lambda: api_context)
    monkeypatch.setattr(k8s_controls.client, "CoreV1Api", lambda api: core)
    await controls.evict_pod("model", "managed-pod", "current-uid")
    call = core.create_namespaced_pod_eviction.call_args
    assert call.args == ("managed-pod", "dynamo-workloads")
    assert call.kwargs["body"].api_version == "policy/v1"
    assert call.kwargs["body"].delete_options.preconditions.uid == "current-uid"


def test_disruption_budget_refusal_is_reported(deployed, monkeypatch):
    from api import main

    monkeypatch.setattr(KubernetesControls, "evict_pod", AsyncMock(side_effect=main.ApiException(status=429)))
    response = deployed.post("/api/v1/models/operated-model/pods/pod/evict", headers=HEADERS, json={"uid": "uid"})
    assert response.status_code == 409
    assert "disruption budget" in response.json()["detail"]


@pytest.mark.asyncio
async def test_failed_drain_keeps_workload_and_exposes_failure(reconciler, mock_k8s, monkeypatch):
    await mock_k8s.apply_dynamo_deployment({"metadata": {"name": "busy-model"}, "spec": {}})
    monkeypatch.setenv("CONTROL_PLANE_GATEWAY_DRAIN_DELAY_SECONDS", "0")
    monkeypatch.setattr(mock_k8s, "mock_mode", False)
    monkeypatch.setattr(reconciler, "drain_timeout_seconds", 1)
    monkeypatch.setattr(mock_k8s, "get_running_requests_count", AsyncMock(side_effect=RuntimeError("Monitoring unavailable")))
    with pytest.raises(RuntimeError, match="Monitoring unavailable"):
        await reconciler.delete_model("busy-model")
    assert "busy-model" in mock_k8s._mock_resources
    assert reconciler._in_memory_cache["busy-model"]["status"] == "delete_failed"
