# Project: AI Inference Platform Codebase Audit & Defect Analysis

## Architecture
The platform orchestrates distributed LLM inference on Kubernetes with NVIDIA Dynamo, LiteLLM Proxy, and Cloudflare Tunnels:
- **Control Plane API (`control-plane/api/`)**: FastAPI application providing authenticated REST endpoints, session management with Argon2 & CSRF, asynchronous Kubernetes client (`k8s_client.py`), operational controls (`k8s_controls.py`), reconciliation engine (`dynamo_reconciler.py`), LiteLLM registration/synchronization (`litellm_sync.py`), Prometheus metrics collection (`metrics.py`), and data capture/dataset export (`data_routes.py`, `data_store.py`).
- **Web Console (`control-plane/api/web/`)**: Vanilla JS SPA consuming control plane APIs for cluster monitoring, model deployment, pod logs, and operational controls.
- **Autotuner (`control-plane/autotuner/`)**: Hugging Face metadata inspector (`hf_inspector.py`), mathematical VRAM formulas (`vram_calculator.py`), and SLA deployment planner (`sla_planner.py`).
- **Contracts (`contracts/`)**: JSON Schemas (`dynamo_crd.schema.json`, `model_spec.schema.json`) and OpenAPI 3.1.0 specification (`control_plane_openapi.yaml`).
- **Templates (`dynamo/templates/`)**: Jinja2 templates for aggregated (`dgd-aggregated.yaml.j2`) and disaggregated (`dgd-disaggregated.yaml.j2`, `dgd-components.yaml.j2`) `DynamoGraphDeployment` manifests.
- **Loader (`loader/`)**: Model cache warmer (`model_warmer.py`), K8s Job template (`warmer-job.yaml.j2`), and Docker container (`Dockerfile`).
- **Automation Scripts (`scripts/`)**: Bare-metal bootstrap (`bootstrap_inference_cluster.sh`), multi-cluster deployment (`deploy_clusters.py`), LiteLLM installer (`install_litellm.py`), and manifest validation (`validate_manifests.py`).
- **Infrastructure & Monitoring (`infra/`, `monitoring/`)**: Kubernetes manifests for control plane, Cloudflare tunnels, RBAC, NVMe storage, KEDA ScaledObjects, and Prometheus alert/recording rules.
- **Test Suite (`tests/`)**: 12 pytest test modules with 145 tests (127 baseline + 18 dedicated PoC tests) verifying sizing, CRD rendering, reconciler lifecycle, auth, K8s controls, scripts, and defect reproductions.

---

## Feature Inventory
| # | Feature | Subsystem | Milestone | Source |
|---|---------|-----------|-----------|--------|
| F01 | FastAPI Entrypoint & Lifecycle | `control-plane/api` | M1 | Survey 1 |
| F02 | Session Management & Argon2 Auth | `control-plane/api` | M1 | Survey 1 |
| F03 | Kubernetes Async Client & Reconciler | `control-plane/api` | M1 | Survey 1 |
| F04 | Workload Operational Controls | `control-plane/api` | M1 | Survey 1 |
| F05 | LiteLLM Synchronization Client | `control-plane/api` | M1 | Survey 1 |
| F06 | Prometheus Telemetry & Metrics API | `control-plane/api` | M1 | Survey 1 |
| F07 | Data Capture & Dataset Review | `control-plane/api` | M1 | Survey 1 |
| F08 | Web Console Vanilla JS SPA | `control-plane/api/web` | M1 | Survey 1 |
| F09 | Hugging Face Metadata Inspector | `control-plane/autotuner` | M2 | Survey 2 |
| F10 | VRAM Calculation Engine (Weights, KV, Overhead) | `control-plane/autotuner` | M2 | Survey 2 |
| F11 | Parallelism Selection (TP/PP) & Clique Allocation | `control-plane/autotuner` | M2 | Survey 2 |
| F12 | SLA Planner & Disaggregated Routing | `control-plane/autotuner` | M2 | Survey 2 |
| F13 | Verified Models Database & GPU Profiles | `control-plane/autotuner` | M2 | Survey 2 |
| F14 | Existing Pytest Regression Test Suite (127 tests) | `tests/` | M2 | Survey 2 |
| F15 | Dynamo CRD JSON Schema Validation | `contracts/` | M3 | Survey 3 |
| F16 | Model Deployment Specification Schema | `contracts/` | M3 | Survey 3 |
| F17 | Control Plane OpenAPI 3.1.0 Contract | `contracts/` | M3 | Survey 3 |
| F18 | Dynamo Jinja2 CRD Templates | `dynamo/templates` | M3 | Survey 3 |
| F19 | Model Cache Warmer & K8s Job Template | `loader/` | M3 | Survey 3 |
| F20 | Cluster Bootstrap Automation Script | `scripts/` | M3 | Survey 3 |
| F21 | Multi-Cluster Deployment Automation | `scripts/` | M3 | Survey 3 |
| F22 | LiteLLM Automated Installer | `scripts/` | M3 | Survey 3 |
| F23 | Kubeconform Manifest Validation | `scripts/` | M3 | Survey 3 |
| F24 | Control Plane Manifests & RBAC Policies | `infra/` | M3 | Survey 3 |
| F25 | Cloudflare Tunnel & ExternalDNS Sync | `infra/cloudflare` | M3 | Survey 3 |
| F26 | KEDA ScaledObjects & Prometheus Alert Rules | `monitoring/` | M3 | Survey 3 |
| F27 | Defect Reproduction PoC Suite & Regression Verification (18 PoC tests) | `tests/` | M4 | E2E Track |

