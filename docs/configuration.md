# Configuration Reference

## Control plane (`control-plane/api/`)

| Variable | Default | Purpose |
|---|---|---|
| `CONTROL_PLANE_MOCK` | `0` | `1` = explicit offline mock (strictly forbidden in-cluster) |
| `CONTROL_PLANE_API_TOKEN` | `test-token` in mock, else required | Bearer token for `/api/v1/*` and console |
| `CONTROL_PLANE_ADMIN_USERNAME` / `CONTROL_PLANE_ADMIN_PASSWORD_HASH` | unset | Enable administrator cookie sign-in (Argon2id hash) |
| `CONTROL_PLANE_SESSION_DB` | `:memory:` | SQLite session & login-attempt store path (WAL + RLock) |
| `CONTROL_PLANE_SESSION_SECONDS` | `3600` | Session TTL in seconds (clamped to `300–86400`) |
| `CONTROL_PLANE_COOKIE_SECURE` | secure unless mock | `0`/`false` to allow plain HTTP cookies on local loopback |
| `CONTROL_PLANE_PUBLIC_URL` | request base URL | Expected browser `Origin` for CSRF verification |
| `CONTROL_PLANE_CONFIG_FILE` | `.inference/config.json` | Protected `0600` local config file used by `python -m api` |
| `CONTROL_PLANE_KUBE_CONTEXT` / `KUBECONFIG` | current context / default | Explicit kubeconfig path and context for out-of-cluster runs |
| `CONTROL_PLANE_GATEWAY_DRAIN_DELAY_SECONDS` | `15` | Propagation wait after LiteLLM deregistration before draining |
| `LITELLM_PROXY_URL` | `http://litellm-service.ai-gateway.svc.cluster.local:4000` | LiteLLM Proxy management endpoint |
| `LITELLM_MASTER_KEY` | required (non-mock) | `Authorization: Bearer` key for `/model/new` and `/model/delete` |
| `DYNAMO_RUNTIME_IMAGE` | `nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.5.1` | DGD frontend and worker container image |
| `DYNAMO_RUNTIME_IMAGE_PULL_SECRET` | unset | Image pull secret for Dynamo runtime containers |
| `MODEL_WARMER_IMAGE` / `MODEL_WARMER_IMAGE_PULL_SECRET` | `ai-inference-model-warmer:latest` / unset | Image and pull secret for per-node NVMe warmer Jobs |
| `PROMETHEUS_URL` / `PROMETHEUS_BEARER_TOKEN` | unset | Prometheus HTTP API endpoint and optional bearer token |
| `HF_TOKEN` | unset | Hugging Face token for gated/private models |
| `PORT` | `8000` | Uvicorn container listen port |

## Data capture & LoRA fine-tuning (`DATA_WORKFLOW_ENABLED=1`)

| Variable | Default | Purpose |
|---|---|---|
| `DATA_WORKFLOW_ENABLED` | `0` | `1` enables `/api/v1/data/*` and `/api/v1/training/*` |
| `DATA_DB_PATH` | required when enabled | Persistent SQLite path for events, reviews, datasets, and runs |
| `DATA_ARTIFACT_ROOT` | `/var/lib/inference-data/artifacts` | Local fallback directory for dataset artifacts |
| `DATA_S3_BUCKET` / `DATA_S3_ENDPOINT` | unset | S3 bucket (required for live training) and optional endpoint URL |
| `DATA_EVENT_RETENTION_DAYS` | `30` | Source event retention window (`1–365` days) |
| `DATA_CAPTURE_TOKEN` | required for capture | Dedicated bearer token for `POST /api/v1/data/events` |
| `DATA_CAPTURE_ENABLED` / `DATA_CAPTURE_CONTENT_ENABLED` | `0` / `0` | Enable LiteLLM callback (`gateway/data_capture.py`) and consented message capture |
| `DATA_CAPTURE_URL` | unset | Control plane ingestion URL (`/api/v1/data/events`) |
| `TRAINING_IMAGE` / `TRAINING_IMAGE_PULL_SECRET` | required (live) / unset | Immutable digest (`repo@sha256:...`) and pull secret for trainer Jobs |
| `TRAINING_HF_OWNER` | required (live) | Required Hugging Face namespace prefix for exported private models |

## Gateway / LiteLLM (`gateway/litellm-deployment.yaml`)

`LITELLM_MASTER_KEY`, `DATABASE_URL`, `LITELLM_SALT_KEY` (from
`inference-runtime`), `STORE_MODEL_IN_DB=True`,
`PROXY_CONFIG_RELOAD_INTERVAL_SECONDS=10`. Bundled Postgres:
`POSTGRES_DB=litellm`, `POSTGRES_USER=litellm`, `POSTGRES_PASSWORD` +
`PGDATA` (10 Gi PVC). Config template: `gateway/config.example.yaml`
(`store_model_in_db: true`, empty `model_list`, `least-busy`).

## Bootstrap (`scripts/bootstrap_inference_cluster.sh`)

`MODEL_CACHE_DIR` (default `/mnt/nvme/model-cache`), `K3S_VERSION`
(default `v1.31.1+k3s1`), `LITELLM_IMAGE`, `LITELLM_KUBE_CONTEXT`,
`PYTHON_BIN`.

## Timers (code constants)

Warmer wait 1800 s/job; EndpointSlice readiness 1800 s; delete drain 120 s;
LiteLLM retries 3× (`2^n` s); KEDA polling 15 s, cooldowns 120/180 s.
