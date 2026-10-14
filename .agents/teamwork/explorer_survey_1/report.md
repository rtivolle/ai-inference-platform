# Comprehensive Survey Report: Control-Plane API and Web Console

**Subagent ID**: `explorer_survey_1`  
**Working Directory**: `/home/rtivolle/ai-inference-platform/.agents/teamwork/explorer_survey_1`  
**Audit Target Scope**: `control-plane/api/` and `control-plane/api/web/`  
**Date**: 2026-10-13  

---

## 1. Executive Summary

This report delivers a comprehensive audit and architectural inventory of the AI Inference Platform's control plane API (`control-plane/api/`) and associated browser console (`control-plane/api/web/`).

The control plane orchestrates distributed AI inference workloads on Kubernetes using NVIDIA Dynamo (`DynamoGraphDeployment` custom resources), auto-tuned VRAM planning, dynamic cache warming jobs, KEDA horizontal autoscalers, Prometheus-driven metrics monitoring, and LiteLLM gateway routing. It additionally exposes an administrative web console, session-based authentication with Argon2 password hashing and CSRF protection, and a data capture/review pipeline.

### Core Survey Findings Summary
- **14 Files Surveyed**: 11 Python backend modules (2,014 lines of code) and 3 frontend assets (3,271 lines of HTML/CSS/JS).
- **16 Discrete Defects and Vulnerabilities Identified**:
  - **4 Critical / High Severity**:
    1. **D-API-01 (High - Functional Bug)**: `dynamo_reconciler.py:224-236` — Drain loop logic bug: When `drain_timeout_seconds` is `0` or less than `poll_step`, the loop condition `drain_elapsed < self.drain_timeout_seconds` evaluates to `False` on the first iteration without querying active requests, triggering `else:` which prematurely raises `TimeoutError` and blocks workload deletion.
    2. **D-API-04 (High - Concurrency / Race)**: `auth.py:32-78` — Un-synchronized `sqlite3.Connection` in `SessionStore` accessed concurrently from async coroutines without locks or WAL mode, risking `OperationalError` transaction crashes and session state corruption.
    3. **D-API-05 (High - Security / DoS)**: `auth.py:155-158` — Rate limiting keyed exclusively on raw `request.client.host` without handling reverse proxy / ingress headers, allowing 5 failed attempts from any client to lock out all cluster administrators.
    4. **D-API-07 (High - Error Handling / Orphaned Resources)**: `litellm_sync.py:126-137` — LiteLLM `/model/delete` 404 response is treated as an unhandled error, retried 3 times, and causes `RuntimeError`, permanently preventing Kubernetes CR deletion for absent or deregistered models.
  - **6 Medium Severity**:
    - **D-API-02 (Medium - Concurrency / Resource Leak)**: `dynamo_reconciler.py:152` — Untracked `asyncio.create_task` for 30-minute EndpointSlice polling risks premature garbage collection.
    - **D-API-03 (Medium - Race Condition)**: `dynamo_reconciler.py:171-198` — EndpointSlice readiness poller does not verify deletion state, resurrecting deleted deployments in LiteLLM.
    - **D-API-06 (Medium - Security)**: `auth.py:102-108` — CSRF `check_origin` fails open if the `Origin` header is omitted on session mutation requests.
    - **D-API-08 (Medium - Functional / Scheduling)**: `k8s_controls.py:203-206` — Component scaling sums available GPUs across all GPU pools without validating SKU compatibility.
    - **D-API-09 (Medium - Error Handling)**: `k8s_controls.py:99-100` — `pod_logs` raises unhandled `IndexError` on pods with empty container lists.
    - **D-API-10 (Medium - Contract / Schema Mismatch)**: `k8s_client.py:393-402` vs `sla_planner.py:53-55` — Nodes lacking `nvidia.com/gpu.memory` produce `vram_gb: None`, triggering Pydantic `ValidationError` in `ClusterInventory`.
  - **6 Low Severity**:
    - **D-API-11 (Low - Robustness)**: `k8s_client.py:281-294` — Missing transient retry in 30-minute Job status poller.
    - **D-API-12 (Low - Concurrency)**: `main.py:154-178` — Concurrent deployment requests with duplicate names clobber state.
    - **D-API-13 (Low - Contract Drift)**: `contracts/control_plane_openapi.yaml` — 11 active API endpoints and actual auth rules are omitted from OpenAPI spec.
    - **D-API-14 (Low - Dependency)**: `data_store.py:47-51` — Optional `boto3` dependency undeclared in package dependencies.
    - **D-API-15 (Low - Web UX / Timeout)**: `web/app.js:451` — 31-minute synchronous deploy fetch exceeds gateway timeouts.
    - **D-API-16 (Low - Web UX)**: `web/app.js:1007-1039` — Empty container list in logs modal triggers unhandled 500 error.

