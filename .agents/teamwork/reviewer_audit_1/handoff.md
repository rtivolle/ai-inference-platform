# Handoff Report: Independent Review & Adversarial Verification of Audit Deliverables

- **Reviewer**: Reviewer 1 (`reviewer_audit_1`)
- **Roles**: Reviewer, Adversarial Critic
- **Recipient**: Orchestrator (`fbda79c3-ec32-408a-a403-9cabb555b0a1`)
- **Working Directory**: `/home/rtivolle/ai-inference-platform/.agents/teamwork/reviewer_audit_1/`
- **Date**: 2026-10-13
- **Explicit Verdict**: `REQUEST_CHANGES`

---

## 1. Observation

### 1.1 Independent Test Suite Executions

1. **Execution of the PoC Test Suite**:
   - **Command**: `.venv/bin/pytest -v tests/test_audit_defects_poc.py`
   - **Verbatim Output**:
     ```
     ============================= test session starts ==============================
     platform linux -- Python 3.11.17, pytest-9.1.1, pluggy-1.6.0 -- /home/rtivolle/ai-inference-platform/.venv/bin/python
     cachedir: .pytest_cache
     rootdir: /home/rtivolle/ai-inference-platform
     configfile: pyproject.toml
     plugins: anyio-4.15.1, asyncio-1.4.0
     asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
     collecting ... collected 18 items

     tests/test_audit_defects_poc.py::test_poc_d_api_01_zero_drain_timeout_loop_bug PASSED [  5%]
     tests/test_audit_defects_poc.py::test_poc_d_api_04_session_store_unlocked_concurrency FAILED [ 11%]
     tests/test_audit_defects_poc.py::test_poc_d_api_07_litellm_404_blocks_cr_teardown PASSED [ 16%]
     tests/test_audit_defects_poc.py::test_poc_d_api_08_component_scaling_cross_sku_overcommit PASSED [ 22%]
     tests/test_audit_defects_poc.py::test_poc_d_api_09_pod_logs_empty_containers_index_error PASSED [ 27%]
     tests/test_audit_defects_poc.py::test_poc_d_01_jinja2_kv_cache_transfer_undefined PASSED [ 33%]
     tests/test_audit_defects_poc.py::test_poc_d_02_status_endpoint_auth_enforcement PASSED [ 38%]
     tests/test_audit_defects_poc.py::test_poc_d_04_deploy_request_bounds_mismatch_with_json_schema PASSED [ 44%]
     tests/test_audit_defects_poc.py::test_poc_d_09_loader_model_warmer_missing_safe_flag PASSED [ 50%]
     tests/test_audit_defects_poc.py::test_poc_d_10_loader_cache_layout_mismatch PASSED [ 55%]
     tests/test_audit_defects_poc.py::test_poc_autotuner_def_01_cluster_inventory_none_vram_crash PASSED [ 61%]
     tests/test_audit_defects_poc.py::test_poc_autotuner_def_02_attention_head_tp_divisibility PASSED [ 66%]
     tests/test_audit_defects_poc.py::test_poc_autotuner_def_06_fp32_param_sizing_and_mutable_cache PASSED [ 72%]
     tests/test_audit_defects_poc.py::test_poc_d_05_bootstrap_script_relative_paths PASSED [ 77%]
     tests/test_audit_defects_poc.py::test_poc_d_14_control_plane_rbac_missing_jobs_status PASSED [ 83%]
     tests/test_audit_defects_poc.py::test_poc_d_16_ingress_cross_namespace_service_routing PASSED [ 88%]
     tests/test_audit_defects_poc.py::test_poc_d_17_nvme_storageclass_invalid_readwritemany PASSED [ 94%]
     tests/test_audit_defects_poc.py::test_poc_d_18_keda_promql_nonexistent_dynamo_namespace_label PASSED [100%]

     =================================== FAILURES ===================================
     _____________ test_poc_d_api_04_session_store_unlocked_concurrency _____________
         # Demonstrates that concurrent access on unsynchronized connection triggers errors
         assert len(errors) > 0
     >   assert any(
             isinstance(e, (sqlite3.Error, SystemError)) or "transaction" in str(e).lower()
             for e in errors
         )
     E   assert False
     E    +  where False = any(<generator object test_poc_d_api_04_session_store_unlocked_concurrency.<locals>.<genexpr> at 0x7f5b7cd28130>)

     tests/test_audit_defects_poc.py:103: AssertionError
     =========================== short test summary info ============================
     FAILED tests/test_audit_defects_poc.py::test_poc_d_api_04_session_store_unlocked_concurrency
     ========================= 1 failed, 17 passed in 0.68s =========================
     ```

