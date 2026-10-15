# API Reference

Base contract: `contracts/control_plane_openapi.yaml` (regenerated via
`python -m scripts.export_data_contracts`). Interactive docs at `/docs`.

## Authentication

All `/api/v1/*` routes (except `POST /api/v1/data/events`, which uses
`DATA_CAPTURE_TOKEN`) require `Depends(verify_auth)` (`control-plane/api/auth.py`):

- **Bearer**: `Authorization: Bearer <CONTROL_PLANE_API_TOKEN>`. In mock mode the
  default is `test-token`. Mismatched token → `401`.
- **Session**: admin login (`POST /auth/login`) sets an `HttpOnly; SameSite=Strict`
  cookie (`Secure` unless mock or `CONTROL_PLANE_COOKIE_SECURE=0`). Non-GET
  requests also require the `X-CSRF-Token` header and a matching `Origin`
  (`CONTROL_PLANE_PUBLIC_URL` or request base URL).
- `GET /auth/config` reports whether login and mock tokens are enabled.

`/health`, `/health/readiness`, `/`, `/assets/*`, `/auth/config`, and `/auth/login`
are unauthenticated (`/auth/session` and `/auth/logout` require an active session).

## Core Endpoints

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

Body (`DeployRequest` in `control-plane/autotuner/sla_planner.py` and `contracts/model_spec.schema.json`):

| Field | Default | Notes |
|---|---|---|
| `model_id` | required, `^\S+$`, ≤256 chars | HF id, e.g. `meta-llama/Llama-3.3-70B-Instruct` |
| `model_revision` | `null` | Optional 40-hex commit SHA (`^[a-f0-9]{40}$`) |
| `deployment_name` | auto-sanitized | RFC 1123 `^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$` |
| `profile` | `balanced` | `low-latency` \| `high-throughput` \| `balanced` |
| `dtype` | `auto` | `auto` \| `bfloat16` \| `float16` \| `bf16` \| `fp16` |
| `quantization` | `null` | `fp8` \| `awq` \| `gptq` |
| `max_model_len` | `32768` | `512` to `131072` |
| `gpu_memory_utilization` | `0.90` | `0.5` to `0.98` |
| `target_concurrency` | `16` | `>= 1` |

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
`provisioning|running|failed|readiness_timeout|deleting|delete_failed` state.

### `DELETE /api/v1/models/{name}`

Returns immediately with `{"status": "deleting", ...}` and runs teardown in the
background: LiteLLM deregistration → gateway propagation delay → dynamic drain →
ScaledObject deletion → DGD deletion. `404` if unknown.

### `GET /api/v1/metrics/usage?window_minutes=`

`window_minutes` must be `5`, `15`, or `60` (else `422`). Prometheus-backed
(`PROMETHEUS_URL`); see [Operations](operations.md).

## Kubernetes Operational Controls (`control-plane/api/k8s_controls.py`)

- `GET /api/v1/cluster/nodes` — Lists cluster nodes with readiness, schedulable status, GPU count/SKU, CPU/memory capacity, and pressure conditions.
- `GET /api/v1/models/{model_name}/workload` — Returns pods and components (with `DynamoGraphDeploymentScalingAdapter` state, replica counts, `resource_version`, and `can_scale` status) for a managed deployment.
- `POST /api/v1/models/{model_name}/components/{component_name}/scale` — Body `{"replicas": 1..64, "resource_version": "..."}`. Verifies GPU capacity strictly within the matching GPU SKU pool (`409` on insufficient capacity, autoscaler ownership, or stale `resource_version`).
- `GET /api/v1/models/{model_name}/pods/{pod_name}/logs?container=&tail_lines=100` — Reads up to 500 lines / 128 KiB from a managed pod container (`404` if pod has no containers).
- `POST /api/v1/models/{model_name}/pods/{pod_name}/evict` — Body `{"uid": "..."}`. Evicts a pod via `policy/v1` Eviction with UID precondition (`409` on UID mismatch or PodDisruptionBudget refusal).

## Data Capture & Fine-Tuning Endpoints (`DATA_WORKFLOW_ENABLED=1`)

See [Data & Fine-Tuning](data-and-fine-tuning.md) for full schemas:

- `POST /api/v1/data/events` (`202`, requires `Authorization: Bearer <DATA_CAPTURE_TOKEN>`) — Ingests a gateway capture event (≤128 KiB).
- `POST /api/v1/data/examples` (`201`), `GET /api/v1/data/events`, `GET /api/v1/data/events/{request_id}`, `POST /api/v1/data/events/{request_id}/review` — Admin import, listing, inspection, and approval/rejection of captured examples.
- `POST /api/v1/data/datasets` (`201`), `GET /api/v1/data/datasets`, `GET /api/v1/data/datasets/{dataset_id}`, `GET /api/v1/data/datasets/{dataset_id}/export?split=train|validation|test` — Builds and exports immutable, checksummed JSONL dataset splits.
- `POST /api/v1/training/runs` (`202`), `GET /api/v1/training/runs`, `GET /api/v1/training/runs/{run_id}`, `POST /api/v1/training/runs/{run_id}/cancel`, `GET /api/v1/training/runs/{run_id}/logs`, `POST /api/v1/training/runs/{run_id}/promote` (`202`) — Launches single-GPU LoRA SFT Jobs, inspects status/logs, enforces held-out loss gates, and promotes evaluated models by immutable commit SHA.