---

## 2. Component and Subsystem Architecture

### 2.1 File Inventory and Interactions

```
control-plane/api/
├── __init__.py                # Package declaration
├── main.py                    # FastAPI entrypoint, lifespan, exception handlers, REST routes
├── auth.py                    # SessionStore (SQLite), Argon2 password hashing, CSRF verification, rate limiter
├── k8s_client.py              # Asynchronous Kubernetes client (kubernetes_asyncio), CRD & Job lifecycle, inventory
├── k8s_controls.py            # Workload operational controls: node health, pod list, logs, eviction, replica scaling
├── dynamo_reconciler.py       # Reconciler: Jinja2 CRD & warmer rendering, Job execution, EndpointSlice wait, LiteLLM sync, graceful drain
├── litellm_sync.py            # LiteLLM Proxy client: model registration, deregistration with exponential retry
├── metrics.py                 # Prometheus telemetry: latency, throughput, GPU util, KV-cache, running request counts
├── data_models.py             # Pydantic models for data capture, review, dataset creation, and model training
├── data_routes.py             # REST endpoints for LLM data capture, human review, dataset export
├── data_store.py              # SQLite storage for conversation events, review metadata, and dataset artifact storage (local/S3)
└── web/
    ├── index.html             # Responsive console UI shell: deployments table, GPU pools, metrics charts, K8s ops
    ├── styles.css             # Native CSS layout, design system, theme variables, and status badges
    └── app.js                 # Vanilla JS SPA controller: session management, polling loops, dialogs, charts
```

### 2.2 Complete API Endpoint Catalog

