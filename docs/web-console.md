# Web Console

Served by FastAPI at `/` (`control-plane/api/web/`, no build step). Static assets
under `/assets`. `Cache-Control: no-store` on HTML and API responses. Detailed
setup and operational notes: `control-plane/README.md`.

## Sign-in & First-Run Setup

- **Interactive terminal setup**: run `python -m api` (or `python -m api setup`)
  to configure the administrator account, browser origin, kubeconfig/context (or
  mock mode), LiteLLM gateway credentials, and Prometheus URL in `.inference/config.json` (`0600`).
- **Mock (`CONTROL_PLANE_MOCK=1`)**: enter `test-token` as Bearer token when no
  admin password is set. Token lives **only in memory**; reload or sign-out clears it.
- **Live with admin configured**: use username/password form → `HttpOnly; SameSite=Strict`
  cookie session + CSRF token.
- **Live API-token mode**: paste `CONTROL_PLANE_API_TOKEN`.
- A "Mock environment" banner is shown when `/health` reports `mode: mock`.

## Views

- **Model deployments (`#deployments`)**: search/filter list, per-model plan detail,
  deploy modal (verified profiles work offline; other ids query Hugging Face),
  and delete with confirmation. Auto-refreshes every 10 s while the tab is visible.
- **GPU inventory (`#gpus`)**: pool SKU, per-GPU VRAM, total vs available GPUs,
  and RDMA availability badge.
- **Live usage (`#metrics`)**: Prometheus telemetry over 5, 15, or 60-minute windows
  (request rate, output token rate, running/waiting requests, TTFT p95, GPU utilization,
  GPU memory occupancy, and peak KV-cache usage) with gap-preserving SVG charts.
- **Kubernetes (`#kubernetes`)**: cluster node health cards (readiness, scheduling,
  GPU count/SKU, CPU/memory, pressure conditions), workload component replica
  scaling (with SKU capacity and autoscaler checks), container log viewer, and
  UID-guarded pod eviction/replacement.

## API mapping

| Action | Request |
|---|---|
| Auth config / login / session / logout | `GET /auth/config`, `POST /auth/login`, `GET /auth/session`, `POST /auth/logout` |
| List / status | `GET /api/v1/models`, `GET /api/v1/models/{name}/status` |
| GPU inventory | `GET /api/v1/cluster/gpus` |
| Deploy / delete | `POST /api/v1/models/deploy`, `DELETE /api/v1/models/{name}` |
| Live usage metrics | `GET /api/v1/metrics/usage?window_minutes=5\|15\|60` |
| Cluster nodes | `GET /api/v1/cluster/nodes` |
| Workload pods & components | `GET /api/v1/models/{name}/workload` |
| Scale component | `POST /api/v1/models/{name}/components/{component}/scale` |
| Pod logs / replace pod | `GET /api/v1/models/{name}/pods/{pod}/logs`, `POST /api/v1/models/{name}/pods/{pod}/evict` |

Tests: `tests/test_web_console.py`, `tests/test_console_setup.py`, `tests/test_auth.py`,
`tests/test_kubernetes_controls.py`, `tests/test_usage_metrics.py`.
