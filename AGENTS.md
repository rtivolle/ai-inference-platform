# Repository Guidelines

## Project Structure & Module Organization

This repository orchestrates Kubernetes inference with NVIDIA Dynamo, LiteLLM, and Cloudflare Tunnel.

- `control-plane/api/`: FastAPI endpoints, Kubernetes reconciliation, and LiteLLM synchronization.
- `control-plane/autotuner/`: Hugging Face model inspection, VRAM calculations, and SLA planning.
- `contracts/`: JSON Schemas and the OpenAPI specification; keep these aligned with API and template changes.
- `dynamo/templates/` and `monitoring/`: Jinja2 deployment/autoscaling templates and Prometheus rules.
- `infra/` and `gateway/`: Kubernetes manifests and gateway configuration.
- `loader/`: Model download/cache warmer and its container definition.
- `scripts/`: Cluster bootstrap scripts. `tests/`: unit, rendering, and mocked lifecycle tests.

## Build, Test, and Development Commands

Use Python 3.11, matching CI and container images. Run from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r control-plane/requirements.txt pytest pytest-asyncio
ln -s control-plane control_plane  # Only if the import alias is absent.
uvicorn api.main:app --app-dir control-plane --reload --port 8000
pytest -v tests/
shellcheck scripts/*.sh
```

The ignored `control_plane` symlink supports test imports. Uvicorn starts the API; pytest checks sizing, schema-compatible rendering, and deployment lifecycle behavior. ShellCheck validates bootstrap scripts. Starting the API may initialize Kubernetes access; use configured development credentials.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions/modules, and `PascalCase` classes. Follow existing type annotations and Pydantic models. Preserve asynchronous I/O patterns in API integrations. Use two-space YAML indentation and existing hyphenated manifest names; Jinja2 templates end in `.yaml.j2`. No Python formatter or linter is currently configured.

## Testing Guidelines

Use pytest and pytest-asyncio. Name tests `test_*.py` with `test_*` functions; `pytest.ini` also collects `e2e_*.py` and `*_test.py`. Reuse fixtures from `tests/conftest.py` and mock Kubernetes/LiteLLM for lifecycle tests. Cover changed VRAM formulas, tensor-parallel constraints, contract rendering, and reconciliation behavior. There is no enforced numeric coverage threshold.

## Commit & Pull Request Guidelines

The available history contains one `feat:` commit. Follow that style with concise, imperative messages such as `fix: correct KV-cache sizing`. Target `main`; describe the problem, behavior change, affected contracts/manifests, and validation results. Link relevant issues and explain deployment/configuration implications. Both CI quality gates should pass.

## Security & Configuration

Use example configuration files as starting points. Keep real Cloudflare tokens, Hugging Face credentials, and gateway secrets out of commits; supply them through environment variables or Kubernetes Secrets.