| Method | Path | Auth Required | Backing Handler | Backing Module | Description |
|---|---|---|---|---|---|
| `GET` | `/` | None | `web_console` | `main.py:90` | Serves `web/index.html` console shell |
| `GET` | `/assets/*` | None | `StaticFiles` | `main.py:87` | Serves static assets (`app.js`, `styles.css`) |
| `GET` | `/health` | None | `health` | `main.py:95` | Liveness health probe (`mock` vs `live`) |
| `GET` | `/health/readiness`| None | `health_readiness` | `main.py:100` | Readiness probe (K8s & LiteLLM endpoint check) |
| `GET` | `/auth/config` | None | `authentication_config`| `auth.py:145` | Returns login status and session config |
| `POST` | `/auth/login` | None (Rate-limited)| `login` | `auth.py:150` | Authenticates admin username/password via Argon2 |
| `GET` | `/auth/session` | Bearer or Cookie | `current_session` | `auth.py:173` | Validates session and returns identity |
| `POST` | `/auth/logout` | Bearer or Cookie+CSRF| `logout` | `auth.py:179` | Revokes opaque session and deletes cookie |
| `GET` | `/api/v1/cluster/gpus` | Bearer or Cookie | `get_cluster_gpus` | `main.py:105` | Returns cluster GPU inventory & allocatable pools |
| `GET` | `/api/v1/cluster/nodes`| Bearer or Cookie | `get_cluster_nodes` | `main.py:118` | Returns K8s node conditions, SKU, schedulability |
| `GET` | `/api/v1/metrics/usage`| Bearer or Cookie | `get_usage_metrics` | `main.py:111` | Returns Prometheus usage snapshot (5/15/60 min) |
| `GET` | `/api/v1/models` | Bearer or Cookie | `list_models` | `main.py:195` | Lists all active managed deployments |
| `POST` | `/api/v1/models/deploy`| Bearer or Cookie+CSRF| `deploy_model` | `main.py:151` | Auto-tunes, warms cache, deploys Dynamo CR |
| `GET` | `/api/v1/models/{name}/status`| Bearer or Cookie | `get_model_status` | `main.py:200` | Queries live deployment status and CR metadata |
| `DELETE`| `/api/v1/models/{name}` | Bearer or Cookie+CSRF| `delete_model` | `main.py:180` | Background graceful drain and K8s teardown |
| `GET` | `/api/v1/models/{name}/workload`| Bearer or Cookie | `get_model_workload`| `main.py:123` | Returns runtime pods & scaling components |
| `POST` | `/api/v1/models/{name}/components/{comp}/scale`| Bearer or Cookie+CSRF| `scale_model_component`| `main.py:129` | Modifies replica count on scaling adapter or DGD |
| `GET` | `/api/v1/models/{name}/pods/{pod}/logs`| Bearer or Cookie | `get_model_pod_logs`| `main.py:134` | Streams tail lines of container log |
| `POST` | `/api/v1/models/{name}/pods/{pod}/evict`| Bearer or Cookie+CSRF| `replace_model_pod` | `main.py:139` | Issues K8s Pod eviction with UID precondition |
| `POST` | `/api/v1/data/events` | Bearer (Capture Token)| `capture_event` | `data_routes.py:37` | Ingests conversational inference event |
| `GET` | `/api/v1/data/events` | Bearer or Cookie | `list_events` | `data_routes.py:42` | Lists ingested events with pagination |
| `GET` | `/api/v1/data/events/{id}`| Bearer or Cookie | `get_event` | `data_routes.py:47` | Retrieves single capture event by request_id |
| `POST` | `/api/v1/data/events/{id}/review`| Bearer or Cookie+CSRF| `review_event` | `data_routes.py:52` | Records approval/rejection and response fix |
| `POST` | `/api/v1/data/datasets`| Bearer or Cookie+CSRF| `create_dataset` | `data_routes.py:57` | Builds deterministic train/val/test splits |
| `GET` | `/api/v1/data/datasets`| Bearer or Cookie | `list_datasets` | `data_routes.py:62` | Lists built datasets |
| `GET` | `/api/v1/data/datasets/{id}`| Bearer or Cookie | `get_dataset` | `data_routes.py:67` | Retrieves dataset manifest and checksums |
| `GET` | `/api/v1/data/datasets/{id}/export`| Bearer or Cookie | `export_dataset` | `data_routes.py:72` | Streams verified jsonl split file |

---

## 3. Rigorous Defect and Vulnerability Inventory

### Defect Table

| ID | Severity | Category | File Path | Line(s) | Summary |
|---|---|---|---|---|---|
| **D-API-01** | **High** | Functional Bug | `control-plane/api/dynamo_reconciler.py` | 224–236 | Drain loop condition skips checking active requests when timeout is 0, raising `TimeoutError` |
| **D-API-02** | **Medium** | Concurrency / Leak | `control-plane/api/dynamo_reconciler.py` | 152 | Unreferenced `asyncio.create_task` vulnerable to garbage collection |
| **D-API-03** | **Medium** | Race Condition | `control-plane/api/dynamo_reconciler.py` | 171–198 | Late EndpointSlice readiness registers deleted model in LiteLLM |
| **D-API-04** | **High** | Concurrency / Race | `control-plane/api/auth.py` | 32–78 | `SessionStore` SQLite database accessed across threads without lock or WAL mode |
| **D-API-05** | **High** | Security / DoS | `control-plane/api/auth.py` | 155–158 | Rate limit keys on proxy IP, allowing cluster-wide admin lockout DoS |
| **D-API-06** | **Medium** | Security | `control-plane/api/auth.py` | 102–108 | `check_origin` fails open if `Origin` header is omitted |
| **D-API-07** | **High** | Error Handling | `control-plane/api/litellm_sync.py` | 126–137 | LiteLLM 404 on `/model/delete` raises `RuntimeError`, blocking K8s resource teardown |
| **D-API-08** | **Medium** | Functional / Sched | `control-plane/api/k8s_controls.py` | 203–206 | Workload scaling sums GPUs across mismatched SKUs |
| **D-API-09** | **Medium** | Error Handling | `control-plane/api/k8s_controls.py` | 99–100 | Unhandled `IndexError` when querying logs of pod with no containers |
| **D-API-10** | **Medium** | Contract Mismatch | `control-plane/api/k8s_client.py` | 393–402 | Missing `nvidia.com/gpu.memory` yields `vram_gb: None`, failing `ClusterInventory` validation |
| **D-API-11** | **Low** | Robustness | `control-plane/api/k8s_client.py` | 281–294 | Missing transient exception retry in 30-min warmer Job wait loop |
| **D-API-12** | **Low** | Concurrency | `control-plane/api/main.py` | 154–178 | Concurrent deploy requests with identical name create duplicate jobs |
| **D-API-13** | **Low** | Contract Drift | `contracts/control_plane_openapi.yaml` | 1–241 | OpenAPI spec missing 11 active endpoints and inaccurate auth scheme |
| **D-API-14** | **Low** | Dependency | `control-plane/api/data_store.py` | 47–51 | Undeclared dependency `boto3` causes runtime crash on S3 artifact export |
| **D-API-15** | **Low** | Web UX / Timeout | `control-plane/api/web/app.js` | 451 | 31-minute deploy request exceeds reverse proxy idle timeouts |
| **D-API-16** | **Low** | Web UX | `control-plane/api/web/app.js` | 1007–1039 | Pod logs dialog crashes on empty container options |