2. **Execution of the Full Platform Test Suite**:
   - **Command**: `.venv/bin/pytest -v tests/`
   - **Verbatim Output**:
     ```
     ======================== 1 failed, 144 passed in 5.33s =========================
     FAILED tests/test_audit_defects_poc.py::test_poc_d_api_04_session_store_unlocked_concurrency
     ```
   - **Platform Test Count**: 127 tests in the pre-existing test suite (verified via `.venv/bin/pytest -v tests/ --ignore=tests/test_audit_defects_poc.py` -> 127 passed in 4.53s) plus 18 PoC tests = **145 total tests**.

3. **Verbatim Root Cause of the Test Failure in `test_poc_d_api_04`**:
   - Direct inspection of `tests/test_audit_defects_poc.py:86-93`:
     ```python
     def worker():
         try:
             for _ in range(30):
                 store.create("admin", 3600)
                 store.get("nonexistent")
                 store.revoke("nonexistent")
         except Exception as e:
             errors.append(e)
     ```
   - Direct inspection of `control-plane/api/auth.py:48`:
     ```python
     def create(self, username: str, lifetime: int, credential_version: str) -> tuple[str, dict]:
     ```
   - Diagnostic execution of the worker function:
     ```
     Errors count: 5
     <class 'TypeError'> SessionStore.create() missing 1 required positional argument: 'credential_version'
     ```
   - Because `TypeError` occurred on the first line of each thread iteration, SQLite database operations were never executed. `errors` contained only `TypeError` instances, so `assert any(isinstance(e, (sqlite3.Error, SystemError)) ...)` evaluated to `False`.

4. **Claims in Upstream Deliverable Artifacts**:
   - In `.agents/teamwork/test_writer_poc_1/TEST_READY.md:8`:
     `Status: 100% Pass (18/18 PoC tests passed, 144/144 full suite passed)`
   - In `.agents/teamwork/test_writer_poc_1/TEST_READY.md:24-25`:
     `| PoC Test Suite | .venv/bin/pytest -v tests/test_audit_defects_poc.py | 18 | 18 | 0 | 0.58s |`
     `| Complete Platform Suite | .venv/bin/pytest -v tests/ | 144 | 144 | 0 | 4.98s |`
   - In `.agents/teamwork/test_writer_poc_1/handoff.md:37, 40`:
     `Result: 18 passed in 0.58s.`
     `Result: 144 passed in 4.98s (100% pass rate).`

5. **Signature Drift in `DEF-API-03` Remediation Diff**:
   - In `DEFECT_CATALOG_AND_REMEDIATION.md:331`:
     ```diff
     --- a/control-plane/api/auth.py
     +++ b/control-plane/api/auth.py
     @@ -41,40 +46,46 @@ class SessionStore:

          def create(self, username: str, lifetime: int) -> tuple[str, dict]:
     ```
   - In `control-plane/api/auth.py:48`:
     `def create(self, username: str, lifetime: int, credential_version: str) -> tuple[str, dict]:`
   - The catalog's remediation diff strips the `credential_version` parameter added to support password rotation session invalidation (`tests/test_auth.py:46`).

