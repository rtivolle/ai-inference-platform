# AI Inference Platform — Documentation

Central index for all guides. Start with [Quickstart](quickstart.md), then read
[Architecture](architecture.md).

## Guides

| Guide | Contents |
|---|---|
| [Quickstart](quickstart.md) | Local mock run (venv + Docker), first deploy via console and API |
| [Architecture](architecture.md) | Components, namespaces, request lifecycle, mermaid DAG |
| [API Reference](api-reference.md) | Endpoints, auth, request/response schemas, status codes |
| [Web Console](web-console.md) | Using the `/` SPA: login, inventory, deploy form, status |
| [Auto-Tuner & VRAM Sizing](autotuner.md) | HF inspection, VRAM formulas, SLA planner, profiles, capacity errors |
| [Dynamo Deployments](dynamo-deployments.md) | Aggregated vs disaggregated DGD, NIXL, templates, warmer Jobs |
| [LiteLLM Gateway](gateway.md) | Installer, Postgres, DB-backed models, sync lifecycle |
| [Infrastructure & Bootstrap](infrastructure.md) | `bootstrap_inference_cluster.sh`, GPU/RDMA/NVMe, Ingress + Cloudflare |
| [Multi-Cluster Releases](multi-cluster.md) | Immutable digests, inventory, `deploy_clusters.py`, rollback |
| [Operations & Autoscaling](operations.md) | KEDA ScaledObjects, Prometheus rules, drains, upgrades |
| [Data & Fine-Tuning](data-and-fine-tuning.md) | Opt-in capture, review, versioned datasets, LoRA Jobs, evaluation, pinned promotion |
| [Security](security.md) | Bearer/session auth, Secrets, RBAC, Cloudflare Access |
| [Configuration Reference](configuration.md) | All environment variables and defaults |
| [Troubleshooting](troubleshooting.md) | Symptom → cause → fix for common failures |
| [Development & CI](development.md) | Dev setup, pytest, Ruff/Mypy, kubeconform, contracts |

## Source-of-truth files

- `README.md` — project overview and console intro
- `agent-plan.md` — agent responsibilities, contracts, R5/Wave 4–5 roadmap
- `docs.md` — architectural audit (Dynamo v1beta1, NIXL, KEDA/DGDSA)
- `contracts/control_plane_openapi.yaml` — REST contract
- `contracts/model_spec.schema.json`, `contracts/dynamo_crd.schema.json` — plans and CRDs
- `deploy/README.md` — multi-cluster pipeline; `gateway/README.md` — LiteLLM installer