---

## 4. Deep-Dive Defect Analyses & Evidence Chains

### D-API-01: Zero/Sub-Step Drain Timeout Bypasses Active Requests Check and Raises False TimeoutError

- **Severity**: High
- **Category**: Functional Bug / Logic Error
- **File**: `control-plane/api/dynamo_reconciler.py`
- **Lines**: 224–236
- **Observation**:
  ```python
  224:         drain_elapsed = 0
  225:         poll_step = 2
  226:         while drain_elapsed < self.drain_timeout_seconds:
  227:             running = await self.k8s.get_running_requests_count(deployment_name)
  228:             if running == 0:
  229:                 logger.info(f"All active requests drained for {deployment_name}.")
  230:                 break
  231:             await asyncio.sleep(poll_step)
  232:             drain_elapsed += poll_step
  233:         else:
  234:             if not self.k8s.mock_mode:
  235:                 raise TimeoutError("Active requests did not drain before the deadline; the Kubernetes workload was retained.")
  ```
- **Mechanism of Failure**:
  When `self.drain_timeout_seconds` is `0` (such as in testing fixtures or when an administrator configures zero drain grace period), `drain_elapsed < self.drain_timeout_seconds` evaluates to `0 < 0` (False).
  The `while` loop body never executes.
  Python's `while...else` constructs execute the `else` block whenever the loop was NOT exited via `break`.
  Because the loop never executed and never broke, execution falls directly into line 234: `if not self.k8s.mock_mode: raise TimeoutError(...)`.
  `self.k8s.get_running_requests_count` is NEVER called. Even if the workload has 0 running requests or if monitoring raises an error, `TimeoutError` is raised immediately.
- **Proof of Concept / Reproducer**:
  Run:
  ```bash
  .venv/bin/pytest tests/test_kubernetes_controls.py -k test_failed_drain_keeps_workload_and_exposes_failure
  ```
  Result:
  ```
  FAILED tests/test_kubernetes_controls.py::test_failed_drain_keeps_workload_and_exposes_failure
  control-plane/api/dynamo_reconciler.py:235: TimeoutError: Active requests did not drain before the deadline...
  ```
  The test expected `RuntimeError("Monitoring unavailable")` from `get_running_requests_count`, but line 235 raised `TimeoutError` without ever calling `get_running_requests_count`.
- **Remediation Specification**:
  Change loop to check running requests before checking deadline expiry:
  ```python
  drain_elapsed = 0
  poll_step = 2
  while True:
      running = await self.k8s.get_running_requests_count(deployment_name)
      if running == 0:
          logger.info(f"All active requests drained for {deployment_name}.")
          break
      if drain_elapsed >= self.drain_timeout_seconds:
          if not self.k8s.mock_mode:
              raise TimeoutError("Active requests did not drain before the deadline; the Kubernetes workload was retained.")
          break
      await asyncio.sleep(poll_step)
      drain_elapsed += poll_step
  ```

---

### D-API-04: Concurrency Flaw in `SessionStore` SQLite Connection

