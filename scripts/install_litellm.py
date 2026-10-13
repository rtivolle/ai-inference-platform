"""Install LiteLLM and configure shared control-plane credentials in one cluster."""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_NAMESPACE = "ai-gateway"
CONTROL_NAMESPACE = "dynamo-control-plane"
BUNDLED_DATABASE = "postgres-service.ai-gateway.svc.cluster.local:5432/litellm"


def kubectl(context: str, *args: str, payload: dict[str, Any] | None = None) -> str:
    """Send credentials over stdin, never argv, release files, or error output."""
    try:
        result = subprocess.run(
            ["kubectl", "--context", context, "--request-timeout=630s", *args],
            input=json.dumps(payload) if payload is not None else None,
            check=True, capture_output=True, text=True, timeout=660,
        )
    except subprocess.CalledProcessError:
        # kubectl apply errors may echo the full submitted Secret.
        raise RuntimeError(f"kubectl operation failed in context {context}; inspect cluster events") from None
    return result.stdout


def read_runtime(context: str, namespace: str) -> dict[str, str]:
    # Listing a named Secret returns an empty list when absent, including a new namespace.
    response = json.loads(kubectl(
        context, "-n", namespace, "get", "secrets", "--field-selector=metadata.name=inference-runtime", "-o", "json",
    ))
    items = response.get("items", [])
    if not items:
        return {}
    return {key: base64.b64decode(value, validate=True).decode() for key, value in items[0].get("data", {}).items()}


def runtime_configuration(
    gateway: dict[str, str], control: dict[str, str], database_url: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Preserve credentials on reinstall, including LiteLLM's legacy salt fallback."""
    gateway = dict(gateway)
    control = dict(control)
    keys = {data["litellm-master-key"] for data in (gateway, control) if data.get("litellm-master-key")}
    if len(keys) > 1:
        raise ValueError("Existing LiteLLM master keys differ between namespaces")
    master_key = next(iter(keys)) if keys else "sk-" + secrets.token_urlsafe(32)
    # Previously LiteLLM encrypted with the master key when no salt was configured.
    salt = gateway.get("litellm-salt-key") or (gateway.get("litellm-master-key") or secrets.token_urlsafe(32))
    existing_url = gateway.get("database-url")
    if database_url and existing_url and database_url != existing_url:
        raise ValueError("Database URL differs from the existing Secret; migrate the database separately")
    url = existing_url or database_url
    if url and not url.startswith(("postgresql://", "postgres://")):
        raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")
    if not url:
        password = gateway.setdefault("postgres-password", secrets.token_urlsafe(32))
        url = f"postgresql://litellm:{password}@{BUNDLED_DATABASE}"
    if url.endswith("@" + BUNDLED_DATABASE) and not gateway.get("postgres-password"):
        raise ValueError("Bundled database password is missing; recover it before reinstalling")
    gateway.update({"litellm-master-key": master_key, "litellm-salt-key": salt, "database-url": url})
    control["litellm-master-key"] = master_key
    if not control.get("api-token"):
        control["api-token"] = secrets.token_urlsafe(32)
    return gateway, control


def render_gateway(image: str, bundled: bool, storage_class: str | None = None) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image) or image.startswith("-"):
        raise ValueError("Images must use repository@sha256:<64 lowercase hex characters>")
    documents = list(yaml.safe_load_all((ROOT / "gateway/litellm-deployment.yaml").read_text()))
    config = (ROOT / "gateway/config.example.yaml").read_text()
    result = []
    for doc in documents:
        name = doc["metadata"]["name"]
        if doc["kind"] == "Namespace" or (not bundled and name in ("postgres", "postgres-service")):
            continue
        if name == "postgres" and storage_class is not None:
            doc["spec"]["volumeClaimTemplates"][0]["spec"]["storageClassName"] = storage_class
        if name == "litellm-config":
            doc["data"]["config.yaml"] = config
        if name == "litellm-proxy":
            template = doc["spec"]["template"]
            template["metadata"].setdefault("annotations", {})["checksum/config"] = hashlib.sha256(config.encode()).hexdigest()
            template["spec"]["containers"][0]["image"] = image
        result.append(doc)
    return result


def install(context: str, image: str, database_url: str | None, storage_class: str | None) -> None:
    # Validate the image and inspect existing state before writing anything.
    render_gateway(image, False)
    gateway, control = runtime_configuration(
        read_runtime(context, GATEWAY_NAMESPACE), read_runtime(context, CONTROL_NAMESPACE), database_url,
    )
    bundled = gateway["database-url"].endswith("@" + BUNDLED_DATABASE)
    if bundled:
        legacy = json.loads(kubectl(context, "-n", GATEWAY_NAMESPACE, "get", "deployments",
                                    "--field-selector=metadata.name=postgres", "-o", "json"))
        if legacy.get("items"):
            raise ValueError("Legacy ephemeral Postgres detected; migrate its data before installing persistent Postgres")
    manifests = render_gateway(image, bundled, storage_class)
    for namespace, data in ((GATEWAY_NAMESPACE, gateway), (CONTROL_NAMESPACE, control)):
        kubectl(context, "apply", "-f", "-", payload={
            "apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace},
        })
        kubectl(context, "apply", "-f", "-", payload={
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "inference-runtime", "namespace": namespace},
            "data": {key: base64.b64encode(value.encode()).decode() for key, value in data.items()},
        })
    if bundled:
        database = [doc for doc in manifests if doc["metadata"]["name"] in ("postgres", "postgres-service")]
        kubectl(context, "apply", "-f", "-", payload={"apiVersion": "v1", "kind": "List", "items": database})
        kubectl(context, "-n", GATEWAY_NAMESPACE, "rollout", "status", "statefulset/postgres", "--timeout=600s")
    gateway_manifests = [doc for doc in manifests if doc["metadata"]["name"] not in ("postgres", "postgres-service")]
    kubectl(context, "apply", "-f", "-", payload={"apiVersion": "v1", "kind": "List", "items": gateway_manifests})
    kubectl(context, "-n", GATEWAY_NAMESPACE, "rollout", "status", "deployment/litellm-proxy", "--timeout=600s")
    print("LiteLLM ready at http://litellm-service.ai-gateway.svc.cluster.local:4000")
    print("Control-plane credentials configured in dynamo-control-plane/inference-runtime")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="Explicit kubeconfig context")
    parser.add_argument("--image", required=True, help="Tested LiteLLM image as repository@sha256:<digest>")
    parser.add_argument("--storage-class", help="Bundled Postgres PVC storage class; defaults to cluster default")
    args = parser.parse_args()
    install(args.context, args.image, os.environ.get("DATABASE_URL"), args.storage_class)


if __name__ == "__main__":
    main()
