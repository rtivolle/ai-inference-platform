"""Launch the console, offering protected terminal setup on its first run."""

import argparse
import getpass
import json
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from argon2 import PasswordHasher

CONFIG_KEYS = {
    "CONTROL_PLANE_ADMIN_USERNAME", "CONTROL_PLANE_ADMIN_PASSWORD_HASH", "CONTROL_PLANE_API_TOKEN",
    "CONTROL_PLANE_PUBLIC_URL", "CONTROL_PLANE_COOKIE_SECURE", "CONTROL_PLANE_SESSION_DB",
    "CONTROL_PLANE_MOCK", "CONTROL_PLANE_KUBE_CONTEXT", "KUBECONFIG", "LITELLM_PROXY_URL",
    "LITELLM_MASTER_KEY", "PROMETHEUS_URL", "PROMETHEUS_BEARER_TOKEN",
}


def read_configuration(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError(f"Configuration permissions must be 0600: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or any(key not in CONFIG_KEYS or not isinstance(value, str) for key, value in data.items()):
        raise ValueError("Invalid console configuration.")
    return data


def write_configuration(path: Path, configuration: dict[str, str]):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(configuration, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def prompt(label: str, default: str = "") -> str:
    value = input(f"{label}{f' [{default}]' if default else ''}: ").strip()
    return value or default


def url_prompt(label: str, default: str) -> str:
    while True:
        value = prompt(label, default)
        parsed = urlsplit(value)
        if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment:
            return value.rstrip("/")
        print("Enter an HTTP(S) URL without credentials, query parameters, or a fragment.")


def setup(path: Path, existing: dict[str, str]) -> dict[str, str]:
    print("Inference console · first-run setup")
    print("Passwords are entered privately. Local configuration is saved with owner-only permissions.")
    configuration = dict(existing)
    configuration["CONTROL_PLANE_ADMIN_USERNAME"] = prompt("Administrator username", existing.get("CONTROL_PLANE_ADMIN_USERNAME", "admin"))
    if not 1 <= len(configuration["CONTROL_PLANE_ADMIN_USERNAME"]) <= 128:
        raise ValueError("Administrator username must contain 1–128 characters.")
    while True:
        password = getpass.getpass("Administrator password (12+ characters): ")
        if len(password) < 12:
            print("Use at least 12 characters.")
            continue
        if password != getpass.getpass("Confirm password: "):
            print("Passwords did not match.")
            continue
        configuration["CONTROL_PLANE_ADMIN_PASSWORD_HASH"] = PasswordHasher().hash(password)
        break
    configuration["CONTROL_PLANE_API_TOKEN"] = os.getenv("CONTROL_PLANE_API_TOKEN") or existing.get("CONTROL_PLANE_API_TOKEN") or secrets.token_urlsafe(32)
    configuration["CONTROL_PLANE_PUBLIC_URL"] = url_prompt("Browser origin", existing.get("CONTROL_PLANE_PUBLIC_URL", "http://127.0.0.1:8000"))
    origin = urlsplit(configuration["CONTROL_PLANE_PUBLIC_URL"])
    if origin.path:
        raise ValueError("Browser origin must not contain a path.")
    local_http = origin.scheme == "http" and origin.hostname in ("127.0.0.1", "localhost", "::1")
    if origin.scheme != "https" and not local_http:
        raise ValueError("Use HTTPS for a browser origin outside loopback.")
    configuration["CONTROL_PLANE_COOKIE_SECURE"] = "0" if local_http else "1"
    configuration["CONTROL_PLANE_SESSION_DB"] = str(path.parent / "sessions.sqlite3")
    print("Kubernetes connection: 1 = local kubeconfig, 2 = in-cluster service account, 3 = offline mock")
    mode = prompt("Connection mode", "1")
    if mode not in ("1", "2", "3"):
        raise ValueError("Choose connection mode 1, 2, or 3.")
    configuration["CONTROL_PLANE_MOCK"] = "1" if mode == "3" else "0"
    configuration.pop("KUBECONFIG", None)
    configuration.pop("CONTROL_PLANE_KUBE_CONTEXT", None)
    if mode == "1":
        kubeconfig = Path(prompt("Existing kubeconfig path", os.getenv("KUBECONFIG", str(Path.home() / ".kube/config")))).expanduser().resolve()
        data = yaml.safe_load(kubeconfig.read_text())
        contexts = [item["name"] for item in data.get("contexts", [])]
        if not contexts:
            raise ValueError("The kubeconfig has no contexts.")
        print("Available contexts: " + ", ".join(contexts))
        context = prompt("Kubernetes context", data.get("current-context") or contexts[0])
        if context not in contexts:
            raise ValueError("The selected context is absent from this kubeconfig.")
        configuration["KUBECONFIG"] = str(kubeconfig)
        configuration["CONTROL_PLANE_KUBE_CONTEXT"] = context
    if mode != "3":
        configuration["LITELLM_PROXY_URL"] = url_prompt("LiteLLM gateway URL", existing.get("LITELLM_PROXY_URL", "http://litellm-service.ai-gateway.svc.cluster.local:4000" if mode == "2" else "http://127.0.0.1:4000"))
        master_key = os.getenv("LITELLM_MASTER_KEY") or getpass.getpass("Existing LiteLLM master key: ")
        if not master_key:
            raise ValueError("The existing gateway's master key is required for live mode.")
        configuration["LITELLM_MASTER_KEY"] = master_key
        configuration["PROMETHEUS_URL"] = url_prompt("Prometheus URL", existing.get("PROMETHEUS_URL", "http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090" if mode == "2" else "http://127.0.0.1:9090"))
        configuration["PROMETHEUS_BEARER_TOKEN"] = os.getenv("PROMETHEUS_BEARER_TOKEN") or getpass.getpass("Prometheus bearer token (leave blank if not required): ")
    else:
        configuration.pop("LITELLM_MASTER_KEY", None)
        configuration.pop("PROMETHEUS_BEARER_TOKEN", None)
        configuration["PROMETHEUS_URL"] = ""
    write_configuration(path, configuration)
    print(f"Configuration saved to {path}. Kubernetes credentials remain in the selected kubeconfig or service account.")
    return configuration


def main():
    parser = argparse.ArgumentParser(description="Inference console with first-run configuration")
    parser.add_argument("command", nargs="?", choices=("serve", "setup"), default="serve")
    parser.add_argument("--config", type=Path, default=Path(os.getenv("CONTROL_PLANE_CONFIG_FILE", ".inference/config.json")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--force", action="store_true", help="Replace an existing first-run configuration")
    args = parser.parse_args()
    try:
        path = args.config.expanduser().resolve()
        configuration = read_configuration(path)
        if args.command == "setup" or (not configuration and not os.getenv("CONTROL_PLANE_ADMIN_PASSWORD_HASH")):
            if configuration and not args.force:
                raise ValueError("Configuration already exists; use setup --force to replace it.")
            if not sys.stdin.isatty():
                raise ValueError("Run python -m api setup in an interactive terminal for first-run configuration, or supply configuration through environment variables.")
            configuration = setup(path, configuration)
        for key, value in configuration.items():
            os.environ.setdefault(key, value)
        if args.command == "setup":
            return
        import uvicorn

        uvicorn.run("api.main:app", host=args.host, port=args.port)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
