# Configuration Reference

## Control plane

| Variable | Default | Purpose |
|---|---|---|
| `CONTROL_PLANE_MOCK` | `0` | `1` = explicit offline mock (forbidden in-cluster) |
| `CONTROL_PLANE_API_TOKEN` | `test-token` in mock, else required | Bearer for `/api/v1/*` + console |
| `CONTROL_PLANE_ADMIN_USERNAME` / `CONTROL_PLANE_ADMIN_PASSWORD_HASH` | unset | Enable admin cookie login |
| `CONTROL_PLANE_SESSION_DB` | `:memory:` | SQLite session store path |
| `CONTROL_PLANE_SESSION_SECONDS` | `3600` | Session TTL (300–86400) |
| `CONTROL_PLANE_COOKIE_SECURE` | secure unless mock | `0/false` to allow plain HTTP |
| `CONTROL_PLANE_PUBLIC_URL` | request base URL | CSRF `Origin` allowlist |
| `LITELLM_PROXY_URL` | `http://litellm-service.ai-gateway.svc.cluster.local:4000` | Gateway endpoint |
| `LITELLM_MASTER_KEY` | required (non-mock) | `Authorization: Bearer` for `/model/*` |
| `DYNAMO_RUNTIME_IMAGE` | `nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.5.1` | DGD worker/frontend image |
| `DYNAMO_RUNTIME_IMAGE_PULL_SECRET` | unset | Image pull secret name |
| `MODEL_WARMER_IMAGE` / `MODEL_WARMER_IMAGE_PULL_SECRET` | `ai-inference-model-warmer:latest` / unset | Warmer image override |
| `PROMETHEUS_URL` / `PROMETHEUS_BEARER_TOKEN` | unset | Usage metrics + drain queries |
| `HF_TOKEN` | unset | Private HF models (warmer + inspector) |
| `PORT` | `8000` | Container listen port |

## Gateway / LiteLLM (`litellm-deployment.yaml`)

`LITELLM_MASTER_KEY`, `DATABASE_URL`, `LITELLM_SALT_KEY` (from
`inference-runtime`), `STORE_MODEL_IN_DB=True`,
`PROXY_CONFIG_RELOAD_INTERVAL_SECONDS=10`. Bundled Postgres:
`POSTGRES_DB=litellm`, `POSTGRES_USER=litellm`, `POSTGRES_PASSWORD` +
`PGDATA` (10 Gi PVC). Config: `gateway/config.example.yaml`
(`store_model_in_db: true`, empty `model_list`, `least-busy`).

## Bootstrap (`bootstrap_inference_cluster.sh`)

`MODEL_CACHE_DIR` (default `/mnt/nvme/model-cache`), `K3S_VERSION`
(default `v1.31.1+k3s1`), `LITELLM_IMAGE`, `LITELLM_KUBE_CONTEXT`,
`PYTHON_BIN`.

## Timers (code constants)

Warmer wait 1800 s/job; EndpointSlice readiness 1800 s; delete drain 120 s;
LiteLLM retries 3× (`2^n` s); KEDA polling 15 s, cooldowns 120/180 s.
