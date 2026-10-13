"""Regression coverage for v1beta1, autoscaler ownership, and per-node warming."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from api import k8s_client as k8s_module
from api.k8s_client import K8sClient
from kubernetes_asyncio.client.rest import ApiException


@pytest.fixture
def plan():
    clique = {"replicas": 1, "tensor_parallel_size": 1, "gpu_sku": "NVIDIA-H100-80GB"}
    return {
        "deployment_name": "test-model", "model_id": "example/model", "serving_mode": "disaggregated",
        "dtype": "bfloat16", "quantization": None, "max_model_len": 8192, "gpu_memory_utilization": 0.9,
        "prefill_clique": clique, "decode_clique": clique, "worker_clique": clique,
        "kv_cache_transfer": {"protocol": "rdma", "engine": "nixl"},
    }


@pytest.mark.asyncio
async def test_warm_all_nodes_before_graph_and_scalers(reconciler, plan, monkeypatch):
    events = []
    jobs = []

    async def apply_job(job, namespace):
        jobs.append(job)
        events.append("job")

    async def wait(name, namespace, timeout_seconds):
        events.append("warm")
        return True

    async def apply_graph(manifest, namespace):
        assert events.count("warm") == 2
        events.append("graph")
        assert manifest["apiVersion"] == K8sClient.DGD_GROUP + "/" + K8sClient.DGD_VERSION
        for component in manifest["spec"]["components"]:
            if component["type"] == "frontend":
                continue
            affinity = component["podTemplate"]["spec"]["affinity"]["nodeAffinity"]
            assert affinity["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"] == ["gpu-a", "gpu-b"]

    async def apply_scaler(manifest, namespace):
        assert "graph" in events
        events.append("scaler")

    monkeypatch.setattr(reconciler.k8s, "get_target_gpu_nodes", AsyncMock(return_value=["gpu-a", "gpu-b"]))
    monkeypatch.setattr(reconciler.k8s, "apply_job", apply_job)
    monkeypatch.setattr(reconciler.k8s, "wait_for_job_completion", wait)
    monkeypatch.setattr(reconciler.k8s, "apply_dynamo_deployment", apply_graph)
    monkeypatch.setattr(reconciler.k8s, "apply_scaled_object", apply_scaler)
    monkeypatch.setattr(reconciler, "_watch_and_sync_litellm", AsyncMock())
    await reconciler.deploy_model(plan)
    await asyncio.sleep(0)
    assert events == ["job", "job", "warm", "warm", "graph", "scaler", "scaler"]
    assert {job["spec"]["template"]["spec"]["nodeSelector"]["kubernetes.io/hostname"] for job in jobs} == {"gpu-a", "gpu-b"}
    assert len({job["metadata"]["name"] for job in jobs}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("download failed"), TimeoutError("download timeout")])
async def test_warmer_failure_blocks_deployment(reconciler, plan, monkeypatch, failure):
    waiter = AsyncMock(return_value=failure) if failure is False else AsyncMock(side_effect=failure)
    graph = AsyncMock()
    scaler = AsyncMock()
    monkeypatch.setattr(reconciler.k8s, "wait_for_job_completion", waiter)
    monkeypatch.setattr(reconciler.k8s, "apply_dynamo_deployment", graph)
    monkeypatch.setattr(reconciler.k8s, "apply_scaled_object", scaler)
    with pytest.raises((RuntimeError, TimeoutError)):
        await reconciler.deploy_model(plan)
    graph.assert_not_awaited()
    scaler.assert_not_awaited()
    assert not reconciler._in_memory_cache


@pytest.mark.asyncio
async def test_no_eligible_hosts_blocks_graph(reconciler, plan, monkeypatch):
    monkeypatch.setattr(reconciler.k8s, "get_target_gpu_nodes", AsyncMock(return_value=[]))
    graph = AsyncMock()
    monkeypatch.setattr(reconciler.k8s, "apply_dynamo_deployment", graph)
    with pytest.raises(RuntimeError, match="No eligible GPU nodes"):
        await reconciler.deploy_model(plan)
    graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_scalers_persist_and_delete_after_restart(reconciler, plan, monkeypatch):
    monkeypatch.setattr(reconciler, "_watch_and_sync_litellm", AsyncMock())
    await reconciler.deploy_model(plan)
    await asyncio.sleep(0)
    assert len(reconciler.k8s._mock_scaled_objects) == 2
    reconciler._in_memory_cache.clear()
    await reconciler.reconcile_existing()
    assert reconciler._in_memory_cache[plan["deployment_name"]]["model_id"] == plan["model_id"]
    assert (await reconciler.get_deployment_status(plan["deployment_name"]))["model_id"] == plan["model_id"]
    assert (await reconciler.list_deployments())[0]["model_id"] == plan["model_id"]
    await reconciler.delete_model(plan["deployment_name"])
    assert not reconciler.k8s._mock_scaled_objects
    assert not reconciler.k8s._mock_resources


@pytest.mark.asyncio
async def test_aggregated_deployment_has_no_scalers(reconciler, plan, monkeypatch):
    plan["serving_mode"] = "aggregated"
    monkeypatch.setattr(reconciler, "_watch_and_sync_litellm", AsyncMock())
    await reconciler.deploy_model(plan)
    await asyncio.sleep(0)
    assert not reconciler.k8s._mock_scaled_objects
    await reconciler.delete_model(plan["deployment_name"])


@pytest.mark.asyncio
async def test_teardown_order_and_deregistration_failure(reconciler, monkeypatch):
    events = []

    async def deregister(name):
        events.append("deregister")

    async def delete_scaler(name, namespace):
        events.append("scaler")

    async def delete_graph(name, namespace):
        events.append("graph")
        return True

    monkeypatch.setattr(reconciler.litellm, "deregister_model", deregister)
    monkeypatch.setattr(reconciler.k8s, "delete_scaled_object", delete_scaler)
    monkeypatch.setattr(reconciler.k8s, "delete_dynamo_deployment", delete_graph)
    await reconciler.delete_model("test")
    assert events == ["deregister", "scaler", "scaler", "graph"]
    events.clear()
    monkeypatch.setattr(reconciler.litellm, "deregister_model", AsyncMock(side_effect=RuntimeError("gateway failed")))
    with pytest.raises(RuntimeError, match="gateway failed"):
        await reconciler.delete_model("test")
    assert not events


def test_job_name_is_bounded_and_unique_per_host(reconciler, plan):
    plan["deployment_name"] = "a" * 63
    names = [reconciler.render_warmer_job({**plan, "target_node": node})["metadata"]["name"] for node in ("gpu-a", "gpu-b")]
    assert all(len(name) <= 63 for name in names)
    assert names[0] != names[1]


def live_client(monkeypatch, api_type, api):
    context = AsyncMock()
    monkeypatch.setattr(k8s_module.client, "ApiClient", lambda: context)
    monkeypatch.setattr(k8s_module.client, api_type, lambda _: api)
    k8s = K8sClient(mock_mode=False)
    k8s._initialized = True
    return k8s


@pytest.mark.asyncio
@pytest.mark.parametrize("exists", [True, False])
async def test_live_scaler_create_or_patch(monkeypatch, exists):
    api = AsyncMock()
    if not exists:
        api.get_namespaced_custom_object.side_effect = ApiException(status=404)
    k8s = live_client(monkeypatch, "CustomObjectsApi", api)
    manifest = {"metadata": {"name": "test-prefill-scaler"}}
    await k8s.apply_scaled_object(manifest)
    method = api.patch_namespaced_custom_object if exists else api.create_namespaced_custom_object
    assert method.await_args.kwargs["group"] == "keda.sh"
    assert method.await_args.kwargs["plural"] == "scaledobjects"
    assert method.await_args.kwargs["body"] == manifest


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500])
async def test_live_scaler_api_failures_propagate(monkeypatch, status):
    api = AsyncMock()
    api.get_namespaced_custom_object.side_effect = ApiException(status=status)
    k8s = live_client(monkeypatch, "CustomObjectsApi", api)
    with pytest.raises(ApiException):
        await k8s.apply_scaled_object({"metadata": {"name": "test"}})
    api.create_namespaced_custom_object.assert_not_awaited()
    api.patch_namespaced_custom_object.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 403])
async def test_live_scaler_delete_only_ignores_not_found(monkeypatch, status):
    api = AsyncMock()
    api.delete_namespaced_custom_object.side_effect = ApiException(status=status)
    k8s = live_client(monkeypatch, "CustomObjectsApi", api)
    if status == 404:
        assert await k8s.delete_scaled_object("test") is False
    else:
        with pytest.raises(ApiException):
            await k8s.delete_scaled_object("test")


@pytest.mark.asyncio
async def test_waiter_requires_all_job_completions(monkeypatch):
    api = AsyncMock()
    api.read_namespaced_job_status.side_effect = [
        SimpleNamespace(spec=SimpleNamespace(completions=2), status=SimpleNamespace(succeeded=1, conditions=[])),
        SimpleNamespace(spec=SimpleNamespace(completions=2), status=SimpleNamespace(succeeded=2, conditions=[])),
    ]
    k8s = live_client(monkeypatch, "BatchV1Api", api)
    monkeypatch.setattr(k8s_module.asyncio, "sleep", AsyncMock())
    assert await k8s.wait_for_job_completion("test")
    assert api.read_namespaced_job_status.await_count == 2


@pytest.mark.asyncio
async def test_waiter_rejects_failed_job(monkeypatch):
    api = AsyncMock()
    api.read_namespaced_job_status.return_value = SimpleNamespace(status=SimpleNamespace(
        succeeded=0, conditions=[SimpleNamespace(type="Failed", status="True")],
    ))
    k8s = live_client(monkeypatch, "BatchV1Api", api)
    with pytest.raises(RuntimeError, match="failed"):
        await k8s.wait_for_job_completion("test")


@pytest.mark.asyncio
async def test_gpu_node_selection_filters_readiness_sku_and_capacity(monkeypatch, plan):
    def node(name, count=4, sku="NVIDIA-H100-80GB", ready="True", cordoned=False):
        return SimpleNamespace(
            metadata=SimpleNamespace(labels={"kubernetes.io/hostname": name, "nvidia.com/gpu.product": sku}),
            spec=SimpleNamespace(unschedulable=cordoned),
            status=SimpleNamespace(allocatable={"nvidia.com/gpu": str(count)}, conditions=[SimpleNamespace(type="Ready", status=ready)]),
        )
    api = AsyncMock()
    api.list_node.return_value = SimpleNamespace(items=[
        node("gpu-a"), node("gpu-b"), node("cpu", count=0), node("other-sku", sku="T4"),
        node("unready", ready="False"), node("cordoned", cordoned=True),
    ])
    k8s = live_client(monkeypatch, "CoreV1Api", api)
    assert await k8s.get_target_gpu_nodes(plan) == ["gpu-a", "gpu-b"]
    assert await k8s.get_target_gpu_nodes({**plan, "target_node": "gpu-b"}) == ["gpu-b"]


@pytest.mark.asyncio
async def test_replacing_warmer_waits_for_deletion(monkeypatch):
    api = AsyncMock()
    api.read_namespaced_job.side_effect = [object(), ApiException(status=404)]
    k8s = live_client(monkeypatch, "BatchV1Api", api)
    monkeypatch.setattr(k8s_module.asyncio, "sleep", AsyncMock())
    await k8s.apply_job({"metadata": {"name": "test"}})
    assert api.read_namespaced_job.await_count == 2
    assert api.delete_namespaced_job.await_args.kwargs["propagation_policy"] == "Foreground"
    api.create_namespaced_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_forbidden_warmer_deletion_blocks_replacement(monkeypatch):
    api = AsyncMock()
    api.delete_namespaced_job.side_effect = ApiException(status=403)
    k8s = live_client(monkeypatch, "BatchV1Api", api)
    with pytest.raises(ApiException):
        await k8s.apply_job({"metadata": {"name": "test"}})
    api.create_namespaced_job.assert_not_awaited()


def test_warmer_job_names_distinguish_long_deployment_names(reconciler, plan):
    names = [reconciler.render_warmer_job({
        **plan, "deployment_name": "a" * 60 + suffix, "target_node": "gpu-a",
    })["metadata"]["name"] for suffix in ("-b", "-c")]
    assert names[0] != names[1]