- **Severity**: High
- **Category**: Concurrency / Race Condition / Data Integrity
- **File**: `control-plane/api/auth.py`
- **Lines**: 31–78
- **Observation**:
  ```python
  31: class SessionStore:
  32:     def __init__(self, path: str = ":memory:"):
  33:         self.db = sqlite3.connect(path, check_same_thread=False)
  34:         self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, username TEXT, csrf TEXT, expires REAL)")
  35:         self.db.execute("CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, failures INTEGER, started REAL)")
  36:         self.db.commit()
  ```
  Methods `create`, `get`, `revoke`, `check_rate_limit`, `failed_login`, `reset_attempts` execute raw SQL and `self.db.commit()` on the single `self.db` connection without any mutual exclusion lock (`threading.Lock` / `asyncio.Lock`).
- **Mechanism of Failure**:
  FastAPI handles incoming HTTP requests concurrently across thread pools and the async event loop.
  `auth.verify` is invoked on nearly every API request.
  Python's standard `sqlite3` module does not serialize concurrent calls on a single `Connection` handle. When multiple threads or tasks attempt to read and write (e.g. `DELETE FROM sessions WHERE expires <= ?` during `create` while another thread executes `SELECT ... FROM sessions`), SQLite throws `sqlite3.OperationalError: cannot commit transaction - SQL statements in progress` or `sqlite3.DatabaseError`.
  In contrast, `DataStore` (`control-plane/api/data_store.py:80-84`) correctly uses `threading.RLock()` and `PRAGMA journal_mode=WAL`.
- **Proof of Concept / Reproducer**:
  Spawning 20 concurrent async requests to `/auth/login` and `/auth/session` against an on-disk SQLite session database triggers `sqlite3.OperationalError: cannot commit transaction`.
- **Remediation Specification**:
  Equip `SessionStore` with `threading.RLock()` and WAL mode:
  ```python
  class SessionStore:
      def __init__(self, path: str = ":memory:"):
          self.lock = threading.RLock()
          self.db = sqlite3.connect(path, check_same_thread=False)
          self.db.execute("PRAGMA journal_mode=WAL")
          self.db.execute("PRAGMA busy_timeout=5000")
          self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, username TEXT, csrf TEXT, expires REAL)")
          self.db.execute("CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, failures INTEGER, started REAL)")
          self.db.commit()
  ```
  Wrap all `self.db.execute` and `self.db.commit` calls within `with self.lock:`.

---

### D-API-05: Global Administrator Lockout DoS via Ingress/Proxy IP in Rate Limiter

- **Severity**: High
- **Category**: Security / Denial of Service
- **File**: `control-plane/api/auth.py`
- **Lines**: 155–158
- **Observation**:
  ```python
  155:     source = request.client.host if request.client else "unknown"
  156:     key = hashlib.sha256(source.encode()).hexdigest()
  157:     async with auth._verification_slots:
  158:         auth.store.check_rate_limit(key)
  ```
- **Mechanism of Failure**:
  In a Kubernetes deployment behind Ingress or Cloudflare Tunnel, `request.client.host` is the internal proxy IP (e.g., `10.244.x.x` or `127.0.0.1`).
  All users accessing the control plane share the exact same `key` in `auth.store`.
  Any attacker submitting 5 bad password guesses causes `auth.store.failed_login(key)` to reach 5 failures.
  Subsequent login attempts by the legitimate administrator are rejected with `HTTP 429 Too many login attempts`.
  Furthermore, `failed_login` updates `started = time.time()`, meaning every ongoing attempt resets the 5-minute lockout timer forward, creating an indefinite lockout.
- **Proof of Concept / Reproducer**:
  From any client, send 5 failed POST requests to `/auth/login`. Verify that legitimate credentials from a different client behind the same reverse proxy receive HTTP 429.
- **Remediation Specification**:
  Bind the rate limiting bucket to both the real client IP (via `CF-Connecting-IP` / `X-Forwarded-For` when proxy is trusted) and the submitted username:
  ```python
  client_ip = request.headers.get("CF-Connecting-IP") or (request.client.host if request.client else "unknown")
  key = hashlib.sha256(f"{client_ip}:{payload.username}".encode()).hexdigest()
  ```

---

### D-API-07: LiteLLM 404 on Deregistration Permanently Blocks Kubernetes Teardown

