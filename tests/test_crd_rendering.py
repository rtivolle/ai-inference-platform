"""
Unit tests validating Jinja2 rendering of DynamoGraphDeployment Custom Resources against Contract 2.
"""

import json
import jsonschema
import pytest
from control_plane.api.dynamo_reconciler import DynamoReconciler

@pytest.fixture
def crd_schema():
    with open("contracts/dynamo_crd.schema.json", "r", encoding="utf-8") as f:
        return json.load(f)

def test_render_disaggregated_crd(reconciler, crd_schema):
    sample_plan = {
        "model_id": "meta-llama/Llama-3.3-70B-Instruct",
        "deployment_name": "llama-3-3-70b",
        "serving_mode": "disaggregated",
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

    # 2. Assert key fields
    assert manifest["apiVersion"] == "nvidia.com/v1alpha1"
    assert manifest["kind"] == "DynamoGraphDeployment"
    assert manifest["metadata"]["name"] == "llama-3-3-70b"
    assert manifest["spec"]["backend"] == "vllm"
    assert manifest["spec"]["model"] == "meta-llama/Llama-3.3-70B-Instruct"
    assert manifest["spec"]["sharedStorage"]["hostPath"] == "/mnt/nvme/model-cache"

    services = manifest["spec"]["services"]
    assert "Frontend" in services
    assert services["Frontend"]["replicas"] == 2
    assert services["Frontend"]["ports"][0]["port"] == 8000

    assert "PrefillWorker" in services
    assert services["PrefillWorker"]["replicas"] == 2
    assert services["PrefillWorker"]["resources"]["limits"]["nvidia.com/gpu"] == 4
    assert "--tensor-parallel-size=4" in services["PrefillWorker"]["extraArgs"]
    assert "--quantization=fp8" in services["PrefillWorker"]["extraArgs"]

    assert "DecodeWorker" in services
    assert services["DecodeWorker"]["replicas"] == 4
    assert services["DecodeWorker"]["resources"]["limits"]["nvidia.com/gpu"] == 2
    assert "--tensor-parallel-size=2" in services["DecodeWorker"]["extraArgs"]

def test_render_aggregated_crd(reconciler, crd_schema):
    sample_plan = {
        "model_id": "meta-llama/Meta-Llama-3-8B-Instruct",
        "deployment_name": "llama-3-8b",
        "serving_mode": "aggregated",
        "quantization": "bf16",
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

    assert manifest["spec"]["model"] == "meta-llama/Meta-Llama-3-8B-Instruct"
    services = manifest["spec"]["services"]
    assert "Worker" in services
    assert services["Worker"]["replicas"] == 2
    assert services["Worker"]["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert "--tensor-parallel-size=1" in services["Worker"]["extraArgs"]
