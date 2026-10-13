"""Validate static and rendered manifests with kubeconform and pinned upstream CRDs."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import yaml
from api.dynamo_reconciler import DynamoReconciler
from api.k8s_client import K8sClient
from api.litellm_sync import LiteLLMSync

ROOT = Path(__file__).resolve().parents[1]
CRD_URLS = [
    "https://raw.githubusercontent.com/ai-dynamo/dynamo/v1.5.1/deploy/operator/config/crd/bases/nvidia.com_dynamographdeployments.yaml",
    "https://raw.githubusercontent.com/ai-dynamo/dynamo/v1.5.1/deploy/operator/config/crd/bases/nvidia.com_dynamographdeploymentscalingadapters.yaml",
    "https://raw.githubusercontent.com/kedacore/keda/v2.16.1/config/crd/bases/keda.sh_scaledobjects.yaml",
    "https://raw.githubusercontent.com/Mellanox/network-operator/v24.7.0/deployment/network-operator/crds/mellanox.com_nicclusterpolicies.yaml",
]


def json_schema(value: Any) -> Any:
    """Convert structural OpenAPI schemas, preserving free-form maps and unions."""
    if isinstance(value, list):
        return [json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: json_schema(item) for key, item in value.items() if not key.startswith("x-kubernetes-")}
    if value.get("x-kubernetes-int-or-string") or value.get("format") == "int-or-string":
        result.pop("type", None)
        result.pop("format", None)
        result["anyOf"] = [{"type": "integer"}, {"type": "string"}]
    if "properties" in result and not value.get("x-kubernetes-preserve-unknown-fields"):
        result.setdefault("additionalProperties", False)
    if result.pop("nullable", False) and isinstance(result.get("type"), str):
        result["type"] = [result["type"], "null"]
    return result


def download_schemas(directory: Path) -> None:
    with urlopen(
        "https://raw.githubusercontent.com/yannh/kubernetes-json-schema/master/v1.31.0-standalone-strict/"
        "_definitions.json", timeout=60,
    ) as response:
        definitions = json.load(response)["definitions"]
    for url in CRD_URLS:
        with urlopen(url, timeout=60) as response:
            crd = yaml.safe_load(response.read())
        spec = crd["spec"]
        for version in spec["versions"]:
            if not version.get("served") or (spec["group"] == "nvidia.com" and version["name"] != "v1beta1"):
                continue
            schema = json_schema(version["schema"]["openAPIV3Schema"])
            # CRD metadata is deliberately untyped; use the Kubernetes ObjectMeta schema.
            schema["properties"]["metadata"] = definitions["io.k8s.apimachinery.pkg.apis.meta.v1.ObjectMeta"]
            schema["definitions"] = definitions
            name = f"{spec['group']}_{spec['names']['kind']}_{version['name']}".lower()
            (directory / f"{name}.json").write_text(json.dumps(schema))


def rendered_manifests() -> list[dict[str, Any]]:
    reconciler = DynamoReconciler(k8s_client=K8sClient(mock_mode=True), litellm_sync=LiteLLMSync(mock_mode=True))
    manifests = []
    for mode in ("aggregated", "disaggregated"):
        for protocol in ("tcp", "rdma"):
            clique = {
                "replicas": 1, "tensor_parallel_size": 2, "pipeline_parallel_size": 2,
                "gpu_sku": "NVIDIA-H100-80GB", "max_num_seqs": 128,
                "max_num_batched_tokens": 16384, "enable_chunked_prefill": True,
            }
            plan = {
                "deployment_name": f"validation-{mode}-{protocol}", "model_id": "example/model",
                "serving_mode": mode, "dtype": "bfloat16", "quantization": "fp8" if protocol == "rdma" else None,
                "max_model_len": 8192, "gpu_memory_utilization": 0.9,
                "worker_clique": clique, "prefill_clique": clique, "decode_clique": clique,
                "kv_cache_transfer": {"protocol": protocol, "engine": "nixl"},
                "target_node_names": ["gpu-0", "gpu-1"], "target_node": "gpu-0",
                "warmer_image_pull_secret": "registry-credentials",
            }
            manifests.append(reconciler.render_manifest(plan))
            manifests.append(reconciler.render_warmer_job(plan))
            manifests.extend(reconciler.render_scaled_objects(plan))
    return manifests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconform", default="kubeconform")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="inference-manifests-") as temporary:
        directory = Path(temporary)
        download_schemas(directory)
        manifests = rendered_manifests()
        for folder in ("infra", "gateway"):
            for path in sorted((ROOT / folder).rglob("*.yaml")):
                for document in yaml.safe_load_all(path.read_text()):
                    # Helm values and gateway configuration have no GVK.
                    if isinstance(document, dict) and ("apiVersion" in document or "kind" in document):
                        manifests.append(document)
        output = directory / "manifests.yaml"
        output.write_text(yaml.safe_dump_all(manifests, sort_keys=False))
        subprocess.run([
            args.kubeconform, "-strict", "-summary", "-kubernetes-version", "1.31.0",
            "-schema-location", "default", "-schema-location",
            str(directory / "{{.Group}}_{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"), str(output),
        ], check=True, env={**os.environ})


if __name__ == "__main__":
    main()
