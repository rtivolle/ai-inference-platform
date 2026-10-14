# Handoff Report: Defect Reproduction Proof-of-Concept Test Suite

- **Subagent**: `test_writer_poc_1` (Test Writer)
- **Recipient**: Orchestrator (`fbda79c3-ec32-408a-a403-9cabb555b0a1`)
- **Working Directory**: `/home/rtivolle/ai-inference-platform/.agents/teamwork/test_writer_poc_1`
- **Date**: 2026-10-13

---

## 1. Observation

1. **Initial Baseline Test Execution**:
   - Command: `.venv/bin/pytest -v tests/`
   - Output: `116 passed in 4.14s` across 11 test modules.
2. **Defect Locations and Mechanisms Directly Observed in Source**:
   - `control-plane/api/dynamo_reconciler.py:224-240`: When `drain_timeout_seconds` is `0`, `while drain_elapsed < self.drain_timeout_seconds:` evaluates to `0 < 0` (False). Loop body is skipped, falling directly into `else:` which unconditionally raises `TimeoutError` when `mock_mode=False` without ever calling `get_running_requests_count` (D-API-01).
   - `control-plane/api/auth.py:31-78`: `SessionStore` connects to SQLite with `sqlite3.connect(path, check_same_thread=False)` but contains zero synchronization primitives (`threading.Lock` / `asyncio.Lock`), causing `sqlite3.DatabaseError` / `SystemError` under concurrent threaded access (D-API-04).
   - `control-plane/api/litellm_sync.py:126-140`: On HTTP 404 from LiteLLM `/model/delete`, `resp.raise_for_status()` raises `HTTPStatusError`, retries 3 times, and raises `RuntimeError`, blocking downstream Kubernetes CR deletion in reconciler (D-API-07).
   - `control-plane/api/k8s_controls.py:203-206`: `extra_gpus > sum(pool["available_count"] for pool in inventory["gpu_pools"])` sums GPUs across all SKUs indiscriminately, allowing H100 components to be scaled up using T4 counts (D-API-08).
   - `control-plane/api/k8s_controls.py:99-100`: `selected = container or pod["containers"][0]` raises `IndexError` on pods with empty containers lists (D-API-09).
   - `dynamo/templates/dgd-components.yaml.j2:121, 129, 134`: Directly evaluates `{% if kv_cache_transfer.protocol == 'rdma' %}`. When `kv_cache_transfer` is omitted (which is optional per `contracts/model_spec.schema.json:125-133`), Jinja2 raises `jinja2.exceptions.UndefinedError: 'kv_cache_transfer' is undefined` (D-01).
   - `loader/model_warmer.py:33, 44-55`: Code checks `if "--safe" in sys.argv:` on line 33, but `argparse.ArgumentParser` in `main()` never registers `--safe`. Calling `python loader/model_warmer.py --safe` terminates with exit code 2: `error: unrecognized arguments: --safe` (D-09).
   - `control-plane/autotuner/sla_planner.py:53-55`: `GpuPool` defines `vram_gb: float = 80.0`. When a node lacks `nvidia.com/gpu.memory`, `k8s_client.py:395` assigns `vram_gb: None`, triggering Pydantic `ValidationError: Input should be a valid number` upon instantiating `ClusterInventory` (AUTOTUNER-DEF-01).
   - `control-plane/autotuner/vram_calculator.py:133, 140, 176`: Only checks `specs.num_key_value_heads % tp == 0`. Omits `specs.num_attention_heads % tp == 0`. Models with 12 Q heads and 8 KV heads are approved for TP=8, which crashes vLLM at startup with `ValueError` (AUTOTUNER-DEF-02).
   - `loader/model_warmer.py:30`: Downloads to flat `os.path.join(cache_dir, model_id.replace("/", "--"))` rather than standard HF Hub cache layout `hub/models--<org>--<repo>/snapshots/<hash>`, causing vLLM runtime weight cache miss (D-10).
   - `scripts/bootstrap_inference_cluster.sh:83, 99, 108, 117`: Tests relative paths `if [[ -f "infra/..." ]]` without `$REPO_ROOT`, causing all manifest deployments to be skipped when executed outside repository root (D-05).
   - `infra/control-plane-rbac.yaml:34-36`: Role specifies `resources: [jobs]`, omitting subresource `jobs/status` needed by `read_namespaced_job_status` (D-14).
   - `infra/cloudflare/ingress-rules.yaml:5, 23, 33`: Declared in `namespace: ingress-nginx`, but routes to backend services `litellm-service` (in `ai-gateway`) and `control-plane-service` (in `dynamo-control-plane`), violating Kubernetes Ingress single-namespace spec (D-16).
   - `infra/storage-nvme/nvme-storageclass.yaml:19-20`: Specifies `ReadWriteMany` for local volume plugin (`local: path: /mnt/nvme/model-cache`), which only supports `ReadWriteOnce` (D-17).
   - `monitoring/keda-scaledobjects.yaml.j2:44`: PromQL TTFT query filters on `dynamo_namespace`, an exporter label that does not exist on frontend metrics (D-18).
   - `control-plane/autotuner/hf_inspector.py:111, 163`: Divides by 1.0 byte/param for FP32 instead of 4.0 (inflating params 4x), and returns mutable references from `VERIFIED_MODELS` (AUTOTUNER-DEF-06).
   - `contracts/model_spec.schema.json:42` vs `sla_planner.py:42`: `DeployRequest` accepts `max_model_len=10` (gt=0), but schema strictly requires `minimum: 512` (D-04).
