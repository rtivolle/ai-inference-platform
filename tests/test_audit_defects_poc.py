"""
Defect Remediation Verification Test Suite.

Authoritative Reference:
- ORIGINAL_REQUEST.md (Requirements R1, R2, R3)
- DEFECT_CATALOG_AND_REMEDIATION.md (Defect specifications & unified diff remediations)
- PROJECT.md (Subsystem decomposition and interface contracts)

This test module verifies the resolution of the Critical and High severity defects
identified across all platform subsystems.
All tests execute without live cloud/cluster dependencies using mocks and fixtures.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath("."))

import jinja2
import jsonschema
import pydantic
import pytest
import yaml
from api.dynamo_reconciler import DynamoReconciler
from api.k8s_controls import ControlError, KubernetesControls
from api.litellm_sync import LiteLLMSync
from autotuner.hf_inspector import VERIFIED_MODELS, HFInspector
from autotuner.sla_planner import ClusterInventory, DeployRequest
from autotuner.vram_calculator import ModelArchitectureSpecs, VRAMCalculator
from jinja2 import Environment, FileSystemLoader
from starlette.testclient import TestClient

# ==============================================================================
# Subsystem 1: Control Plane API & Reconciler
# ==============================================================================

@pytest.mark.asyncio
async def test_poc_d_api_01_zero_drain_timeout_loop_bug():
    """Verification for DEF-API-01 (High - Functional Bug):
    In dynamo_reconciler.py, when drain_timeout_seconds is 0, the poller checks active requests
    at least once before checking the deadline. If active requests is 0, it drains cleanly
    without prematurely raising TimeoutError.
    """
    mock_k8s = MagicMock()
    mock_k8s.mock_mode = False
    mock_k8s.get_running_requests_count = AsyncMock(return_value=0)
    mock_k8s.delete_scaled_object = AsyncMock(return_value=True)
    mock_k8s.delete_dynamo_deployment = AsyncMock(return_value=True)
    mock_litellm = AsyncMock()
    mock_litellm.deregister_model = AsyncMock(return_value=True)

    reconciler = DynamoReconciler(k8s_client=mock_k8s, litellm_sync=mock_litellm)
    reconciler.drain_timeout_seconds = 0

    with patch.dict(os.environ, {"CONTROL_PLANE_GATEWAY_DRAIN_DELAY_SECONDS": "0"}):
        # Should not raise TimeoutError when active requests are 0
        await reconciler._delete_model_resources("test-deployment", "dynamo-workloads")
        mock_k8s.get_running_requests_count.assert_called_once_with("test-deployment")


def test_poc_d_api_04_session_store_unlocked_concurrency():
    """Verification for DEF-API-03 (High - Concurrency / Race Condition):
    SessionStore has a threading.RLock and WAL mode enabled, ensuring safe concurrent access
    across multiple threads with 0 transaction or concurrency errors.
    """
    from api.auth import SessionStore

    with tempfile.NamedTemporaryFile() as tmp:
        store = SessionStore(tmp.name)
        assert hasattr(store, "lock")

        errors = []

        def worker():
            try:
                for _ in range(30):
                    store.create("admin", 3600, "v1")
                    store.get("nonexistent")
                    store.revoke("nonexistent")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


@pytest.mark.asyncio
async def test_poc_d_api_07_litellm_404_blocks_cr_teardown():
    """Verification for DEF-API-02 (High - Error Handling / Resource Leak):
    In litellm_sync.py, HTTP 404 on /model/delete is treated as an idempotent success,
    returning True immediately without retrying or raising RuntimeError.
    """
    import httpx

    sync = LiteLLMSync(master_key="sk-test", mock_mode=False, base_url="http://litellm-service:4000")

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await sync.deregister_model("test-model")
        assert res is True
        assert mock_post.call_count == 1


@pytest.mark.asyncio
async def test_poc_d_api_08_component_scaling_cross_sku_overcommit():
    """Verification for DEF-API-08 (Medium - Functional / Scheduling):
    In k8s_controls.py, component scaling validates available capacity strictly against
    matching GPU SKU. When cluster lacks matching GPUs, it raises ControlError(409).
    """
    import asyncio

    mock_k8s = MagicMock()
    mock_k8s.mock_mode = True
    mock_k8s._control_lock = asyncio.Lock()
    mock_k8s.get_cluster_gpu_inventory = AsyncMock(
        return_value={
            "gpu_pools": [
                {"gpu_sku": "NVIDIA-H100-80GB", "available_count": 1},
                {"gpu_sku": "NVIDIA-T4-16GB", "available_count": 10},
            ]
        }
    )

    controls = KubernetesControls(mock_k8s)
    target = {
        "name": "worker",
        "can_scale": True,
        "minimum_replicas": 1,
        "desired_replicas": 1,
        "gpu_per_replica": 2,
        "gpu_sku": "NVIDIA-H100-80GB",
        "resource_version": "rv1",
        "target_kind": "deployment",
        "api_version": "nvidia.com/v1beta1",
    }
    controls.components = AsyncMock(return_value=[target])

    with pytest.raises(ControlError) as exc_info:
        await controls.scale("test-deployment", "worker", 3, "rv1")
    assert exc_info.value.status == 409
    assert "There is not enough unallocated GPU capacity" in str(exc_info.value)


@pytest.mark.asyncio
async def test_poc_d_api_09_pod_logs_empty_containers_index_error():
    """Verification for DEF-API-09 (Medium - Error Handling):
    In k8s_controls.py, when a pod has an empty containers list, pod_logs raises
    ControlError(404) with an informative message instead of an unhandled IndexError.
    """
    mock_k8s = MagicMock()
    mock_k8s.mock_mode = True
    controls = KubernetesControls(mock_k8s)

    mock_pod = {
        "name": "test-pod-0",
        "containers": [],
        "uid": "uid-123",
    }
    controls.pods = AsyncMock(return_value=[mock_pod])

    with pytest.raises(ControlError) as exc_info:
        await controls.pod_logs("test-model", "test-pod-0", container=None, tail_lines=50)
    assert exc_info.value.status == 404
    assert "Pod contains no inspectable containers" in str(exc_info.value)


# ==============================================================================
# Subsystem 2: Contracts & Templates
# ==============================================================================

def test_poc_d_01_jinja2_kv_cache_transfer_undefined():
    """Verification for DEF-CONTRACT-01 (High - Contract / Template Mismatch):
    Templates guard access to optional kv_cache_transfer, rendering cleanly without UndefinedError.
    """
    env = Environment(loader=FileSystemLoader(["dynamo/templates"]))
    tmpl = env.get_template("dgd-aggregated.yaml.j2")
    plan = {
        "deployment_name": "test-model",
        "namespace": "dynamo-workloads",
        "model_id": "meta-llama/Llama-3-8b",
        "dynamo_image": "vllm:latest",
        "dtype": "auto",
        "max_model_len": 2048,
        "gpu_memory_utilization": 0.9,
        "worker_components": [
            ("worker", {"replicas": 1, "tensor_parallel_size": 1, "gpu_sku": "L40S"})
        ],
        # kv_cache_transfer omitted
    }
    rendered = tmpl.render(plan)
    assert "test-model" in rendered
    assert "rdma/rdma_shared_device" not in rendered


def test_poc_d_02_status_endpoint_auth_enforcement():
    """Verification for DEF-CONTRACT-02 (High - Security / Contract Alignment):
    Verifies that /api/v1/models/{model_name}/status strictly enforces authentication matching
    the security requirement in contracts/control_plane_openapi.yaml.
    """
    with patch.dict(os.environ, {"CONTROL_PLANE_MOCK": "1", "CONTROL_PLANE_API_TOKEN": "secret-token"}):
        from api.main import app

        with TestClient(app) as client:
            resp = client.get("/api/v1/models/test-deployment/status")
            assert resp.status_code == 401, (
                f"Expected 401 Unauthorized per OpenAPI contract, got {resp.status_code}"
            )


def test_poc_d_04_deploy_request_bounds_mismatch_with_json_schema():
    """Verification for DEF-CONTRACT-04 / DEF-AUTOTUNER-07 (Contract / Validation Alignment):
    DeployRequest bounds match contracts/model_spec.schema.json (ge=512, ge=0.5).
    """
    with open("contracts/model_spec.schema.json", "r", encoding="utf-8") as f:
        schema = json.load(f)

    deploy_request_schema = schema["$defs"]["DeployRequest"]

    # max_model_len=10 now correctly fails Pydantic validation (min 512)
    with pytest.raises(pydantic.ValidationError) as exc_info:
        DeployRequest(model_id="test-model", max_model_len=10)
    assert "greater than or equal to 512" in str(exc_info.value)

    # Valid request passes both Pydantic and JSON Schema
    valid_req = DeployRequest(model_id="test-model", max_model_len=4096, gpu_memory_utilization=0.90)
    jsonschema.validate(valid_req.model_dump(exclude_none=True), deploy_request_schema)


# ==============================================================================
# Subsystem 3: Loader (Model Warmer)
# ==============================================================================

def test_poc_d_09_loader_model_warmer_missing_safe_flag():
    """Verification for DEF-LOADER-01 (High - Loader / Functional):
    loader/model_warmer.py registers --safe in argparse, so passing --safe is recognized.
    """
    result = subprocess.run(
        [sys.executable, "loader/model_warmer.py", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--safe" in result.stdout


def test_poc_d_10_loader_cache_layout_mismatch(tmp_path):
    """Verification for DEF-LOADER-02 (High - Loader / Cache Compatibility):
    loader/model_warmer.py writes to standard Hugging Face Hub cache layout.
    """
    from loader.model_warmer import warm_model

    model_id = "meta-llama/Llama-3-8b"
    cache_dir = str(tmp_path / "model-cache")

    with patch("loader.model_warmer.snapshot_download") as mock_download:
        repo_name = model_id.replace("/", "--")
        mock_download.return_value = os.path.join(cache_dir, "hub", f"models--{repo_name}")

        download_path = warm_model(model_id, cache_dir)
        mock_download.assert_called_once()

        passed_local_dir = mock_download.call_args.kwargs["local_dir"]
        expected_hub_path = os.path.join(cache_dir, "hub", f"models--{repo_name}")

        assert passed_local_dir == expected_hub_path
        assert "hub" in passed_local_dir.split(os.sep)


# ==============================================================================
# Subsystem 4: Autotuner (VRAM Calculator & SLA Planner)
# ==============================================================================

def test_poc_autotuner_def_01_cluster_inventory_none_vram_crash():
    """Verification for DEF-AUTOTUNER-02 (High - Functional Bug / Schema Mismatch):
    ClusterInventory permits nullable vram_gb without ValidationError when nodes lack memory labels.
    """
    inventory_data = {
        "nodes_count": 1,
        "gpu_pools": [
            {
                "gpu_sku": "NVIDIA-A100-SXM4-80GB",
                "vram_gb": None,
                "total_count": 4,
                "available_count": 4,
                "rdma_supported": False,
            }
        ],
        "rdma_network_active": False,
    }

    inv = ClusterInventory(**inventory_data)
    assert inv.gpu_pools[0].vram_gb is None


def test_poc_autotuner_def_02_attention_head_tp_divisibility():
    """Verification for DEF-AUTOTUNER-01 (High - Algorithmic / Container Crash):
    compute_parallelism enforces query attention head divisibility (specs.num_attention_heads % tp == 0).
    For a model with 12 query attention heads and 8 KV heads, TP=8 is rejected (12 % 8 != 0)
    and TP=4 is selected instead.
    """
    calc = VRAMCalculator()
    specs = ModelArchitectureSpecs(
        num_params=130_000_000_000,
        num_hidden_layers=80,
        hidden_size=8192,
        num_attention_heads=12,
        num_key_value_heads=8,
        head_dim=128,
    )
    tp, pp = calc.compute_parallelism(specs, gpu_vram_gb=80.0, available_gpus=8)

    assert tp == 4
    assert specs.num_key_value_heads % tp == 0
    assert specs.num_attention_heads % tp == 0


def test_poc_autotuner_def_06_fp32_param_sizing_and_mutable_cache():
    """Verification for DEF-AUTOTUNER-06 (Medium - Functional Bug / State Mutation):
    1. FP32 parameter sizing uses 4.0 bytes per param (40GB -> 10B params).
    2. HFInspector.inspect returns an independent deepcopy of VERIFIED_MODELS.
    """
    inspector = HFInspector()

    # 1. FP32 parameter count sizing
    fp32_config = {"hidden_size": 4096, "num_hidden_layers": 32, "torch_dtype": "float32"}
    specs = inspector._parse_config(fp32_config, total_size_bytes=40_000_000_000)
    assert specs.num_params == 10_000_000_000

    # 2. Immutable global profile cache
    profile = inspector.inspect("meta-llama/Meta-Llama-3-8B-Instruct")
    assert profile is not VERIFIED_MODELS["meta-llama/Meta-Llama-3-8B-Instruct"]
    assert profile.num_params == VERIFIED_MODELS["meta-llama/Meta-Llama-3-8B-Instruct"].num_params


# ==============================================================================
# Subsystem 5: Scripts, Infra & Monitoring
# ==============================================================================

def test_poc_d_05_bootstrap_script_relative_paths():
    """Verification for DEF-SCRIPT-01 (High - Script / Path Resolution):
    scripts/bootstrap_inference_cluster.sh prefixes all manifest checks with ${REPO_ROOT}.
    """
    with open("scripts/bootstrap_inference_cluster.sh", "r", encoding="utf-8") as f:
        content = f.read()

    relative_checks = re.findall(r'\[\[\s+-f\s+"\$\{REPO_ROOT\}/(infra/[^"]+)"\s+\]\]', content)
    assert len(relative_checks) >= 5


def test_poc_d_14_control_plane_rbac_missing_jobs_status():
    """Verification for DEF-INFRA-02 (High - Infra / RBAC):
    infra/control-plane-rbac.yaml includes jobs/status subresource.
    """
    with open("infra/control-plane-rbac.yaml", "r", encoding="utf-8") as f:
        docs = list(yaml.safe_load_all(f))

    role = next(
        doc
        for doc in docs
        if doc and doc.get("kind") == "Role" and doc.get("metadata", {}).get("name") == "control-plane"
    )
    batch_rules = [r for r in role.get("rules", []) if "batch" in r.get("apiGroups", [])]
    all_batch_resources = [res for r in batch_rules for res in r.get("resources", [])]

    assert "jobs" in all_batch_resources
    assert "jobs/status" in all_batch_resources


def test_poc_d_16_ingress_cross_namespace_service_routing():
    """Verification for DEF-INFRA-04 (High - Infra / Networking):
    infra/cloudflare/ingress-rules.yaml defines ExternalName services in ingress-nginx
    to enable cross-namespace routing to litellm-service and control-plane-service.
    """
    with open("infra/cloudflare/ingress-rules.yaml", "r", encoding="utf-8") as f:
        docs = list(yaml.safe_load_all(f))

    services = [d for d in docs if d and d.get("kind") == "Service"]
    assert len(services) == 2
    service_names = {s["metadata"]["name"]: s["spec"]["externalName"] for s in services}
    assert service_names["litellm-service"] == "litellm-service.ai-gateway.svc.cluster.local"
    assert service_names["control-plane-service"] == "control-plane-service.dynamo-control-plane.svc.cluster.local"


def test_poc_d_17_nvme_storageclass_invalid_readwritemany():
    """Verification for DEF-INFRA-05 (Medium - Infra / Storage):
    infra/storage-nvme/nvme-storageclass.yaml specifies ReadWriteOnce for local PV/PVC.
    """
    with open("infra/storage-nvme/nvme-storageclass.yaml", "r", encoding="utf-8") as f:
        docs = list(yaml.safe_load_all(f))

    pv = next(doc for doc in docs if doc and doc.get("kind") == "PersistentVolume")
    pvc = next(doc for doc in docs if doc and doc.get("kind") == "PersistentVolumeClaim")

    assert "ReadWriteOnce" in pv["spec"]["accessModes"]
    assert "ReadWriteMany" not in pv["spec"]["accessModes"]
    assert "ReadWriteOnce" in pvc["spec"]["accessModes"]
    assert "ReadWriteMany" not in pvc["spec"]["accessModes"]


def test_poc_d_18_keda_promql_nonexistent_dynamo_namespace_label():
    """Verification for DEF-MON-01 (High - Monitoring / KEDA):
    monitoring/keda-scaledobjects.yaml.j2 renders PromQL filtering by standard
    namespace and app_kubernetes_io_name labels instead of non-existent dynamo_namespace.
    """
    env = Environment(loader=FileSystemLoader(["monitoring"]))
    tmpl = env.get_template("keda-scaledobjects.yaml.j2")
    rendered = tmpl.render(
        deployment_name="test-dep",
        namespace="dynamo-workloads",
        model_id="llama-3-8b",
        prefill_clique={"replicas": 1},
        decode_clique={"replicas": 2},
    )
    assert "dynamo_namespace=" not in rendered
    assert "app_kubernetes_io_name=" in rendered
    assert 'namespace="dynamo-workloads"' in rendered
