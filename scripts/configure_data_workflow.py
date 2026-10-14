"""Render opt-in manifests and deployment patches without reading/writing secrets."""

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(output: Path, config_path: Path, bucket: str, endpoint: str, image: str, owner: str):
    import re

    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Training image must be pinned by digest")
    if not bucket or not re.fullmatch(r"[A-Za-z0-9_.-]+", owner):
        raise ValueError("Provide an artifact bucket and Hugging Face owner")
    config = yaml.safe_load(config_path.read_text())
    settings = config.setdefault("litellm_settings", {})
    callbacks = settings.get("callbacks", [])
    if isinstance(callbacks, str):
        callbacks = [callbacks]
    settings["callbacks"] = list(dict.fromkeys([*callbacks, "data_capture.capture"]))
    config_map = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "litellm-data-capture", "namespace": "ai-gateway"},
                  "data": {"config.yaml": yaml.safe_dump(config, sort_keys=False), "data_capture.py": (ROOT / "gateway/data_capture.py").read_text()}}
    common_env = [{"name": "DATA_S3_BUCKET", "value": bucket}, {"name": "DATA_S3_ENDPOINT", "value": endpoint}]
    control_env: list[dict[str, Any]] = common_env + [{"name": "DATA_WORKFLOW_ENABLED", "value": "1"}, {"name": "DATA_DB_PATH", "value": "/var/lib/inference-data/metadata.sqlite3"},
                                {"name": "DATA_ARTIFACT_ROOT", "value": "/var/lib/inference-data/artifacts"}, {"name": "TRAINING_IMAGE", "value": image},
                                {"name": "TRAINING_HF_OWNER", "value": owner}]
    for variable, secret, key in [("DATA_CAPTURE_TOKEN", "data-runtime", "capture-token"), ("AWS_ACCESS_KEY_ID", "data-runtime", "access-key-id"), ("AWS_SECRET_ACCESS_KEY", "data-runtime", "secret-access-key"), ("HF_TOKEN", "data-runtime", "hf-read-token")]:
        control_env.append({"name": variable, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}})
    control_patch = {"spec": {"replicas": 1, "strategy": {"type": "Recreate", "rollingUpdate": None}, "template": {"spec": {
        "containers": [{"name": "control-plane", "env": control_env, "volumeMounts": [{"name": "inference-data", "mountPath": "/var/lib/inference-data"}]}],
        "volumes": [{"name": "inference-data", "persistentVolumeClaim": {"claimName": "inference-data"}}]}}}}
    gateway_patch = {"spec": {"template": {"spec": {
        "containers": [{"name": "litellm-proxy", "env": [{"name": "DATA_CAPTURE_ENABLED", "value": "1"}, {"name": "DATA_CAPTURE_CONTENT_ENABLED", "value": "1"},
            {"name": "DATA_CAPTURE_URL", "value": "http://control-plane-service.dynamo-control-plane.svc.cluster.local:8000/api/v1/data/events"},
            {"name": "DATA_CAPTURE_TOKEN", "valueFrom": {"secretKeyRef": {"name": "data-runtime", "key": "capture-token"}}}]}],
        "volumes": [{"name": "config", "configMap": {"name": "litellm-data-capture"}}]}}}}
    output.mkdir(parents=True, exist_ok=True)
    (output / "gateway-capture.yaml").write_text(yaml.safe_dump(config_map, sort_keys=False))
    (output / "control-plane.patch.json").write_text(json.dumps(control_patch, indent=2) + "\n")
    (output / "gateway.patch.json").write_text(json.dumps(gateway_patch, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--litellm-config", type=Path, default=ROOT / "gateway/config.example.yaml")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--training-image", required=True)
    parser.add_argument("--hf-owner", required=True)
    args = parser.parse_args()
    render(args.output, args.litellm_config, args.bucket, args.endpoint, args.training_image, args.hf_owner)


if __name__ == "__main__":
    main()