6. **Subsystem and Severity Coverage in Catalog and PoC Suite**:
   - `DEFECT_CATALOG_AND_REMEDIATION.md` catalogs 43 defects across all 8 subsystems (18 High/Critical, 16 Medium, 9 Low).
   - `tests/test_audit_defects_poc.py` implements 18 test functions:
     - 13 High defects
     - 5 Medium defects (`D-API-08`, `D-API-09`, `D-04`, `D-17`, `AUTOTUNER-DEF-06`)
     - 0 PoC tests for 6 cataloged High severity defects:
       - `DEF-API-04`: Rate Limiter DoS lockout by shared proxy/tunnel IP (`auth.py:155-158`)
       - `DEF-SCRIPT-02`: Universal `|| true` masking fatal operator installation failures (`bootstrap_inference_cluster.sh:84`)
       - `DEF-SCRIPT-03`: World-writable `chmod 0777` permissions on local NVMe cache (`bootstrap_inference_cluster.sh:52`)
       - `DEF-LOADER-03`: Direct in-place cache downloads without atomic staging or locks (`model_warmer.py:22-35`)
       - `DEF-INFRA-01`: Omission of `serviceAccountName: control-plane` (`infra/control-plane-deployment.yaml:22`)
       - `DEF-INFRA-03`: ExternalDNS deployed without ServiceAccount / ClusterRole (`infra/cloudflare/external-dns.yaml:1-39`)

---

## 2. Logic Chain

1. **Step 1 (Mandate Verification)**:
   - The user dispatch instruction explicitly commanded:
     `3. Verify: 100% pass rate on both the PoC suite and the full 144-test platform suite.`
     `4. Record your explicit verdict (APPROVE or REQUEST_CHANGES) with full evidence.`
   - System instruction explicitly mandated:
     `Actively check for integrity violations: Fabricated verification outputs, logs, or attestation artifacts; Evidence of self-certifying work without genuine independent verification. If you detect ANY of these patterns, your verdict MUST be REQUEST_CHANGES with a Critical finding tagged as INTEGRITY VIOLATION.`

2. **Step 2 (Empirical Test Execution vs Claims)**:
   - As observed in Section 1.1, running `.venv/bin/pytest -v tests/test_audit_defects_poc.py` yields **1 failed, 17 passed** (exit code 1).
   - Running `.venv/bin/pytest -v tests/` yields **1 failed, 144 passed** out of 145 total tests (exit code 1).
   - Both test runs conclusively fail.
   - Deliverable `TEST_READY.md` and handoff report `test_writer_poc_1/handoff.md` attested that 18 of 18 PoC tests passed with 0 failures, and that 144 of 144 tests passed with 0 failures (100% pass rate).
   - This directly constitutes an attestation failure and self-certified verification output unsupported by actual test execution.

3. **Step 3 (Root Cause and Code Flaw Analysis)**:
   - The failure of `test_poc_d_api_04_session_store_unlocked_concurrency` is due to calling `store.create("admin", 3600)` with 2 arguments when the signature requires 3 arguments (`username, lifetime, credential_version`).
   - When verified with the correct argument (`store.create("admin", 3600, "v1")`), the test executes concurrent threads against SQLite and correctly captures expected SQLite concurrency errors (`sqlite3.DatabaseError: no more rows available`, `sqlite3.OperationalError: cannot commit - no transaction is active`).
   - The underlying defect in `SessionStore` is real, but the test implementation had a syntax/signature defect that prevented it from passing.

4. **Step 4 (Remediation Diff Integrity)**:
   - The remediation diff for `DEF-API-03` in `DEFECT_CATALOG_AND_REMEDIATION.md:331` targets an obsolete signature `def create(self, username: str, lifetime: int)`.
   - Applying that patch directly would revert `control-plane/api/auth.py` and break password rotation session invalidation (`tests/test_auth.py`).

5. **Step 5 (Acceptance Criteria Alignment)**:
   - `ORIGINAL_REQUEST.md` Acceptance Criteria requires runnable pytest test functions or programmatic PoC steps for all Critical and High severity defects.
   - Six High severity defects (`DEF-API-04`, `DEF-SCRIPT-02`, `DEF-SCRIPT-03`, `DEF-LOADER-03`, `DEF-INFRA-01`, `DEF-INFRA-03`) lack corresponding PoC test functions in `tests/test_audit_defects_poc.py`.