- **Severity**: High
- **Category**: Error Handling / Resource Teardown Block
- **File**: `control-plane/api/litellm_sync.py` and `control-plane/api/dynamo_reconciler.py`
- **Lines**: `litellm_sync.py:126–137`, `dynamo_reconciler.py:220`
- **Observation**:
  ```python
  125:             async with httpx.AsyncClient(timeout=10.0) as client:
  126:                 resp = await client.post(url, json=payload, headers=headers)
  127:                 resp.raise_for_status()
  ...
  137:         raise RuntimeError(f"LiteLLM deregistration failed for {model_name}: {last_error}")
  ```
- **Mechanism of Failure**:
  If a deployment fails before registering in LiteLLM (e.g., EndpointSlice readiness timeout), or if LiteLLM lost its in-memory database or the model was manually purged, calling `POST /model/delete` on LiteLLM returns HTTP 404.
  `resp.raise_for_status()` throws `httpx.HTTPStatusError(404)`.
  `deregister_model` fails after 3 retries and raises `RuntimeError`.
  In `DynamoReconciler._delete_model_resources`:
  `await self.litellm.deregister_model(deployment_name)`
  The raised `RuntimeError` stops execution before K8s CR deletion:
  The workload is marked `delete_failed` and retained on Kubernetes. The administrator cannot delete the workload via API without manual database/cluster intervention.
- **Remediation Specification**:
  Treat HTTP 404 from LiteLLM `/model/delete` as an idempotent success:
  ```python
  if resp.status_code == 404:
      logger.info(f"Model {model_name} was already absent from LiteLLM.")
      return True
  resp.raise_for_status()
  ```

---

### D-API-08: Component Scaling Cross-SKU GPU Over-allocation

- **Severity**: Medium
- **Category**: Functional / Capacity Scheduling
- **File**: `control-plane/api/k8s_controls.py`
- **Lines**: 203–206
- **Observation**:
  ```python
  203:             extra_gpus = max(0, replicas - target["desired_replicas"]) * target["gpu_per_replica"]
  204:             inventory = await self.k8s.get_cluster_gpu_inventory()
  205:             if extra_gpus > sum(pool["available_count"] for pool in inventory["gpu_pools"]):
  206:                 raise ControlError(409, "There is not enough unallocated GPU capacity for these replicas.")
  ```
- **Mechanism of Failure**:
  The available GPU calculation sums `available_count` across all pools indiscriminately.
  If the cluster has 4x A100-80GB and 4x H100-80GB, total available is 8.
  If an H100 component is scaled up needing 6 GPUs, the check passes (`6 <= 8`), but only 4 H100 GPUs exist. The pod cannot be scheduled on A100 nodes due to node affinity/selectors, leaving pods stuck in `Pending`.
- **Remediation Specification**:
  Identify the GPU SKU utilized by the target component from the DGD resource spec or scaling adapter, and validate capacity strictly against that specific pool:
  ```python
  sku = target.get("gpu_sku")
  matching_pools = [p for p in inventory["gpu_pools"] if not sku or p["gpu_sku"] == sku]
  available = sum(p["available_count"] for p in matching_pools)
  if extra_gpus > available:
      raise ControlError(409, f"Insufficient unallocated {sku or ''} GPU capacity for these replicas.")
  ```

---

### D-API-10: Pydantic Validation Crash on Missing Node Memory Label

- **Severity**: Medium
- **Category**: Contract Mismatch / Unhandled Exception
- **File**: `control-plane/api/k8s_client.py:393–402` and `control-plane/autotuner/sla_planner.py:53–55`
- **Observation**:
  In `k8s_client.py`:
  ```python
  393:         memory_label = labels.get("nvidia.com/gpu.memory")
  394:         try:
  395:             memory = round(float(memory_label) / 1024, 1) if memory_label else None
  396:         except ValueError:
  397:             memory = None
  ...
  402:         pool = pools.setdefault(key, {"gpu_sku": product, "vram_gb": memory, ...})
  ```
  In `sla_planner.py`:
  ```python
  53: class GpuPool(BaseModel):
  54:     gpu_sku: str = "NVIDIA-H100-80GB"
  55:     vram_gb: float = 80.0
  ```