---

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Control Plane API & Web Console Audit & Remediation | `control-plane/api/`, `control-plane/api/web/` (Defects DEF-API-01 to DEF-API-16) | Phase 0 Survey | DONE |
| M2 | Autotuner & Existing Test Suite Audit & Remediation | `control-plane/autotuner/`, `tests/` (Defects DEF-AUTOTUNER-01 to 08) | Phase 0 Survey | DONE |
| M3 | Contracts, Loader, Scripts, Infra & Monitoring Audit & Remediation | `contracts/`, `loader/`, `scripts/`, `infra/`, `monitoring/`, `dynamo/templates/` (Defects DEF-CONTRACT-01 to DEF-MON-02) | Phase 0 Survey | DONE |
| M4 | Defect PoC Test Suite, Regression Safety & Final Report | Full-repository verification (all 145 tests pass [127 baseline + 18 PoC] + Gate 2 PASS) | M1, M2, M3 | DONE |

---

## Interface Contracts

### M1 (API/Web) ↔ M2 (Autotuner)
- `ClusterInventory` generation in `api/k8s_client.py:inventory_from_nodes` MUST emit integer `vram_gb` (fallback to GPU profile map when `nvidia.com/gpu.memory` is absent) to prevent `ValidationError` in `autotuner/sla_planner.py:ClusterInventory`.
- `DeployRequest` schema constraints in `api/main.py` MUST match `contracts/model_spec.schema.json` and `autotuner/sla_planner.py:DeployRequest`.

### M1 (API/Web) ↔ M3 (Contracts & Templates)
- All active endpoints in `api/main.py` and `api/data_routes.py` MUST be documented in `contracts/control_plane_openapi.yaml`.
- `/api/v1/models/{model_name}/status` enforces `verify_auth` dependency matching OpenAPI security spec.
- Jinja2 context passed by `api/dynamo_reconciler.py` to `dynamo/templates/dgd-*.yaml.j2` MUST handle optional fields (`kv_cache_transfer`) gracefully.

### M2 (Autotuner) ↔ M3 (Contracts & Templates)
- `DeploymentPlan` dictionary returned by `SLAPlanner.plan()` MUST validate against `contracts/model_spec.schema.json` and render cleanly through `dynamo/templates/`.
- Parallelism parameters ($TP \in \{1, 2, 4, 8\}$, $PP \in \{1, 2, 4\}$) MUST satisfy attention head divisibility ($num\_attention\_heads \% tp == 0$) and layer divisibility ($num\_hidden\_layers \% pp == 0$).

### M3 (Loader/Scripts/Infra) ↔ M1 (Reconciler/K8s Client)
- `loader/model_warmer.py` CLI parser MUST accept `--safe` and write Hugging Face Hub standard directory structure matching vLLM expectations.
- `infra/control-plane-rbac.yaml` MUST grant `batch/jobs/status` get/watch to allow `read_namespaced_job_status`.
- `infra/control-plane-deployment.yaml` MUST declare `serviceAccountName: control-plane`.

---

## Code Layout
- `control-plane/api/`: Python FastAPI backend.
- `control-plane/api/web/`: HTML5, CSS3, Vanilla ES6 JavaScript frontend.
- `control-plane/autotuner/`: Python analytical sizing engine.
- `contracts/`: JSON Schemas and OpenAPI specification.
- `dynamo/templates/`: Jinja2 Kubernetes manifest templates.
- `loader/`: Python model warmer and container definition.
- `scripts/`: Bash and Python cluster bootstrap and installer scripts.
- `infra/`: Kubernetes YAML manifests.
- `monitoring/`: Prometheus alert rules and KEDA templates.
- `tests/`: Pytest test suite (145 tests, 100% passing).
