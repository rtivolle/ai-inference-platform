# Web Console

Served by FastAPI at `/` (`control-plane/api/web/`, no build step). Static assets
under `/assets`. `Cache-Control: no-store` on the HTML.

## Sign-in

- Mock (`CONTROL_PLANE_MOCK=1`): enter `test-token` as Bearer token. Token lives
  **only in memory**; reload or sign-out clears it.
- Live with admin configured: use username/password form → cookie session + CSRF.
- Live API-token mode: paste `CONTROL_PLANE_API_TOKEN`.
- A "Mock environment" banner is shown when the backend reports `mode: mock`.

## Views

- **Model deployments**: search/filter list, per-model plan detail, delete with
  confirmation. Auto-refreshes every 10 s while the tab is visible.
- **GPUs**: pool SKU, per-GPU VRAM, total vs available, RDMA flag.
- **Deploy form**: model id (verified profiles work offline; other ids hit HF),
  profile, dtype/quantization, context length, concurrency, memory utilization,
  advanced options. Errors from the API (409 capacity, 404 unknown model) are
  shown inline.

## API mapping

| Action | Request |
|---|---|
| List | `GET /api/v1/models` |
| Status | `GET /api/v1/models/{name}/status` |
| Inventory | `GET /api/v1/cluster/gpus` |
| Deploy | `POST /api/v1/models/deploy` |
| Delete | `DELETE /api/v1/models/{name}` |
| Usage | `GET /api/v1/metrics/usage?window_minutes=` |

Tests: `tests/test_web_console.py` (routing, assets, form wiring).