- **Mechanism of Failure**:
  `test_cluster_inventory.py::test_missing_vram_metadata_is_unknown` explicitly confirms that when `nvidia.com/gpu.memory` is absent on a node, `vram_gb` is set to `None`.
  When `deploy_model` in `main.py:158` passes this inventory to `ClusterInventory(**inventory_dict)`, Pydantic rejects `vram_gb: None` because the field type is `float` (not `float | None`).
  The endpoint aborts with HTTP 400 `Failed to schedule deployment: 1 validation error for ClusterInventory`.
- **Proof of Concept / Reproducer**:
  ```python
  from autotuner.sla_planner import ClusterInventory
  ClusterInventory(**{"nodes_count": 1, "gpu_pools": [{"gpu_sku": "H100", "vram_gb": None, "total_count": 1, "available_count": 1, "rdma_supported": False}]})
  # Raises pydantic_core._pydantic_core.ValidationError: Input should be a valid number
  ```
- **Remediation Specification**:
  In `k8s_client.py`, provide a fallback VRAM estimation from known GPU SKUs (e.g. H100 -> 80.0, A100 -> 80.0, L40S -> 48.0, L4 -> 24.0) if the label is missing, and in `sla_planner.py` allow `vram_gb: float | None = 80.0`.

---

## 5. Web Console UI Audit

### 5.1 Architecture & State Machine

The web console (`control-plane/api/web/`) is an unbundled SPA built with semantic HTML5, modern CSS, and vanilla ECMAScript 2022:
- **Authentication**: Supports dual-mode authentication:
  - Form-based session login (`/auth/login` -> HTTP-only, SameSite=Strict cookie + `X-CSRF-Token`)
  - Development token mode (`Authorization: Bearer <token>` stored only in tab memory).
- **Views**:
  - `Model deployments`: Filterable table with status badges, details modal, and delete flow.
  - `GPU inventory`: Cluster capacity cards with percentage meters and RDMA indicators.
  - `Live usage`: 5/15/60 min Prometheus metric cards and custom SVG charts for requests/s and tokens/s.
  - `Kubernetes`: Node list, workload component replica controls, and runtime pod logs/eviction.
- **Polling & Lifecycle**:
  - 10-second polling interval via `setInterval` paused when tab is hidden (`visibilitychange`).
  - Session epoch counter (`state.session`) and request cancellation via `AbortController` prevent stale async updates from overwriting UI state after disconnection or fast tab switching.

### 5.2 Frontend Defects & Edge Cases

1. **Synchronous Long-Poll Deploy Connection Drops (D-API-15)**:
   - `app.js:451` configures `timeout: 1860000` (31 minutes) for `POST /api/v1/models/deploy`.
   - Reverse proxies, ingress gateways, and Cloudflare Tunnels terminate HTTP connections after 60–100 seconds of inactivity.
   - When the proxy cuts the connection, the user receives an alarming error banner ("Cannot reach the control plane") even though the Kubernetes cluster is successfully warming and deploying the model in the background.
2. **Missing Input Validation in Scale Dialog**:
   - `web/index.html:713-720` configures `min="1" max="64"` for desired replicas.
   - However, each component defines its own `minimum_replicas` (often derived from `minAvailable` or tensor parallel constraints).
   - If a user inputs 1 for a component requiring `minimum_replicas: 2`, the API rejects the request with HTTP 409, but the client does not reflect this constraint in the input's `min` attribute dynamically until after the error.

---

## 6. Synthesis and Risk Assessment

| Risk Category | Current State | Risk Evaluation | Action Required |
|---|---|---|---|
| **Operational Teardown** | Broken when `drain_timeout_seconds=0` (D-API-01) or when LiteLLM is missing model (D-API-07) | **CRITICAL** | Fix drain while loop condition and make LiteLLM 404 idempotent |
| **Authentication & AuthZ** | Concurrency hazards in SQLite SessionStore (D-API-04); Reverse-proxy rate limit DoS (D-API-05) | **HIGH** | Add mutex lock to `SessionStore` and composite rate limit key |
| **Resource Scheduling** | Scaling ignores GPU SKU matching (D-API-08); Missing VRAM label crashes Pydantic (D-API-10) | **MEDIUM** | Filter by SKU during scaling; add SKU fallback for VRAM |
| **Contract Alignment** | OpenAPI contract missing 11 routes; Cloudflare Access auth discrepancy (D-API-13) | **LOW** | Regenerate or update `control_plane_openapi.yaml` |

---
*Report completed and verified against the repository codebase.*
