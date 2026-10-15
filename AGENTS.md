# Repository Guidelines

## Project Structure & Module Organization

This repository orchestrates distributed Kubernetes LLM inference with NVIDIA Dynamo (`v1.5.1`), LiteLLM Proxy, KEDA, Prometheus, and Cloudflare Tunnel, plus an opt-in data capture and single-GPU LoRA fine-tuning workflow.

- `control-plane/api/`: FastAPI REST endpoints (`main.py`), Argon2id/CSRF session authentication (`auth.py`), async Kubernetes client (`k8s_client.py`), operational workload controls (`k8s_controls.py`), Dynamo reconciler (`dynamo_reconciler.py`), LiteLLM synchronizer (`litellm_sync.py`), Prometheus telemetry (`metrics.py`), data capture/dataset curation (`data_models.py`, `data_routes.py`, `data_store.py`), and LoRA training/promotion (`training.py`, `training_routes.py`).
- `control-plane/api/web/`: Zero-build Vanilla ES6 JavaScript, HTML5, and CSS3 single-page administration console served at `/`.
- `control-plane/autotuner/`: Hugging Face metadata inspection (`hf_inspector.py`), analytical VRAM sizing (`vram_calculator.py`), and capacity-aware SLA planning (`sla_planner.py`).
- `contracts/`: JSON Schemas (`model_spec.schema.json`, `dynamo_crd.schema.json`, `data_workflow.schema.json`) and generated OpenAPI 3.1.0 specification (`control_plane_openapi.yaml`).
- `dynamo/`: Helm values (`helm-values/dynamo-platform-values.yaml`) and Jinja2 `DynamoGraphDeployment` `nvidia.com/v1beta1` templates (`templates/dgd-*.yaml.j2`).
- `loader/`: Per-node NVMe Hugging Face model cache warmer (`model_warmer.py`, `warmer-job.yaml.j2`, `Dockerfile`).
- `gateway/`: LiteLLM Proxy + PostgreSQL Kubernetes manifests (`litellm-deployment.yaml`), base configuration (`config.example.yaml`), and opt-in terminal-event callback (`data_capture.py`).
- `infra/` and `monitoring/`: Kubernetes manifests (RBAC, NVMe storage, GPU Operator, RDMA Network Operator, Cloudflare Tunnel/ExternalDNS/Ingress, training namespace/quota), KEDA `ScaledObject` templates, and Prometheus alert/recording rules.
- `training/`: Single-GPU TRL/PEFT LoRA trainer (`train.py`), offline CPU smoke test (`smoke_test.py`), and container image (`Dockerfile`).
- `scripts/`: Cluster bootstrap (`bootstrap_inference_cluster.sh`), multi-cluster digest release (`deploy_clusters.py`), LiteLLM installer (`install_litellm.py`), strict manifest validator (`validate_manifests.py`), data workflow renderer (`configure_data_workflow.py`), and OpenAPI/JSON Schema exporter (`export_data_contracts.py`).
- `docs/`: Documentation index (`docs/README.md`), follow-up & integration roadmap (`docs/followup-plan.md`), architectural audit (`docs/architecture-audit.md`), and forensic defect catalog (`docs/audit-defect-catalog.md`).
- `tests/`: 17 pytest modules (145 tests) covering sizing math, CRD rendering, reconciliation, authentication, K8s operational controls, multi-cluster releases, data/training workflows, and defect reproductions.

## Build, Test, and Development Commands

Use Python 3.11, matching CI and container images. Run from the repository root:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run the API and web console in explicit mock mode
CONTROL_PLANE_MOCK=1 uvicorn api.main:app --host 127.0.0.1 --port 8000

# Interactive first-run console setup (saves 0600 config to .inference/config.json)
python -m api setup

# Run the full test suite (145 tests)
CONTROL_PLANE_MOCK=1 pytest -v tests/

# Run linters, type checks, shellcheck, and contract/manifest validations
ruff check control-plane/ loader/ tests/ gateway/data_capture.py training/ scripts/*.py
mypy --ignore-missing-imports control-plane/ loader/ scripts/*.py
shellcheck scripts/*.sh
python -m scripts.validate_manifests
python -m scripts.export_data_contracts
```

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions/modules, and `PascalCase` classes. Follow Ruff (`line-length = 120`, target `py311`, rules `E, F, I, W`) and Mypy type annotations. Preserve asynchronous I/O patterns (`httpx.AsyncClient`, `kubernetes_asyncio`, `run_in_threadpool`) in API routes. Use two-space YAML indentation and hyphenated manifest names; Jinja2 templates end in `.yaml.j2`.

## Testing Guidelines

Use `pytest` and `pytest-asyncio` (`asyncio_mode = "auto"` in `pyproject.toml`). Name tests `test_*.py` or `e2e_*.py`. Reuse fixtures from `tests/conftest.py` and keep unit/E2E tests deterministic and offline (`CONTROL_PLANE_MOCK=1`). When modifying API routes or Pydantic models in `control-plane/api/`, run `python -m scripts.export_data_contracts` to keep `contracts/control_plane_openapi.yaml` and `contracts/data_workflow.schema.json` synchronized.

## Commit & Pull Request Guidelines

Use concise, imperative Conventional Commit messages (`feat:`, `fix:`, `docs:`, `chore:`). Target `main`; describe the problem, behavior change, affected contracts/manifests, and validation results. All four CI quality gates in `.github/workflows/ci.yaml` (Ruff & Mypy, Shellcheck & Contracts/`kubeconform`, Pytest Suite, and Docker Build Validation) must pass.

## Security & Configuration

Use `*.example.yaml` and `*.example.json` files as templates. Never commit Cloudflare tokens, Hugging Face tokens, LiteLLM master/salt keys, or kubeconfigs; inject them via environment variables or Kubernetes `Secret` references (`secretKeyRef`).
