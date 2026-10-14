import json
import stat

import pytest
from api.__main__ import read_configuration, setup, write_configuration
from api.auth import PASSWORD_HASHER, SessionStore


def test_first_run_stores_hash_and_owner_only_configuration(tmp_path, monkeypatch, capsys):
    answers = iter(["admin", "http://127.0.0.1:8000", "3"])
    passwords = iter(["test-first-run-password", "test-first-run-password"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(passwords))
    path = tmp_path / ".inference/config.json"
    config = setup(path, {})
    assert config["CONTROL_PLANE_MOCK"] == "1"
    assert config["PROMETHEUS_URL"] == ""
    assert PASSWORD_HASHER.verify(config["CONTROL_PLANE_ADMIN_PASSWORD_HASH"], "test-first-run-password")
    assert "test-first-run-password" not in path.read_text()
    assert "test-first-run-password" not in capsys.readouterr().out
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_configuration(path) == config
    store = SessionStore(config["CONTROL_PLANE_SESSION_DB"])
    store.db.close()
    assert stat.S_IMODE((path.parent / "sessions.sqlite3").stat().st_mode) == 0o600


def test_insecure_configuration_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"CONTROL_PLANE_ADMIN_USERNAME": "admin"}))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        read_configuration(path)


def test_unknown_settings_cannot_override_process_environment(tmp_path):
    path = tmp_path / "config.json"
    write_configuration(path, {"PATH": "/untrusted"})
    with pytest.raises(ValueError, match="Invalid console configuration"):
        read_configuration(path)


def test_first_run_selects_existing_kubeconfig_context(tmp_path, monkeypatch):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("contexts:\n  - name: gpu-production\ncurrent-context: gpu-production\n")
    answers = iter(["admin", "https://control.example.com", "1", str(kubeconfig), "gpu-production", "https://gateway.example.com", "https://prometheus.example.com"])
    passwords = iter(["test-first-run-password", "test-first-run-password", "gateway-test-key", "monitoring-test-key"])
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    monkeypatch.delenv("PROMETHEUS_BEARER_TOKEN", raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(passwords))
    config = setup(tmp_path / ".inference/config.json", {})
    assert config["KUBECONFIG"] == str(kubeconfig)
    assert config["CONTROL_PLANE_KUBE_CONTEXT"] == "gpu-production"
    assert config["CONTROL_PLANE_MOCK"] == "0"
    assert config["CONTROL_PLANE_COOKIE_SECURE"] == "1"
    assert config["PROMETHEUS_URL"] == "https://prometheus.example.com"