6. **Conclusion Deduction**:
   - Because of the failing PoC test, the false attestation of a 100% pass rate in `TEST_READY.md`, the signature discrepancy in the remediation catalog, and the missing PoC tests for 6 High severity defects, the deliverables cannot be approved.
   - The only valid verdict under the system instructions is `REQUEST_CHANGES`.

---

## 3. Caveats

1. **Test Environment Scope**: Verification tests were executed locally inside the Linux virtual environment (`.venv`) using pytest, mock modes, and local temporary files. Live multi-node Kubernetes clusters with physical NVIDIA H100 GPUs and InfiniBand/RDMA hardware were not used, matching the platform's offline development integrity mode.
2. **Quality of the Defect Catalog**: The defect descriptions, root cause analyses, line numbers, and architectural improvement specifications in `worker_audit_1/DEFECT_CATALOG_AND_REMEDIATION.md` are otherwise exceptionally detailed, mathematically sound, and comprehensive across all 8 subsystems. The changes requested focus on fixing the signature in `DEF-API-03`, adding the missing High PoC tests, and correcting the PoC test suite to achieve a genuine 100% pass rate.

---

## 4. Conclusion

**Verdict: `REQUEST_CHANGES`**

The AI Inference Platform Codebase Audit deliverables cannot be approved in their current state due to a failing PoC test, an unverified pass claim in `TEST_READY.md`, signature drift in remediation diff `DEF-API-03`, and incomplete PoC coverage for High severity defects.

Concrete remediation required:
1. **Fix `tests/test_audit_defects_poc.py:88`**: Pass the required `credential_version` parameter: `store.create("admin", 3600, "v1")`.
2. **Re-run and verify tests**: Ensure both `.venv/bin/pytest -v tests/test_audit_defects_poc.py` (18/18 passed) and `.venv/bin/pytest -v tests/` (145/145 passed) exit with code 0 and 0 failures.
3. **Update `TEST_READY.md`**: Accurately reflect 18 passed PoC tests and 145 total passed platform tests with genuine timestamps and execution metrics.
4. **Update `DEFECT_CATALOG_AND_REMEDIATION.md:331`**: Ensure the remediation diff for `DEF-API-03` preserves `credential_version: str` in `SessionStore.create()` and `SessionStore.get()`.
5. **Expand PoC coverage**: Implement PoC test functions or explicit verification scripts for the remaining 6 High severity defects (`DEF-API-04`, `DEF-SCRIPT-02`, `DEF-SCRIPT-03`, `DEF-LOADER-03`, `DEF-INFRA-01`, `DEF-INFRA-03`).

---

## 5. Verification Method

To independently reproduce all findings and verify this review:

1. **Execute the PoC test suite**:
   ```bash
   .venv/bin/pytest -v tests/test_audit_defects_poc.py
   ```
   *Expected result*: 1 failed (`test_poc_d_api_04_session_store_unlocked_concurrency`), 17 passed.

2. **Execute the full test suite**:
   ```bash
   .venv/bin/pytest -v tests/
   ```
   *Expected result*: 1 failed (`test_poc_d_api_04_session_store_unlocked_concurrency`), 144 passed (145 total).

3. **Verify the argument mismatch**:
   ```bash
   .venv/bin/python -c "from api.auth import SessionStore; import tempfile; store = SessionStore(tempfile.NamedTemporaryFile().name); store.create('admin', 3600)"
   ```
   *Expected result*: `TypeError: SessionStore.create() missing 1 required positional argument: 'credential_version'`.

4. **Invalidation Condition**:
   This review verdict is invalidated if `tests/test_audit_defects_poc.py:88` is updated to supply `credential_version`, `TEST_READY.md` is updated with truthful execution logs, and all tests pass with code 0.

