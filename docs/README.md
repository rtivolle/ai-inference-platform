# AI Inference Platform — Documentation

Central index for all guides. Start with [Quickstart](quickstart.md), then read
[Architecture](architecture.md) and the [Follow-Up Plan & Upstream Integration Roadmap](followup-plan.md).

## Guides

| Guide | Contents |
|---|---|
| [Quickstart](quickstart.md) | Local mock run (venv + Docker), first deploy via console and API |
| [Follow-Up Plan & Integrations](followup-plan.md) | Post-audit remediation tasks, verified 2025–2026 upstream integrations (`hf_xet`, Run:ai Model Streamer, LMCache + NIXL, Dynamo Planner/GAIE, LiteLLM + Redis), and Waves 4–5 execution plan |
| [Architecture](architecture.md) | Components, namespaces, request lifecycle, mermaid DAG |
| [API Reference](api-reference.md) | Endpoints, auth, K8s operational routes, data/training routes, status codes |
| [Web Console](web-console.md) | Using the `/` SPA: login, deployments, GPUs, live Prometheus metrics, Kubernetes controls |
| [Auto-Tuner & VRAM Sizing](autotuner.md) | HF inspection, VRAM formulas, SLA planner, profiles, capacity errors |
| [Dynamo Deployments](dynamo-deployments.md) | Aggregated vs disaggregated DGD, NIXL, templates, warmer Jobs |
| [LiteLLM Gateway](gateway.md) | Installer, Postgres, DB-backed models, sync lifecycle |
| [Infrastructure & Bootstrap](infrastructure.md) | `bootstrap_inference_cluster.sh`, GPU/RDMA/NVMe, Ingress + Cloudflare |
| [Multi-Cluster Releases](multi-cluster.md) | Immutable digests, inventory, `deploy_clusters.py`, rollback |
| [Operations & Autoscaling](operations.md) | KEDA ScaledObjects, Prometheus rules, drains, upgrades |
| [Data & Fine-Tuning](data-and-fine-tuning.md) | Opt-in capture, review, versioned datasets, LoRA Jobs, evaluation, pinned promotion |
| [Security](security.md) | Bearer/session auth, Secrets, RBAC, Cloudflare Access |
| [Configuration Reference](configuration.md) | All environment variables and defaults across control plane, gateway, and training |
| [Troubleshooting](troubleshooting.md) | Symptom → cause → fix for common failures |
| [Development & CI](development.md) | Dev setup, 145-test pytest suite, Ruff/Mypy, kubeconform, contracts |
| [Architecture Audit (FR)](architecture-audit.md) | Initial deep-dive audit on NVIDIA Dynamo v1beta1, NIXL RDMA, KEDA/DGDSA, and LiteLLM |
| [Forensic Defect Catalog](audit-defect-catalog.md) | 43-defect full-repository audit specifications and unified remediation diffs |

## Source-of-truth files

- `README.md` — project overview and console intro
- `AGENTS.md` — contributor, style, testing, and security guidelines
- `agent-plan.md` — multi-agent responsibilities, contracts, R5/Wave 4–5 roadmap
- `docs/followup-plan.md` — post-audit roadmap and verified upstream integration upgrades
- `contracts/control_plane_openapi.yaml` — generated REST contract (`python -m scripts.export_data_contracts`)
- `contracts/model_spec.schema.json`, `contracts/dynamo_crd.schema.json`, `contracts/data_workflow.schema.json` — JSON Schemas
- `deploy/README.md` — multi-cluster pipeline; `gateway/README.md` — LiteLLM installer; `control-plane/README.md` — console & auth setup
