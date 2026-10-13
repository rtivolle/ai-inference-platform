# API Reference

Base contract: `contracts/control_plane_openapi.yaml`. Interactive docs at `/docs`.

## Authentication

All `/api/v1/*` routes require `Depends(verify_auth)` (`control-plane/api/auth.py`):

- **Bearer**: `Authorization: Bearer <CONTROL_PLANE_API_TOKEN>`. In mock mode the
  default is `test-token`. Mismatched token → `401`.
- **Session**: admin login (`POST /auth/login`) sets an `HttpOnly; SameSite=Strict`
  cookie. Non-GET requests also require the `X-CSRF-Token` header and a matching
  `Origin` (`CONTROL_PLANE_PUBLIC_URL` or request base URL).
- `GET /auth/config` reports whether login and mock tokens are enabled.

`/health`, `/health/readiness`, `/`, `/assets/*`, `/auth/*` are unauthenticated
(except `/auth/session`, `/auth/logout` which need a session).

## Endpoints

### `GET /health` / `GET /health/readiness`

```json
{"status": "ok", "service": "control-plane-api", "mode": "mock"}
{"status": "ready", "k8s_connected": false, "litellm_endpoint": "http://...:4000"}
```

### `GET /api/v1/cluster/gpus`

Live inventory from GFD labels (`nvidia.com/gpu.product`, `nvidia.com/gpu.memory`)
minus `nvidia.com/gpu` requests of non-terminal pods. Mock returns 2 nodes /
8× H100-80GB minus allocations of mock DGDs.

```json
{"nodes_count": 2, "gpu_pools": [
  {"gpu_sku": "NVIDIA-H100-80GB", "vram_gb": 80.0, "total_count": 8,
   "available_count": 8, "rdma_supported": true}],
 "rdma_network_active": true}
```

### `POST /api/v1/models/deploy`

Body (`DeployRequest` in `control-plane/autotuner/sla_planner.py`):

| Field | Default | Notes |
|---|---|---|
| `model_id` | required, `^\S+$`, ≤256 chars | HF id, e.g. `meta-llama/Llama-3.3-70B-Instruct` |
| `deployment_name` | auto-sanitized | RFC 1123 `^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$` |
| `profile` | `balanced` | `low-latency` \| `high-throughput` \| `balanced` |
| `dtype` | `auto` | `auto` \| `bfloat16` \| `float16` \| `bf16` \| `fp16` |
| `quantization` | `null` | `fp8` \| `awq` \| `gptq` |
| `max_model_len` | `32768` | `> 0` |
| `gpu_memory_utilization` | `0.90` | `(0, 1]` |
| `target_concurrency` | `16` | `> 0` |

Success `200`:

```json
{"status": "provisioning", "deployment_name": "llama-3-3-70b-instruct",
 "model_id": "meta-llama/Llama-3.3-70B-Instruct", "plan": {...}}
```

Errors: `401` bad auth, `404` unknown HF model (`ModelNotFoundError`),
`409` insufficient GPUs/VRAM (`InsufficientCapacityError`), `400` other
scheduling failures, `422` Pydantic validation.

### `GET /api/v1/models` / `GET /api/v1/models/{name}/status`

List is sourced from live DGD CRs (mock falls back to the in-memory cache).
Status merges the CR (labels/annotations) with the cached plan and
`provisioning|running|failed|readiness_timeout` state.

### `DELETE /api/v1/models/{name}`

Returns immediately with `{"status": "deleting", ...}` and runs teardown in the
background: LiteLLM deregistration → drain → ScaledObject deletion → DGD
deletion. `404` if unknown.

### `GET /api/v1/metrics/usage?window_minutes=`

`window_minutes` must be `5`, `15`, or `60` (else `422`). Prometheus-backed
(`PROMETHEUS_URL`); see [Operations](operations.md).

### Auth routes

- `POST /auth/login {"username","password"}` — 5 failures / 5 min / IP → `429`;
  unconfigured → `503`. Rotates (revokes previous) session cookie.
- `GET /auth/session`, `POST /auth/logout`, `GET /auth/config`.
- Hash passwords with `python -m api.auth hash-password` (≥12 chars).

## LiteLLM payloads (for reference)

Register (`litellm_sync.py`):

```json
{"model_name": "<deployment>",
 "litellm_params": {"model": "openai/<hf-id>",
   "api_base": "http://<deployment>-frontend.dynamo-workloads.svc.cluster.local:8000/v1",
   "api_key": "internal-dynamo-key", "rpm": 1000, "timeout": 600},
 "model_info": {"id": "dgd-<deployment>", "mode": "chat", "supports_function_calling": true}}
```

Deregister: `POST <litellm>/model/delete {"id": "dgd-<deployment>"}`.