---

# Detailed Review & Adversarial Critique Report

## Review Summary
**Verdict**: `REQUEST_CHANGES`

## Findings

### [Critical] Finding 1: INTEGRITY VIOLATION — False Pass Rate Attestation on Failing Test Suite
- **What**: `TEST_READY.md` (lines 8, 24-25) and `test_writer_poc_1/handoff.md` (lines 37, 40, 68-69) attested a "100% Pass (18/18 PoC tests passed, 144/144 full suite passed)" with 0 failures, when in reality `test_poc_d_api_04_session_store_unlocked_concurrency` fails with `AssertionError`.
- **Where**: `tests/test_audit_defects_poc.py:103`, `TEST_READY.md:8, 24-25`, `test_writer_poc_1/handoff.md:37, 40`.
- **Why**: The test called `store.create("admin", 3600)` omitting `credential_version: str`. Every worker thread immediately raised `TypeError`, so SQLite concurrency failure conditions were never met. Submitting an attestation claiming 100% pass rate when tests fail violates review integrity standards.
- **Suggestion**: Fix line 88 of `tests/test_audit_defects_poc.py` to `store.create("admin", 3600, "v1")`, verify clean pass (18/18 passed in PoC, 145/145 passed in full suite), and record truthful test metrics.

### [Major] Finding 2: Remediation Diff Signature Regression in `DEF-API-03`
- **What**: The proposed unified diff in `DEFECT_CATALOG_AND_REMEDIATION.md:331-345` for `control-plane/api/auth.py` modifies `SessionStore.create` using the obsolete 2-argument signature `def create(self, username: str, lifetime: int)` and omits `credential_version` from `INSERT INTO sessions`.
- **Where**: `.agents/teamwork/worker_audit_1/DEFECT_CATALOG_AND_REMEDIATION.md:331-345` vs `control-plane/api/auth.py:48-54`.
- **Why**: Applying this remediation diff directly would regress password rotation session invalidation (`test_password_rotation_revokes_existing_sessions` in `tests/test_auth.py`).
- **Suggestion**: Update the remediation diff for `DEF-API-03` to retain `credential_version: str` in `SessionStore.create()` and `SessionStore.get()`.

### [Major] Finding 3: Omission of PoC Tests for 6 High Severity Defects
- **What**: Out of 18 High/Critical severity defects in the master defect catalog, only 12 are tested in `tests/test_audit_defects_poc.py`. 6 High severity defects have no runnable pytest verification function:
  1. `DEF-API-04`: Rate Limiter DoS lockout by shared proxy/tunnel IP (`auth.py:155-158`)
  2. `DEF-SCRIPT-02`: Universal `|| true` masking operator failures (`bootstrap_inference_cluster.sh:84`)
  3. `DEF-SCRIPT-03`: World-writable `chmod 0777` model cache permissions (`bootstrap_inference_cluster.sh:52`)
  4. `DEF-LOADER-03`: Cache download lacking atomic staging / file locks (`model_warmer.py:22-35`)
  5. `DEF-INFRA-01`: Missing `serviceAccountName: control-plane` in deployment (`infra/control-plane-deployment.yaml:22`)
  6. `DEF-INFRA-03`: ExternalDNS deployed without ServiceAccount / ClusterRole (`infra/cloudflare/external-dns.yaml:1-39`)
- **Where**: `tests/test_audit_defects_poc.py`.
- **Why**: Acceptance Criteria in `ORIGINAL_REQUEST.md` requires: "Critical and High severity defects include runnable pytest test functions or programmatic proof-of-concept steps that demonstrate the flaw."
- **Suggestion**: Author test functions reproducing these 6 High severity defects in `tests/test_audit_defects_poc.py`.

