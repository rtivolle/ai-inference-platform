# Development & CI

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run API (mock): `CONTROL_PLANE_MOCK=1 uvicorn api.main:app --host 127.0.0.1 --port 8000`
from repo root (package layout in `pyproject.toml`; no `sys.path` hacks). For interactive
first-run administrator setup, run `python -m api setup`.

## Tests

```bash
CONTROL_PLANE_MOCK=1 pytest -v tests/
```

The test suite contains **17 modules (145 tests)**:

| File | Covers |
|---|---|
| `test_vram_calculator.py` | VRAM formulas, GQA/MLA, TP/PP divisibility |
| `test_crd_rendering.py` | Jinja2 render + JSON Schema (`dynamo_crd.schema.json`) |
| `test_reconciliation.py` | Per-node warmer Jobs, DGD + KEDA lifecycle, teardown order |
| `e2e_deployment_flow.py` | End-to-end deploy, LiteLLM sync, drain, and delete flow |
| `test_cluster_deployment.py` | Multi-cluster inventory, SHA-256 digest checks, server dry-run |
| `test_cluster_inventory.py` | GFD node label parsing, GPU pool separation, pod GPU subtraction |
| `test_web_console.py` | `/` + `/assets` delivery, form validation, status responses |
| `test_console_setup.py` | Interactive `python -m api setup` config permissions and validation |
| `test_auth.py` | Argon2 login, session rotation, CSRF, Origin checks, IP rate limiting |
| `test_kubernetes_controls.py` | Component scaling via DGDSA, GPU SKU capacity check, pod logs & eviction |
| `test_usage_metrics.py` | Prometheus range/instant queries, caching, fail-closed drain checks |
| `test_litellm_install.py` | Installer credential generation, preservation, and rollout sequence |
| `test_data_workflow.py` | Consent redaction, idempotent capture, dataset splits, S3 conditional writes |
| `test_capture_callback.py` | LiteLLM async terminal-event callback queue, streaming handling, retries |
| `test_training_promotion.py` | Pinned revision inspection/warming/serving, OpenAPI sync, run promotion |
| `test_audit_defects_poc.py` | 18 deterministic verification tests for High/Critical audit findings |

Shared fixtures live in `tests/conftest.py` (`mock_k8s`, `mock_litellm`, `reconciler`,
`llama_*_specs`). Pytest configuration in `pyproject.toml` sets `asyncio_mode = "auto"`.

## Quality gates (`.github/workflows/ci.yaml`)

1. **Code Quality (`lint-and-typecheck`)**:
   - `ruff check control-plane/ loader/ tests/ gateway/data_capture.py training/ scripts/deploy_clusters.py scripts/install_litellm.py scripts/validate_manifests.py scripts/configure_data_workflow.py scripts/export_data_contracts.py`
   - `mypy --ignore-missing-imports control-plane/ loader/ scripts/deploy_clusters.py scripts/install_litellm.py scripts/validate_manifests.py scripts/configure_data_workflow.py scripts/export_data_contracts.py`
2. **Shellcheck & Contracts (`contracts-and-scripts`)**:
   - `shellcheck scripts/*.sh`
   - `python -m scripts.validate_manifests` (`kubeconform v0.7.0` against Kubernetes `1.31.0` and versioned Dynamo `v1.5.1`, KEDA `v2.16.1`, and Network Operator `v24.7.0` CRDs)
   - Draft 2020-12 JSON Schema validation on `contracts/model_spec.schema.json`, `contracts/dynamo_crd.schema.json`, and `contracts/data_workflow.schema.json`.
3. **Pytest Unit & E2E Validation (`unit-and-e2e-tests`)**:
   - `CONTROL_PLANE_MOCK=1 pytest -v tests/`
4. **Docker Build Validation (`docker-build`)**:
   - Builds `control-plane/Dockerfile`, `loader/Dockerfile`, and `training/Dockerfile` (CPU wheel index) and runs `/app/smoke_test.py` with `--network none`.

Deploy workflow (`deploy.yaml`) re-runs gates, then publishes GHCR images by
`github.sha` and deploys digests per cluster.

## Contracts

- Change `contracts/*.schema.json` + templates + `control_plane_openapi.yaml`
  together; run `python -m scripts.export_data_contracts` after modifying FastAPI routes
  or data/training Pydantic models.
- Never soften `dynamo_crd.schema.json` to fit a template — align templates with
  `nvidia.com/v1beta1` (`agent-plan.md` D3 lesson).
- Follow `AGENTS.md`: 4-space Python, `snake_case`/`PascalCase`, async I/O,
  2-space YAML, `*.yaml.j2` templates, `fix:`/`feat:` commits to `main`, no real
  secrets in git.
