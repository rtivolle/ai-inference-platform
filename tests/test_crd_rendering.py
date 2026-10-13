"""
Unit tests validating Jinja2 rendering of DynamoGraphDeployment Custom Resources against Contract 2.
"""

import json

import jsonschema
import pytest


@pytest.fixture
def crd_schema():
    with open("contracts/dynamo_crd.schema.json", "r", encoding="utf-8") as f:
        return json.load(f)


def test_render_disaggregated_crd(reconciler, crd_schema):
    sample_plan = {
        "model_id": "meta-llama/Llama-3.3-70B-Instruct",
        "deployment_name": "llama-3-3-70b",
        "serving_mode": "disaggregated",
        "dtype": "bfloat16",
        "quantization": "fp8",
        "max_model_len": 32768,
        "gpu_memory_utilization": 0.90,
        "estimated_vram_gb_per_replica": 78.4,
        "prefill_clique": {
            "replicas": 2,
            "tensor_parallel_size": 4,
            "pipeline_parallel_size": 1,
            "gpu_sku": "NVIDIA-H100-80GB",
            "max_num_batched_tokens": 16384,
            "enable_chunked_prefill": True,
        },
        "decode_clique": {
            "replicas": 4,
            "tensor_parallel_size": 2,
            "pipeline_parallel_size": 1,
            "gpu_sku": "NVIDIA-H100-80GB",
            "max_num_seqs": 256,
        },
        "kv_cache_transfer": {
            "protocol": "rdma",
            "engine": "nixl",
        },
    }

    manifest = reconciler.render_manifest(sample_plan, namespace="dynamo-workloads")

    # 1. Validate structure against CRD schema (Contract 2)
    jsonschema.validate(instance=manifest, schema=crd_schema)

    assert manifest["apiVersion"] == "nvidia.com/v1beta1"
    assert manifest["metadata"]["annotations"]["inference.platform/model-id"] == sample_plan["model_id"]
    assert manifest["spec"]["backendFramework"] == "vllm"
    components = {component["type"]: component for component in manifest["spec"]["components"]}
    frontend = components["frontend"]["podTemplate"]["spec"]["containers"][0]
    assert {"name": "DYN_ROUTER_MODE", "value": "kv"} in frontend["env"]
    for role, tp in (("prefill", 4), ("decode", 2)):
        component = components[role]
        assert component["scalingAdapter"] == {}
        container = component["podTemplate"]["spec"]["containers"][0]
        assert container["name"] == "main"
        assert container["command"] == ["python3", "-m", "dynamo.vllm"]
        assert container["resources"]["limits"]["nvidia.com/gpu"] == tp
        args = container["args"]
        assert f"--tensor-parallel-size={tp}" in args
        assert "--dtype=bfloat16" in args
        assert "--quantization=fp8" in args
        assert args[args.index("--disaggregation-mode") + 1] == role
        assert json.loads(args[args.index("--kv-transfer-config") + 1]) == {
            "kv_connector": "NixlConnector", "kv_role": "kv_both",
        }
        assert "IPC_LOCK" in container["securityContext"]["capabilities"]["add"]
    args = components["prefill"]["podTemplate"]["spec"]["containers"][0]["args"]
    assert json.loads(args[args.index("--kv-events-config") + 1])["enable_kv_cache_events"] is True


def test_render_aggregated_crd(reconciler, crd_schema):
    sample_plan = {
        "model_id": "meta-llama/Meta-Llama-3-8B-Instruct",
        "deployment_name": "llama-3-8b",
        "serving_mode": "aggregated",
        "dtype": "bfloat16",
        "quantization": None,  # None quantization must NOT render --quantization
        "max_model_len": 8192,
        "gpu_memory_utilization": 0.90,
        "estimated_vram_gb_per_replica": 18.5,
        "worker_clique": {
            "replicas": 2,
            "tensor_parallel_size": 1,
            "pipeline_parallel_size": 1,
            "gpu_sku": "NVIDIA-H100-80GB",
            "max_num_seqs": 128,
        },
        "kv_cache_transfer": {
            "protocol": "tcp",
            "engine": "default",
        },
    }

    manifest = reconciler.render_manifest(sample_plan, namespace="dynamo-workloads")

    # Validate schema
    jsonschema.validate(instance=manifest, schema=crd_schema)

    assert manifest["metadata"]["annotations"]["inference.platform/model-id"] == sample_plan["model_id"]
    worker = next(component for component in manifest["spec"]["components"] if component["type"] == "worker")
    assert worker["replicas"] == 2
    assert "scalingAdapter" not in worker
    container = worker["podTemplate"]["spec"]["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert "--tensor-parallel-size=1" in container["args"]
    assert "--dtype=bfloat16" in container["args"]
    assert not any("--quantization" in arg for arg in container["args"])
    assert "--disaggregation-mode" not in container["args"]
    assert "securityContext" not in container


@pytest.mark.parametrize("legacy_spec", [
    {"backend": "vllm", "services": {}},
    {"backendFramework": "vllm", "components": []},
    {"backendFramework": "vllm", "components": [{"name": "worker", "type": "worker", "replicas": 1}]},
])
def test_contract_rejects_legacy_and_incomplete_graphs(crd_schema, legacy_spec):
    manifest = {
        "apiVersion": "nvidia.com/v1beta1", "kind": "DynamoGraphDeployment",
        "metadata": {"name": "test", "annotations": {"inference.platform/model-id": "example/model"}},
        "spec": legacy_spec,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, crd_schema)
    manifest["apiVersion"] = "nvidia.com/v1alpha1"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, crd_schema)