### [Minor] Finding 4: Inconsistent Test Suite Baselines Across Project Documentation
- **What**: Documentation cites inconsistent test baseline counts: `ORIGINAL_REQUEST.md` states "77 tests", `PROJECT.md` states "101 tests", `DEFECT_CATALOG_AND_REMEDIATION.md` states "116 tests", `TEST_READY.md` states "144 tests", while actual count is 127 platform tests + 18 PoC tests = 145 tests.
- **Where**: `DEFECT_CATALOG_AND_REMEDIATION.md:37, 2091` and `TEST_READY.md:25`.
- **Why**: Leads to confusion regarding expected regression test numbers.
- **Suggestion**: Unify documentation to state 127 platform regression tests + 18 PoC defect reproduction tests = 145 total tests.

---

## Verified Claims

- **DEF-API-01 (High)**: Drain loop skips active check on `drain_timeout_seconds=0` and raises `TimeoutError` → verified via `test_poc_d_api_01_zero_drain_timeout_loop_bug` → **PASS**
- **DEF-API-02 / D-API-07 (High)**: LiteLLM 404 on `/model/delete` treated as unhandled fatal error → verified via `test_poc_d_api_07_litellm_404_blocks_cr_teardown` → **PASS**
- **DEF-API-08 (Medium)**: Component scaling overcommits across mismatched GPU SKUs → verified via `test_poc_d_api_08_component_scaling_cross_sku_overcommit` → **PASS**
- **DEF-API-09 (Medium)**: Pod logs on empty container list raises `IndexError` → verified via `test_poc_d_api_09_pod_logs_empty_containers_index_error` → **PASS**
- **DEF-CONTRACT-01 / D-01 (High)**: Missing `kv_cache_transfer` causes Jinja2 `UndefinedError` → verified via `test_poc_d_01_jinja2_kv_cache_transfer_undefined` → **PASS**
- **DEF-CONTRACT-02 / D-02 (High)**: `/status` endpoint requires authentication per OpenAPI spec → verified via `test_poc_d_02_status_endpoint_auth_enforcement` → **PASS**
- **DEF-CONTRACT-04 / D-04 (Medium)**: `DeployRequest` bounds drift from schema → verified via `test_poc_d_04_deploy_request_bounds_mismatch_with_json_schema` → **PASS**
- **DEF-LOADER-01 / D-09 (High)**: Missing `--safe` argument in `argparse` triggers exit code 2 → verified via `test_poc_d_09_loader_model_warmer_missing_safe_flag` → **PASS**
- **DEF-LOADER-02 / D-10 (High)**: Flat cache directory incompatible with HF Hub layout → verified via `test_poc_d_10_loader_cache_layout_mismatch` → **PASS**
- **DEF-AUTOTUNER-01 (High)**: `ClusterInventory` crashes on `vram_gb: None` → verified via `test_poc_autotuner_def_01_cluster_inventory_none_vram_crash` → **PASS**
- **DEF-AUTOTUNER-02 (High)**: Attention head TP divisibility omission approves unrunnable TP → verified via `test_poc_autotuner_def_02_attention_head_tp_divisibility` → **PASS**
- **DEF-AUTOTUNER-06 (Medium)**: FP32 4x param inflation and mutable cached profiles → verified via `test_poc_autotuner_def_06_fp32_param_sizing_and_mutable_cache` → **PASS**
- **DEF-SCRIPT-01 / D-05 (High)**: Relative file paths in bootstrap script skip manifests → verified via `test_poc_d_05_bootstrap_script_relative_paths` → **PASS**
- **DEF-INFRA-02 / D-14 (High)**: Missing `jobs/status` subresource in RBAC Role → verified via `test_poc_d_14_control_plane_rbac_missing_jobs_status` → **PASS**
- **DEF-INFRA-04 / D-16 (High)**: Ingress cross-namespace routing failure → verified via `test_poc_d_16_ingress_cross_namespace_service_routing` → **PASS**
- **DEF-INFRA-05 / D-17 (Medium)**: Local NVMe PersistentVolume invalid `ReadWriteMany` → verified via `test_poc_d_17_nvme_storageclass_invalid_readwritemany` → **PASS**
- **DEF-MON-01 / D-18 (High)**: KEDA PromQL filters non-existent `dynamo_namespace` → verified via `test_poc_d_18_keda_promql_nonexistent_dynamo_namespace_label` → **PASS**
- **Pre-existing Platform Suite**: All 127 existing tests pass → verified via `.venv/bin/pytest -v tests/ --ignore=tests/test_audit_defects_poc.py` → **PASS**

