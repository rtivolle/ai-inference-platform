"""Release safety and cross-cluster isolation checks without live Kubernetes access."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy_clusters.py"
spec = importlib.util.spec_from_file_location("deploy_clusters", SCRIPT)
assert spec is not None and spec.loader is not None
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)

IMAGES = tuple(f"ghcr.io/example/{name}@sha256:{digit * 64}" for name, digit in
               (("control", "a"), ("loader", "b"), ("gateway", "c")))
CLUSTERS = [{"name": name, "context": f"context-{name}", "environment": name} for name in ("a", "b")]


def test_inventory_rejects_duplicate_context_and_unknown_target(tmp_path):
    path = tmp_path / "clusters.json"
    path.write_text(json.dumps({"clusters": CLUSTERS}))
    assert deployment.load_clusters(path, "b") == [CLUSTERS[1]]
    with pytest.raises(ValueError, match="Unknown cluster"):
        deployment.load_clusters(path, "missing")
    path.write_text(json.dumps({"clusters": [CLUSTERS[0], {**CLUSTERS[1], "context": "context-a"}]}))
    with pytest.raises(ValueError, match="unique"):
        deployment.load_clusters(path)


@pytest.mark.parametrize("image", ["example:latest", "example:v1", "example@sha256:abc", "-bad@sha256:" + "a" * 64])
def test_mutable_or_invalid_images_are_rejected(image):
    with pytest.raises(ValueError, match="Images must"):
        deployment.render_release(image, IMAGES[1], IMAGES[2])


def test_release_has_no_embedded_credentials_and_uses_runtime_rbac():
    manifests = deployment.render_release(*IMAGES)
    documents = {doc["metadata"]["name"]: doc for doc in manifests if doc["kind"] == "Deployment"}
    control = documents["control-plane"]["spec"]["template"]["spec"]
    assert control["serviceAccountName"] == "control-plane"
    container = control["containers"][0]
    assert container["image"] == IMAGES[0]
    env = {item["name"]: item for item in container["env"]}
    assert env["MODEL_WARMER_IMAGE"]["value"] == IMAGES[1]
    assert "secretKeyRef" in env["LITELLM_MASTER_KEY"]["valueFrom"]
    gateway = documents["litellm-proxy"]["spec"]["template"]["spec"]["containers"][0]
    assert gateway["image"] == IMAGES[2]
    gateway_env = {item["name"]: item for item in gateway["env"]}
    for name in ("LITELLM_MASTER_KEY", "DATABASE_URL", "LITELLM_SALT_KEY"):
        assert "secretKeyRef" in gateway_env[name]["valueFrom"]
    assert gateway_env["STORE_MODEL_IN_DB"]["value"] == "True"
    assert gateway_env["PROXY_CONFIG_RELOAD_INTERVAL_SECONDS"]["value"] == "10"
    assert "postgres" not in documents
    rendered = yaml.safe_dump_all(manifests)
    assert "litellm_secret" not in rendered
    assert "sk-internal" not in rendered


def test_preflight_failure_prevents_all_cluster_mutations(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(deployment, "kubectl", lambda context, *args: calls.append((context, args)) or "")

    def preflight(context):
        if context == "context-b":
            raise ValueError("Missing runtime secret")

    monkeypatch.setattr(deployment, "preflight", preflight)
    with pytest.raises(ValueError, match="Missing runtime secret"):
        deployment.deploy(CLUSTERS, deployment.render_release(*IMAGES), tmp_path / "release.yaml")
    assert all("--dry-run=server" in args for _, args in calls)


def test_gateway_rollout_precedes_control_plane_and_contexts_are_explicit(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(deployment, "preflight", lambda context: None)
    monkeypatch.setattr(deployment, "kubectl", lambda context, *args: calls.append((context, args)) or "")
    deployment.deploy(CLUSTERS, deployment.render_release(*IMAGES), tmp_path / "release.yaml")
    for cluster in CLUSTERS:
        commands = [args for context, args in calls if context == cluster["context"]]
        gateway_ready = next(i for i, args in enumerate(commands) if "deployment/litellm-proxy" in args)
        control_apply = next(i for i, args in enumerate(commands)
                             if args == ("apply", "-f", str(tmp_path / "release.yaml")))
        assert gateway_ready < control_apply
        assert "deployment/control-plane" in commands[-1]
    assert {context for context, _ in calls} == {cluster["context"] for cluster in CLUSTERS}


def test_gateway_failure_stops_control_plane_and_remaining_targets(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(deployment, "preflight", lambda context: None)

    def kubectl(context, *args):
        calls.append((context, args))
        if "deployment/litellm-proxy" in args:
            raise RuntimeError("Rollout failed")

    monkeypatch.setattr(deployment, "kubectl", kubectl)
    with pytest.raises(RuntimeError, match="Rollout failed"):
        deployment.deploy(CLUSTERS, deployment.render_release(*IMAGES), tmp_path / "release.yaml")
    assert not any("deployment/control-plane" in args for _, args in calls)
    assert not any(context == "context-b" and "--dry-run=server" not in args for context, args in calls)


def test_preflight_rejects_mismatched_master_keys(monkeypatch):
    def kubectl(context, *args):
        if "json" in args:
            return json.dumps({"data": {
                "litellm-master-key": args[1], "database-url": "encoded-db", "api-token": "encoded-token",
                "litellm-salt-key": "encoded-salt",
            }})
        return ""

    monkeypatch.setattr(deployment, "kubectl", kubectl)
    with pytest.raises(ValueError, match="master keys differ"):
        deployment.preflight("context-a")


def test_warmer_uses_release_image_and_pull_secret(monkeypatch, reconciler):
    monkeypatch.setenv("MODEL_WARMER_IMAGE", IMAGES[1])
    monkeypatch.setenv("MODEL_WARMER_IMAGE_PULL_SECRET", "registry-credentials")
    job = reconciler.render_warmer_job({
        "deployment_name": "test", "model_id": "example/model", "warmer_image": "local:latest",
    })
    pod = job["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == IMAGES[1]
    assert pod["imagePullSecrets"] == [{"name": "registry-credentials"}]


def test_kubectl_uses_selected_context_and_preserves_rollout_timeout(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ready")

    monkeypatch.setattr(deployment.subprocess, "run", run)
    deployment.kubectl("context-b", "rollout", "status", "deployment/control-plane", "--timeout=600s")
    assert calls[0][:4] == ["kubectl", "--context", "context-b", "--request-timeout=630s"]


def test_failed_secret_read_does_not_print_secret_contents(monkeypatch):
    def run(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, output="sensitive-secret-data", stderr="access denied")

    monkeypatch.setattr(deployment.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="access denied") as error:
        deployment.kubectl("context-a", "get", "secret", "inference-runtime", "-o", "json")
    assert "sensitive-secret-data" not in str(error.value)
