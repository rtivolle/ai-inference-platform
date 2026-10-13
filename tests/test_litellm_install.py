"""LiteLLM installation, persistence, credential reuse, and rollout failure checks."""

import base64
import copy
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_litellm.py"
spec = importlib.util.spec_from_file_location("install_litellm", SCRIPT)
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

IMAGE = "ghcr.io/berriai/litellm@sha256:" + "a" * 64


def test_credentials_are_shared_and_stable_on_reinstall():
    gateway, control = installer.runtime_configuration({}, {}, None)
    assert gateway["litellm-master-key"] == control["litellm-master-key"]
    assert gateway["litellm-master-key"].startswith("sk-")
    assert gateway["postgres-password"] in gateway["database-url"]
    original = copy.deepcopy((gateway, control))
    assert installer.runtime_configuration(gateway, control, None) == original
    assert (gateway, control) == original


def test_legacy_encryption_key_and_existing_fields_are_preserved():
    gateway, control = installer.runtime_configuration(
        {"litellm-master-key": "sk-existing", "database-url": "postgresql://db/existing", "other": "keep"},
        {"api-token": "existing-token"}, None,
    )
    assert gateway["litellm-salt-key"] == "sk-existing"
    assert gateway["other"] == "keep"
    assert control["api-token"] == "existing-token"
    assert "postgres-password" not in gateway


@pytest.mark.parametrize("gateway,control,url", [
    ({"litellm-master-key": "a"}, {"litellm-master-key": "b"}, None),
    ({"database-url": "postgresql://old/db"}, {}, "postgresql://new/db"),
    ({}, {}, "mysql://db"),
    ({"database-url": "postgresql://litellm:lost@" + installer.BUNDLED_DATABASE}, {}, None),
])
def test_conflicting_or_invalid_configuration_is_rejected(gateway, control, url):
    with pytest.raises(ValueError):
        installer.runtime_configuration(gateway, control, url)


def test_gateway_rendering_supports_persistent_or_external_postgres():
    bundled = installer.render_gateway(IMAGE, True, "fast-storage")
    postgres = next(doc for doc in bundled if doc["kind"] == "StatefulSet")
    assert postgres["spec"]["volumeClaimTemplates"][0]["spec"]["storageClassName"] == "fast-storage"
    external = installer.render_gateway(IMAGE, False)
    assert all(doc["metadata"]["name"] not in ("postgres", "postgres-service") for doc in external)
    proxy = next(doc for doc in external if doc["kind"] == "Deployment")
    container = proxy["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == IMAGE
    assert "startupProbe" in container
    config = next(doc for doc in external if doc["kind"] == "ConfigMap")
    settings = yaml.safe_load(config["data"]["config.yaml"])
    assert settings["general_settings"]["store_model_in_db"] is True
    assert settings["model_list"] == []
    assert "checksum/config" in proxy["spec"]["template"]["metadata"]["annotations"]
    assert "sk-internal" not in yaml.safe_dump_all(bundled)


def fake_cluster(monkeypatch, fail_database=False, legacy=False):
    calls = []
    monkeypatch.setattr(installer, "read_runtime", lambda *args: {})

    def kubectl(context, *args, payload=None):
        calls.append((context, args, payload))
        if "get" in args:
            return json.dumps({"items": [{}] if legacy else []})
        if "statefulset/postgres" in args and fail_database:
            raise RuntimeError("Database failed")
        return ""

    monkeypatch.setattr(installer, "kubectl", kubectl)
    return calls


def test_install_waits_for_database_and_wires_control_credentials(monkeypatch):
    calls = fake_cluster(monkeypatch)
    installer.install("test-context", IMAGE, None, None)
    secrets = [payload for _, _, payload in calls if payload and payload["kind"] == "Secret"]
    assert len(secrets) == 2
    assert secrets[0]["data"]["litellm-master-key"] == secrets[1]["data"]["litellm-master-key"]
    db_ready = next(i for i, (_, args, _) in enumerate(calls) if "statefulset/postgres" in args)
    proxy_apply = next(i for i, (_, _, payload) in enumerate(calls)
                       if payload and payload["kind"] == "List"
                       and any(doc["metadata"]["name"] == "litellm-proxy" for doc in payload["items"]))
    assert db_ready < proxy_apply
    assert "deployment/litellm-proxy" in calls[-1][1]
    assert all(context == "test-context" for context, _, _ in calls)


def test_database_failure_prevents_gateway_install(monkeypatch):
    calls = fake_cluster(monkeypatch, fail_database=True)
    with pytest.raises(RuntimeError, match="Database failed"):
        installer.install("test-context", IMAGE, None, None)
    assert not any(payload and payload["kind"] == "List"
                   and any(doc["metadata"]["name"] == "litellm-proxy" for doc in payload["items"])
                   for _, _, payload in calls)


def test_legacy_database_rejected_before_mutations(monkeypatch):
    calls = fake_cluster(monkeypatch, legacy=True)
    with pytest.raises(ValueError, match="Legacy ephemeral"):
        installer.install("test-context", IMAGE, None, None)
    assert not any("apply" in args for _, args, _ in calls)


def test_external_database_install_skips_postgres(monkeypatch):
    calls = fake_cluster(monkeypatch)
    installer.install("test-context", IMAGE, "postgresql://external/db", None)
    assert not any("statefulset/postgres" in args for _, args, _ in calls)
    assert not any(payload and payload["kind"] == "List"
                   and any(doc["metadata"]["name"] == "postgres" for doc in payload["items"])
                   for _, _, payload in calls)


def test_kubectl_sends_secrets_only_via_stdin_and_redacts_errors(monkeypatch):
    def run(command, **kwargs):
        assert "secret-value" not in " ".join(command)
        assert "secret-value" in kwargs["input"]
        raise subprocess.CalledProcessError(1, command, stderr="secret-value")

    monkeypatch.setattr(installer.subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        installer.kubectl("test-context", "apply", "-f", "-", payload={"data": "secret-value"})
    assert "secret-value" not in str(error.value)


def test_runtime_reader_handles_absence_and_decodes_existing_secrets(monkeypatch):
    monkeypatch.setattr(installer, "kubectl", lambda *args: '{"items": []}')
    assert installer.read_runtime("test-context", "new-namespace") == {}
    response = {"items": [{"data": {"api-token": base64.b64encode(b"existing-token").decode()}}]}
    monkeypatch.setattr(installer, "kubectl", lambda *args: json.dumps(response))
    assert installer.read_runtime("test-context", "existing-namespace") == {"api-token": "existing-token"}


def test_invalid_image_prevents_cluster_access(monkeypatch):
    def read(*args):
        pytest.fail("Invalid image must be rejected before cluster access")

    monkeypatch.setattr(installer, "read_runtime", read)
    with pytest.raises(ValueError, match="Images must"):
        installer.install("test-context", "example:latest", None, None)