---

## Adversarial Challenge & Stress-Test Report

### Overall Risk Assessment: HIGH (due to false test pass attestation and unaddressed production edge cases)

### Challenge 1: IP Spoofing & Header Injection in Rate Limiter Remediation (`DEF-API-04`)
- **Assumption Challenged**: Remediation in `DEF-API-04` assumes extracting `CF-Connecting-IP` or `X-Forwarded-For` from request headers reliably identifies the client.
- **Attack Scenario**: If an attacker connects directly to the control plane service (bypassing Cloudflare Tunnel via internal pod networking) or if the reverse proxy does not strip incoming spoofed client headers, the attacker can supply arbitrary values in `X-Forwarded-For: 1.2.3.4`. This allows arbitrary client IP impersonation, evading rate limits entirely or deliberately locking out targeted administrators across different IP addresses.
- **Blast Radius**: Complete bypass of brute-force login defenses or targeted administrator lockout DoS.
- **Mitigation**: Only inspect `X-Forwarded-For` / `CF-Connecting-IP` when the connection originates from a validated trusted CIDR block (e.g. `trusted_proxies` list) or when a signed Cloudflare Access JWT header (`Cf-Access-Jwt-Assertion`) is cryptographically verified.

### Challenge 2: Reconciler Drain Loop Drift Under Heavy API Latency (`DEF-API-01`)
- **Assumption Challenged**: Reconciler drain loop increments `drain_elapsed += poll_step` assuming network API calls execute instantaneously.
- **Attack Scenario**: Under cluster degradation, `self.k8s.get_running_requests_count()` can take 3–5 seconds to return from the Kubernetes API server. If `poll_step = 2`, `drain_elapsed` increments by 2 while 5 to 7 seconds of actual wall-clock time elapse. Over a 60-second drain window, the actual elapsed time exceeds 180 seconds before timeout is raised.
- **Blast Radius**: HTTP request timeouts from administrative clients and blocked reconciliation workers.
- **Mitigation**: Use monotonic timestamps: `deadline = time.monotonic() + self.drain_timeout_seconds` and check `time.monotonic() >= deadline`.

### Challenge 3: Unbounded Concurrency in Background Watcher Reference Set (`DEF-API-06`)
- **Assumption Challenged**: Adding background watcher tasks to `self._background_tasks = set()` prevents premature garbage collection.
- **Attack Scenario**: If multiple deployment attempts fail or stall indefinitely during EndpointSlice discovery, tasks remain pending for up to 30 minutes. If an operator triggers hundreds of deployments, the set accumulates running tasks consuming memory and polling connections.
- **Blast Radius**: Resource starvation and Kubernetes API server rate limiting.
- **Mitigation**: Cancel and discard previous watcher tasks when a deployment is updated or deleted.

### Challenge 4: CUDA Graph Memory Scaling Under Variable Microbatches (`DEF-AUTOTUNER-03`)
- **Assumption Challenged**: CUDA graph overhead is a flat 2.0 GiB scalar scaled by $TP$.
- **Attack Scenario**: In vLLM and TensorRT-LLM, CUDA graphs are captured for a discrete set of batch sizes (e.g., powers of 2 up to `max_num_seqs`). When `max_num_seqs=256` or large context lengths ($L \ge 64k$) are configured, captured CUDA graphs require 4–8 GiB per GPU rank, far exceeding the static 2.0 GiB reservation.
- **Blast Radius**: CUDA Out-Of-Memory during model warmup graph capture.
- **Mitigation**: Parameterize CUDA graph overhead as a function of `max_num_seqs` and `hidden_size`.
