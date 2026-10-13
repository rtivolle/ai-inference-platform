"""Render or deploy the same immutable release to independent Kubernetes clusters."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMESPACES = ("dynamo-control-plane", "dynamo-workloads", "ai-gateway")


def load_clusters(path: Path, selected: str = "all") -> list[dict[str, str]]:
    clusters = json.loads(path.read_text())["clusters"]
    if not isinstance(clusters, list) or not clusters:
        raise ValueError("Inventory must contain a nonempty clusters list")
    names: set[str] = set()
    contexts: set[str] = set()
    for cluster in clusters:
        for key in ("name", "context", "environment"):
            value = cluster.get(key)
            if not isinstance(value, str) or not value or value.startswith("-"):
                raise ValueError(f"Each cluster needs a valid {key}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", cluster["name"]):
            raise ValueError("Cluster names must be DNS labels")
        if cluster["name"] in names or cluster["context"] in contexts:
            raise ValueError("Cluster names and contexts must be unique")
        names.add(cluster["name"])
        contexts.add(cluster["context"])
    if selected == "all":
        return clusters
    matches = [cluster for cluster in clusters if cluster["name"] == selected]
    if not matches:
        raise ValueError(f"Unknown cluster: {selected}")
    return matches


def immutable_image(value: str) -> str:
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", value) or value.startswith("-"):
        raise ValueError("Images must use repository@sha256:<64 lowercase hex characters>")
    return value


def render_release(control_image: str, loader_image: str, gateway_image: str) -> list[dict[str, Any]]:
    for value in (control_image, loader_image, gateway_image):
        immutable_image(value)
    manifests: list[dict[str, Any]] = []
    for source in ("infra/control-plane-rbac.yaml", "infra/control-plane-deployment.yaml",
                   "gateway/litellm-deployment.yaml"):
        manifests.extend(yaml.safe_load_all((ROOT / source).read_text()))
    # The multi-cluster release uses a separately managed persistent database.
    manifests = [doc for doc in manifests if doc["kind"] != "Namespace"
                 and doc["metadata"]["name"] not in ("postgres", "postgres-service")]
    gateway_config = (ROOT / "gateway/config.example.yaml").read_text()
    for doc in manifests:
        name = doc["metadata"]["name"]
        if doc["kind"] == "ConfigMap" and name == "litellm-config":
            doc["data"]["config.yaml"] = gateway_config
        if doc["kind"] != "Deployment":
            continue
        pod = doc["spec"]["template"]["spec"]
        pod["imagePullSecrets"] = [{"name": "registry-credentials"}]
        container = pod["containers"][0]
        container["imagePullPolicy"] = "IfNotPresent"
        if name == "control-plane":
            pod["serviceAccountName"] = "control-plane"
            container["image"] = control_image
            container["env"] = [
                {"name": "LITELLM_PROXY_URL", "value": "http://litellm-service.ai-gateway.svc.cluster.local:4000"},
                {"name": "LITELLM_MASTER_KEY", "valueFrom": {"secretKeyRef": {
                    "name": "inference-runtime", "key": "litellm-master-key"}}},
                {"name": "CONTROL_PLANE_API_TOKEN", "valueFrom": {"secretKeyRef": {
                    "name": "inference-runtime", "key": "api-token"}}},
                {"name": "MODEL_WARMER_IMAGE", "value": loader_image},
                {"name": "MODEL_WARMER_IMAGE_PULL_SECRET", "value": "registry-credentials"},
            ]
        elif name == "litellm-proxy":
            doc["spec"]["template"]["metadata"].setdefault("annotations", {})["checksum/config"] = (
                hashlib.sha256(gateway_config.encode()).hexdigest()
            )
            container["image"] = gateway_image
            container["env"] = [
                {"name": "LITELLM_MASTER_KEY", "valueFrom": {"secretKeyRef": {
                    "name": "inference-runtime", "key": "litellm-master-key"}}},
                {"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {
                    "name": "inference-runtime", "key": "database-url"}}},
                {"name": "LITELLM_SALT_KEY", "valueFrom": {"secretKeyRef": {
                    "name": "inference-runtime", "key": "litellm-salt-key"}}},
                {"name": "STORE_MODEL_IN_DB", "value": "True"},
                {"name": "PROXY_CONFIG_RELOAD_INTERVAL_SECONDS", "value": "10"},
            ]
    return manifests


def kubectl(context: str, *args: str) -> str:
    request_timeout = "630s" if "rollout" in args else "30s"
    try:
        result = subprocess.run(
            ["kubectl", "--context", context, f"--request-timeout={request_timeout}", *args],
            check=True, capture_output=True, text=True, timeout=660,
        )
    except subprocess.CalledProcessError as error:
        # Never include stdout: Secret preflight responses contain credentials.
        raise RuntimeError(f"kubectl failed for {context}: {error.stderr.strip()}") from None
    return result.stdout


def preflight(context: str) -> None:
    for namespace in NAMESPACES:
        kubectl(context, "get", "namespace", namespace, "-o", "name")
        kubectl(context, "-n", namespace, "get", "secret", "registry-credentials", "-o", "name")
    kubectl(context, "get", "crd", "dynamographdeployments.nvidia.com", "-o", "name")
    keys = {}
    for namespace in ("dynamo-control-plane", "ai-gateway"):
        secret = json.loads(kubectl(context, "-n", namespace, "get", "secret", "inference-runtime", "-o", "json"))
        data = secret.get("data", {})
        required = ["litellm-master-key"] + (
            ["database-url", "litellm-salt-key"] if namespace == "ai-gateway" else ["api-token"]
        )
        if any(not data.get(key) for key in required):
            raise ValueError(f"Missing runtime secret keys in {context}/{namespace}")
        keys[namespace] = data["litellm-master-key"]
    if len(set(keys.values())) != 1:
        raise ValueError(f"LiteLLM master keys differ between namespaces in {context}")


def deploy(clusters: list[dict[str, str]], manifests: list[dict[str, Any]], output: Path) -> None:
    # Check every target before changing the first cluster.
    for cluster in clusters:
        preflight(cluster["context"])
        kubectl(cluster["context"], "apply", "--dry-run=server", "-f", str(output))
    for cluster in clusters:
        context = cluster["context"]
        print(f"Deploying release to {cluster['name']} ({context})", flush=True)
        # Gateway must be ready before starting the control plane.
        gateway = [doc for doc in manifests if doc["metadata"].get("namespace") == "ai-gateway"]
        gateway_path = output.with_name("gateway.yaml")
        gateway_path.write_text(yaml.safe_dump_all(gateway, sort_keys=False))
        kubectl(context, "apply", "-f", str(gateway_path))
        kubectl(context, "-n", "ai-gateway", "rollout", "status", "deployment/litellm-proxy", "--timeout=600s")
        kubectl(context, "apply", "-f", str(output))
        kubectl(context, "-n", "dynamo-control-plane", "rollout", "status", "deployment/control-plane", "--timeout=600s")
        print(f"Rollouts ready: {cluster['name']}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--cluster", default="all")
    parser.add_argument("--control-image", required=True)
    parser.add_argument("--loader-image", required=True)
    parser.add_argument("--gateway-image", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("release"))
    parser.add_argument("--apply", action="store_true", help="Apply to clusters; default only renders locally")
    args = parser.parse_args()
    clusters = load_clusters(args.inventory, args.cluster)
    manifests = render_release(args.control_image, args.loader_image, args.gateway_image)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "release.yaml"
    output.write_text(yaml.safe_dump_all(manifests, sort_keys=False))
    (args.output_dir / "release.json").write_text(json.dumps({
        "clusters": clusters, "control_image": args.control_image,
        "loader_image": args.loader_image, "gateway_image": args.gateway_image,
        "source_revision": os.environ.get("GITHUB_SHA"),
    }, indent=2) + "\n")
    print(f"Rendered release: {output}", flush=True)
    if args.apply:
        deploy(clusters, manifests, output)


if __name__ == "__main__":
    main()
