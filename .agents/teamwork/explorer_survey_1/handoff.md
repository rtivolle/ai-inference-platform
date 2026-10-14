# Handoff Report — Explorer Survey 1 (`control-plane/api/` & `control-plane/api/web/`)

**Author**: Explorer Survey 1  
**Target Recipient**: Orchestrator (`fbda79c3-ec32-408a-a403-9cabb555b0a1`)  
**Scope**: `control-plane/api/` and `control-plane/api/web/`  
**Date**: 2026-10-13  

---

## 1. Observation

1. **Test Failure in `tests/test_kubernetes_controls.py`**:
   - Running `.venv/bin/pytest -v tests/test_kubernetes_controls.py` exits with code 1:
     ```
     FAILED tests/test_kubernetes_controls.py::test_failed_drain_keeps_workload_and_exposes_failure
     control-plane/api/dynamo_reconciler.py:235: TimeoutError: Active requests did not drain before the deadline; the Kubernetes workload was retained.
     ```
   - In `control-plane/api/dynamo_reconciler.py:224-236`:
     ```python
     drain_elapsed = 0
     poll_step = 2
     while drain_elapsed < self.drain_timeout_seconds:
         running = await self.k8s.get_running_requests_count(deployment_name)
         if running == 0:
             logger.info(f"All active requests drained for {deployment_name}.")
             break
         await asyncio.sleep(poll_step)
         drain_elapsed += poll_step
     else:
         if not self.k8s.mock_mode:
             raise TimeoutError("Active requests did not drain before the deadline; the Kubernetes workload was retained.")
     ```
   - In `tests/conftest.py:56`, the `reconciler` fixture is instantiated with `drain_timeout_seconds=0`.
   - In `test_kubernetes_controls.py:102-107`, `mock_k8s.get_running_requests_count` is mocked to raise `RuntimeError("Monitoring unavailable")`. However, because `drain_elapsed < self.drain_timeout_seconds` is `0 < 0` (False), the loop body never runs, and the `else:` branch raises `TimeoutError` without calling `get_running_requests_count`.

2. **SessionStore Concurrency without Locks in `control-plane/api/auth.py`**:
   - `control-plane/api/auth.py:32-37`:
     ```python
     class SessionStore:
         def __init__(self, path: str = ":memory:"):
             self.db = sqlite3.connect(path, check_same_thread=False)
             self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, username TEXT, csrf TEXT, expires REAL)")
             self.db.execute("CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, failures INTEGER, started REAL)")
             self.db.commit()
     ```
   - Methods `create`, `get`, `revoke`, `check_rate_limit`, `failed_login`, `reset_attempts` execute on `self.db` concurrently without `threading.Lock()` or `asyncio.Lock()` and without WAL PRAGMA. Contrast with `control-plane/api/data_store.py:80-84` which uses `self.lock = threading.RLock()` and `PRAGMA journal_mode=WAL`.

3. **Rate Limiting Keyed on Ingress/Proxy Host in `control-plane/api/auth.py`**:
   - `control-plane/api/auth.py:155-158`:
     ```python
     source = request.client.host if request.client else "unknown"
     key = hashlib.sha256(source.encode()).hexdigest()
     async with auth._verification_slots:
         auth.store.check_rate_limit(key)
     ```
   - In live deployments behind Ingress/Cloudflare, `request.client.host` is the reverse proxy or gateway pod IP. All users share the same key. After 5 failed attempts, all administrator logins cluster-wide are blocked with HTTP 429 for 300 seconds.

4. **LiteLLM 404 Teardown Block in `control-plane/api/litellm_sync.py` & `dynamo_reconciler.py`**:
   - `control-plane/api/litellm_sync.py:126-137`:
     ```python
     async with httpx.AsyncClient(timeout=10.0) as client:
         resp = await client.post(url, json=payload, headers=headers)
         resp.raise_for_status()
     ```
   - LiteLLM `/model/delete` returning 404 raises `httpx.HTTPStatusError`, causing `LiteLLMSync.deregister_model` to raise `RuntimeError`.
   - `control-plane/api/dynamo_reconciler.py:220`:
     ```python
     await self.litellm.deregister_model(deployment_name)
     ```
   - Because `deregister_model` raises, execution terminates before K8s CR deletion, permanently leaving the model in `delete_failed` state.

5. **Pydantic Validation Crash on Missing VRAM Label in `control-plane/api/k8s_client.py`**:
   - `control-plane/api/k8s_client.py:393-397`:
     ```python
     memory_label = labels.get("nvidia.com/gpu.memory")
     try:
         memory = round(float(memory_label) / 1024, 1) if memory_label else None
     except ValueError:
         memory = None
     ```
   - `control-plane/autotuner/sla_planner.py:53-55`:
     ```python
     class GpuPool(BaseModel):
         gpu_sku: str = "NVIDIA-H100-80GB"
         vram_gb: float = 80.0
     ```
   - Executing `.venv/bin/python -c 'from autotuner.sla_planner import ClusterInventory; ClusterInventory(**{"nodes_count": 1, "gpu_pools": [{"gpu_sku": "H100", "vram_gb": None, "total_count": 1, "available_count": 1, "rdma_supported": False}]})'` produces verbatim:
     `pydantic_core._pydantic_core.ValidationError: 1 validation error for ClusterInventory: gpu_pools.0.vram_gb: Input should be a valid number`.
   - In `control-plane/api/main.py:158`, `inventory = ClusterInventory(**inventory_dict)` crashes with this error on any cluster missing the optional label.