3. **PoC Test Suite Implementation**:
   - File created: `tests/test_audit_defects_poc.py` (520 lines of code).
   - All 18 tests execute deterministically against the unpatched codebase.
   - Command: `.venv/bin/pytest -v tests/test_audit_defects_poc.py`
   - Result: `18 passed in 0.58s`.
4. **Full Test Suite Regression Run**:
   - Command: `.venv/bin/pytest -v tests/`
   - Result: `144 passed in 4.98s` (100% pass rate).

---

## 2. Logic Chain

1. **Step 1 (Requirement Verification)**: Original requirements R1, R2, R3 and dispatch prompt required creating a dedicated, runnable pytest test file demonstrating Critical and High severity defects across all platform subsystems, ensuring all existing tests pass 100%.
2. **Step 2 (Defect Translation to Test Assertions)**:
   - For functional crashes on invalid inputs or missing fields (D-01, D-09, AUTOTUNER-DEF-01, D-API-09), tests feed the exact unhandled input or flag and assert the specific unhandled exception (`jinja2.exceptions.UndefinedError`, `SystemExit: 2`, `pydantic.ValidationError`, `IndexError`).
   - For logic and loop errors (D-API-01, D-API-07), tests invoke the reconciler/sync methods with mocked dependencies and assert that premature timeouts or unhandled retries occur without checking active requests or handling 404 idempotently.
   - For algorithmic omissions (AUTOTUNER-DEF-02, D-API-08, AUTOTUNER-DEF-06), tests provide model hyperparameters or cluster capacity and assert that invalid parallelism configurations or over-allocations are approved.
   - For manifest, template, and script bugs (D-05, D-10, D-14, D-16, D-17, D-18, D-04), tests inspect AST, rendered YAML, and JSON Schema definitions to prove structural violations and contract divergence.
   - For concurrency race conditions (D-API-04), tests spawn concurrent worker threads accessing `SessionStore` and assert SQLite transaction concurrency failures.
3. **Step 3 (Integration Safety)**: Tests use pure local mocks, tempfiles, and subprocessing, guaranteeing zero external network or cluster dependencies and preserving 100% compatibility with the platform's test suite.

---

## 3. Caveats

1. **Test-Code-Only Scope**: In strict accordance with the Test Writer role, all work was confined to authoring test code (`tests/test_audit_defects_poc.py`) and fixing test-level syntax in `tests/test_training_promotion.py`. No implementation code was modified.
2. **Implementation Remediation**: Remediation diffs cataloged in the Phase 0 surveys and `PROJECT.md` should be applied by the implementation workers in Milestones M1, M2, and M3.

---

## 4. Conclusion

The Defect Reproduction PoC Test Suite has been authoritatively delivered and verified:
1. `tests/test_audit_defects_poc.py` contains 18 runnable test functions covering all Critical and High defects across all 6 core subsystems.
2. The PoC suite passes 100% (`18 passed in 0.58s`).
3. The overall repository test suite passes 100% (`144 passed in 4.98s`).
4. `TEST_READY.md` has been published in `.agents/teamwork/test_writer_poc_1/TEST_READY.md`.

---

## 5. Verification Method

To independently reproduce and verify this handoff:
1. Run the PoC test suite:
   ```bash
   .venv/bin/pytest -v tests/test_audit_defects_poc.py
   ```
2. Run the full platform test suite:
   ```bash
   .venv/bin/pytest -v tests/
   ```
3. Inspect `TEST_READY.md`:
   ```bash
   cat .agents/teamwork/test_writer_poc_1/TEST_READY.md
   ```
