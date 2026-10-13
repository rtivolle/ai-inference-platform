# Development & CI

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run API (mock): `CONTROL_PLANE_MOCK=1 uvicorn api.main:app --host 127.0.0.1 --port 8000`
from repo root (package layout in `pyproject.toml`; no `sys.path` hacks).

## Tests

```bash
CONTROL_PLANE_MOCK=1 pytest -v tests/
```

| File | Covers |
|---|---|
| `test_vram_calculator.py` | VRAM formulas, GQA/MLA, TP divisibility |
| `test_crd_rendering.py` | Jinja render + JSON Schema |
| `test_reconciliation.py` | Deploy/delete lifecycle (mocked K8s/LiteLLM) |
| `e2e_deployment_flow.py` | Full flow incl. drain + blocked delete |
| `test_cluster_deployment.py` | Inventory/digest/dry-run logic |
| `test_web_console.py` | `/` + `/assets` routing |
| `test_litellm_install.py` | Installer credential handling |

Fixtures in `tests/conftest.py` (`mock_k8s`, `mock_litellm`, `reconciler`,
`llama_*_specs`). `pytest.ini`: `test_*.py`, `e2e_*.py`, `*_test.py`, asyncio auto.

## Quality gates (`.github/workflows/ci.yaml`)

1. `ruff check control-plane/ loader/ tests/ scripts/…` + `mypy --ignore-missing-imports …`
2. `shellcheck scripts/*.sh`; `python -m scripts.validate_manifests`
   (kubeconform v0.7.0 vs versioned Dynamo/KEDA/Network CRDs, no skipped schemas);
   JSON Schema check on both contracts.
3. `CONTROL_PLANE_MOCK=1 pytest -v tests/`; Docker builds for control-plane + loader.

Deploy workflow (`deploy.yaml`) re-runs gates, then publishes GHCR images by
`github.sha` and deploys digests per cluster.

## Contracts

- Change `contracts/*.schema.json` + templates + `control_plane_openapi.yaml`
  together; keep `dtype` vs `quantization` split, lowercase `nixl`, RFC 1123 names.
- Never soften `dynamo_crd.schema.json` to fit a template — fix the template to
  `nvidia.com/v1beta1` (`agent-plan.md` D3 lesson).
- Follow `AGENTS.md`: 4-space Python, `snake_case`/`PascalCase`, async I/O,
  2-space YAML, `*.yaml.j2` templates, `fix:`/`feat:` commits to `main`, no real
  secrets in git.