6. **Contract Drift in `contracts/control_plane_openapi.yaml`**:
   - Inspected `contracts/control_plane_openapi.yaml`: Only 7 endpoints are documented (`deploy`, `models/{name}`, `models`, `models/{name}/status`, `cluster/gpus`, `health`, `health/readiness`).
   - Undocumented in OpenAPI: `/auth/*` (4 endpoints), `/api/v1/cluster/nodes`, `/api/v1/metrics/usage`, `/api/v1/models/{name}/workload`, `/api/v1/models/{name}/components/{comp}/scale`, `/api/v1/models/{name}/pods/{pod}/logs`, `/api/v1/models/{name}/pods/{pod}/evict`, `/api/v1/data/*` (7 endpoints).

---

## 2. Logic Chain

1. From Observation 1:
   - `self.drain_timeout_seconds` is compared as `drain_elapsed < self.drain_timeout_seconds`.
   - At iteration 0 with `drain_timeout_seconds=0`, `0 < 0` is false.
   - The loop condition terminates immediately without checking active requests.
   - The `while...else` construct enters `else:` and raises `TimeoutError` when `not self.k8s.mock_mode`.
   - Therefore, a zero or small drain timeout makes graceful teardown impossible and masks monitoring errors, causing `test_failed_drain_keeps_workload_and_exposes_failure` to fail.

2. From Observation 2:
   - `SessionStore` shares a single SQLite connection across multiple concurrent FastAPI threads/tasks.
   - SQLite does not permit concurrent write/commit statements on a shared connection handle without synchronization.
   - Under concurrent authentication traffic, this results in database locking errors and unhandled 500 exceptions.

3. From Observation 3:
   - In Kubernetes, reverse proxies terminate client TLS and forward requests to the pod, masking the true client IP in `request.client.host`.
   - Because `auth.py:156` hashes `source = request.client.host`, all clients share one attempt counter.
   - A single actor exhausting 5 attempts locks out all users, including legitimate cluster admins.

4. From Observation 4:
   - Idempotency requires that attempting to delete an already-deleted or absent entity succeeds without error.
   - LiteLLM returns 404 if the model is absent.
   - Raising an unhandled `RuntimeError` on 404 blocks downstream Kubernetes deletion, deadlocking workload cleanup.

5. From Observation 5:
   - `k8s_client.py` sets `vram_gb: None` when `nvidia.com/gpu.memory` is absent.
   - `ClusterInventory` requires `vram_gb: float`.
   - Pydantic validation fails immediately in `main.py:158`, causing `POST /api/v1/models/deploy` to return HTTP 400.

---

## 3. Caveats

- **Out of Scope Subsystems**: `control-plane/autotuner/`, `loader/`, `infra/`, `monitoring/`, and `scripts/` were reviewed only at their integration interfaces with `control-plane/api/`. Comprehensive internal audits of those components belong to parallel survey agents.
- **Assumptions on Production Topology**: Analysis assumes standard Kubernetes ingress architecture where a reverse proxy or ingress controller forwards traffic to the control plane service.

---

## 4. Conclusion

The control plane API and web console are structurally well-engineered, with comprehensive test fixtures and clean separation of concerns. However, there are 16 discrete defects requiring remediation:
1. **Critical/High priority**:
   - Fix drain loop in `dynamo_reconciler.py` to poll active requests before checking deadline expiry (resolving test failure in `test_kubernetes_controls.py`).
   - Add mutex synchronization and WAL mode to `SessionStore` in `auth.py`.
   - Composite client IP and username in `auth.py` rate limiting to prevent global admin lockout.
   - Make LiteLLM deregistration idempotent on HTTP 404 in `litellm_sync.py`.
2. **Medium priority**:
   - Provide VRAM SKU fallback in `k8s_client.py` and loosen `GpuPool.vram_gb` in `sla_planner.py`.
   - Constrain workload scaling to matching GPU SKUs in `k8s_controls.py`.
   - Guard against empty container lists in `k8s_controls.py:pod_logs`.
   - Track background tasks in `dynamo_reconciler.py` to prevent garbage collection.
3. **Low priority**:
   - Re-sync `contracts/control_plane_openapi.yaml` with all 11 missing routes.

The complete survey report has been saved to `/home/rtivolle/ai-inference-platform/.agents/teamwork/explorer_survey_1/report.md`.

---

## 5. Verification Method

1. **Verify Baseline Test Suite**:
   ```bash
   .venv/bin/pytest -v tests/
   ```
   (Currently 100 passing, 1 failing on `test_failed_drain_keeps_workload_and_exposes_failure`).

2. **Verify Failing Drain Defect (D-API-01)**:
   ```bash
   .venv/bin/pytest -v tests/test_kubernetes_controls.py -k test_failed_drain_keeps_workload_and_exposes_failure
   ```
   Fails with `TimeoutError` at line 235 instead of raising `RuntimeError("Monitoring unavailable")`.

3. **Verify VRAM Label Validation Mismatch (D-API-10)**:
   ```bash
   .venv/bin/python -c 'from autotuner.sla_planner import ClusterInventory; ClusterInventory(**{"nodes_count": 1, "gpu_pools": [{"gpu_sku": "H100", "vram_gb": None, "total_count": 1, "available_count": 1, "rdma_supported": False}]})'
   ```
   Raises `pydantic_core._pydantic_core.ValidationError`.

4. **Verify Web Console Delivery**:
   ```bash
   .venv/bin/pytest -v tests/test_web_console.py
   ```
   All 12 web console tests pass.
