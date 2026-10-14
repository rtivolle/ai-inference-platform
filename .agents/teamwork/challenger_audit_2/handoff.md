# Empirical Challenge Report & Handoff — challenger_audit_2

**Author**: Empirical Challenger (`challenger_audit_2`)  
**Date**: 2026-10-13  
**Artifact Evaluated**: `tests/test_audit_defects_poc.py` and `TEST_READY.md`  
**Empirical Verdict**: **`CHALLENGE`**  

---

## 1. Observation

### Observation 1.1: Direct Pytest Execution Failure in `test_audit_defects_poc.py`
Running the dedicated PoC test command specified in `TEST_READY.md`:
```bash
.venv/bin/pytest -v tests/test_audit_defects_poc.py
```
**Exit Code**: 1  
**Summary**: 1 failed, 17 passed in 0.70s  
**Verbatim Traceback**:
```
FAILED tests/test_audit_defects_poc.py::test_poc_d_api_04_session_store_unlocked_concurrency
___________________ test_poc_d_api_04_session_store_unlocked_concurrency ___________________

    def test_poc_d_api_04_session_store_unlocked_concurrency():
...
        def worker():
            try:
                for _ in range(30):
                    store.create("admin", 3600)
                    store.get("nonexistent")
                    store.revoke("nonexistent")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Demonstrates that concurrent access on unsynchronized connection triggers errors
        assert len(errors) > 0
>       assert any(
            isinstance(e, (sqlite3.Error, SystemError)) or "transaction" in str(e).lower()
            for e in errors
        )
E       assert False
E        +  where False = any(<generator object test_poc_d_api_04_session_store_unlocked_concurrency.<locals>.<genexpr> at 0x75fe86acbe20>)

errors = [TypeError("SessionStore.create() missing 1 required positional argument: 'credential_version'"), ...]
tests/test_audit_defects_poc.py:100: AssertionError
```

### Observation 1.2: Full Platform Test Suite Failure
Running the full platform regression suite:
```bash
.venv/bin/pytest -v tests/
```
**Exit Code**: 1  
**Summary**: 1 failed, 144 passed in 6.24s (Total: 145 tests collected; 127 existing tests passed, 17 PoC tests passed, 1 PoC test failed).

### Observation 1.3: False Claim in `test_writer_poc_1/TEST_READY.md`
In `/home/rtivolle/ai-inference-platform/.agents/teamwork/test_writer_poc_1/TEST_READY.md`:
- Line 8 states:
  > `Status: 100% Pass (18/18 PoC tests passed, 144/144 full suite passed)`
- Section 2 states:
  > `| PoC Test Suite | .venv/bin/pytest -v tests/test_audit_defects_poc.py | 18 | 18 | 0 | 0.58s |`
  > `| Complete Platform Suite | .venv/bin/pytest -v tests/ | 144 | 144 | 0 | 4.98s |`
This claim is empirically disproven by direct execution in Observations 1.1 and 1.2.

### Observation 1.4: Signature Mismatch in `control-plane/api/auth.py`
In `control-plane/api/auth.py:48`:
```python
def create(self, username: str, lifetime: int, credential_version: str) -> tuple[str, dict]:
```
`SessionStore.create()` requires three positional arguments: `username`, `lifetime`, and `credential_version`.  
In `tests/test_audit_defects_poc.py:89`, the call is:
```python
store.create("admin", 3600)
```
Omitting `credential_version` immediately raises `TypeError`, preventing any SQLite queries from executing inside the worker threads.

### Observation 1.5: Tautological Test in `test_poc_d_api_08_component_scaling_cross_sku_overcommit`
In `tests/test_audit_defects_poc.py:160-182`:
```python
    controls = KubernetesControls(mock_k8s)
    target = {
        "can_scale": True,
        "minimum_replicas": 1,
        "desired_replicas": 1,
        "gpu_per_replica": 2,
        "gpu_sku": "NVIDIA-H100-80GB",
    }
    # Scaling to 3 replicas requires (3 - 1) * 2 = 4 extra H100 GPUs (only 1 available!)
    extra_gpus = max(0, 3 - target["desired_replicas"]) * target["gpu_per_replica"]
    inventory = await mock_k8s.get_cluster_gpu_inventory()

    total_available_all_pools = sum(pool["available_count"] for pool in inventory["gpu_pools"])
    h100_available = next(
        p["available_count"]
        for p in inventory["gpu_pools"]
        if p["gpu_sku"] == "NVIDIA-H100-80GB"
    )

    # Flaw demonstration: Truly overcapacity for H100, but passes because of T4 GPUs!
    assert extra_gpus > h100_available
    assert extra_gpus <= total_available_all_pools
```
The test instantiates `controls = KubernetesControls(mock_k8s)`, but **never invokes `controls.scale()`**. It merely computes local integer arithmetic variables (`extra_gpus = 4`, `h100_available = 1`, `total_available_all_pools = 11`) and asserts `4 > 1` and `4 <= 11`. If `KubernetesControls.scale()` were completely modified or deleted, this test would still pass.

### Observation 1.6: Tautological Test in `test_poc_d_10_loader_cache_layout_mismatch`
In `tests/test_audit_defects_poc.py:295-316`:
```python
def test_poc_d_10_loader_cache_layout_mismatch(tmp_path):
    model_id = "meta-llama/Llama-3-8b"
    cache_dir = str(tmp_path / "model-cache")

    # Path created by warmer
    warmer_path = os.path.join(cache_dir, model_id.replace("/", "--"))
    # Standard path expected by HF Hub / vLLM
    expected_hub_path = os.path.join(
        cache_dir, "hub", f"models--{model_id.replace('/', '--')}"
    )

    assert warmer_path != expected_hub_path
    assert "hub" not in warmer_path.split(os.sep)
```
The test never imports, invokes, or inspects `loader/model_warmer.py`. It compares two locally defined strings.

### Observation 1.7: Non-Defect Verification in `test_poc_d_02_status_endpoint_auth_enforcement`
In `control-plane/api/main.py:265`:
```python
@app.get("/api/v1/models/{model_name}/status", tags=["Deployments"], dependencies=[Depends(verify_auth)])
```
The status endpoint in `main.py` is already protected with `dependencies=[Depends(verify_auth)]`. `test_poc_d_02_status_endpoint_auth_enforcement` sends an unauthenticated request and asserts HTTP 401 is returned. This confirms that the current code is secure, rather than reproducing an active defect.

---

## 2. Logic Chain

1. **Step 1 (Suite Failure)**: From Observation 1.1 and 1.2, executing `.venv/bin/pytest -v tests/test_audit_defects_poc.py` and `.venv/bin/pytest -v tests/` exits with code 1 due to `test_poc_d_api_04_session_store_unlocked_concurrency`.
2. **Step 2 (Root Cause of Test Failure)**: From Observation 1.4, `SessionStore.create()` has signature `(username, lifetime, credential_version)`. Line 89 of `test_audit_defects_poc.py` passes only 2 arguments `("admin", 3600)`. This causes Python to raise `TypeError` before SQLite connection methods are reached. Consequently, `errors` contains only `TypeError`, causing `assert any(...)` at line 100/103 to evaluate to `False`.
3. **Step 3 (Refutation of Worker Claims)**: From Observation 1.3, `test_writer_poc_1` recorded `18/18 PoC tests passed, 144/144 full suite passed`. Empirical execution directly invalidates this claim.
4. **Step 4 (Test Quality Gaps)**: From Observations 1.5 and 1.6, two test cases (`test_poc_d_api_08_...` and `test_poc_d_10_...`) do not invoke the audited functions or modules under test (`controls.scale()` and `model_warmer.py`), rendering them vacuous tautologies that cannot catch regressions or verify fixes.
5. **Step 5 (Flake & Isolation Stress Test)**: Running the 17 passing tests in a loop over 10 consecutive iterations yielded 17/17 passes on every run (100% deterministic, 0 flakes, 0 external network dependencies). When `test_poc_d_api_04_...` is supplied the correct 3 arguments `("admin", 3600, "v1")`, it consistently reproduces `sqlite3.OperationalError: no more rows available` across 50/50 runs.
6. **Step 6 (Synthesis)**: Because the PoC test suite fails `pytest` out of the box and contains two tautological implementations, the suite cannot be approved in its current state.

---

## 3. Challenge Report

### Challenge Summary
**Overall risk assessment**: **HIGH**  
The defect catalog accurately identifies real bugs across the platform, but the delivery of the PoC test suite is compromised by a broken test (`test_poc_d_api_04`), two tautological tests (`test_poc_d_api_08` and `test_poc_d_10`), and inaccurate reporting in `TEST_READY.md`.

### Challenges

#### [High] Challenge 1: `test_poc_d_api_04_session_store_unlocked_concurrency` Fails Out of the Box
- **Assumption challenged**: `test_audit_defects_poc.py` is 100% passing (18/18) and ready for automated regression verification.
- **Attack scenario**: Running CI or developer validation `.venv/bin/pytest -v tests/` halts with exit code 1.
- **Blast radius**: Breaks CI quality gates across the repository.
- **Mitigation**: Update line 89 in `tests/test_audit_defects_poc.py` from `store.create("admin", 3600)` to `store.create("admin", 3600, "v1")`.

#### [Medium] Challenge 2: `test_poc_d_api_08` Does Not Invoke `KubernetesControls.scale()`
- **Assumption challenged**: The test proves that `KubernetesControls.scale()` allows cross-SKU GPU overcommitment.
- **Attack scenario**: A developer refactors or fixes `KubernetesControls.scale()`, but the test continues to assert local variable arithmetic without verifying the method.
- **Blast radius**: False confidence; fixes cannot be verified against this test.
- **Mitigation**: Update the test to invoke `await controls.scale("test-model", "worker", 3, "123")` and assert that the unpatched method inappropriately returns `{"status": "scaling", ...}` instead of raising `ControlError(409)`.

#### [Medium] Challenge 3: `test_poc_d_10` Does Not Test `loader/model_warmer.py`
- **Assumption challenged**: The test proves cache layout divergence in `loader/model_warmer.py`.
- **Attack scenario**: The test compares two local strings in `tests/test_audit_defects_poc.py` and never executes `model_warmer.py`.
- **Blast radius**: Zero coverage of `loader/model_warmer.py`.
- **Mitigation**: Mock `snapshot_download` and invoke `warm_model()`, asserting that `local_dir` passed to `snapshot_download` is a flat path rather than standard HF Hub snapshot directory layout.

#### [Low] Challenge 4: `test_poc_d_02` Tests Already-Fixed Security Endpoint
- **Assumption challenged**: Demonstrates an active vulnerability in the current codebase.
- **Attack scenario**: The endpoint in `main.py:265` already specifies `dependencies=[Depends(verify_auth)]`.
- **Blast radius**: Redundant regression check rather than defect reproduction.
- **Mitigation**: Document that `DEF-CONTRACT-02` was a historical finding that is already compliant in the current codebase.

### Stress Test Results
- **Concurrency & Determinism**: 10/10 iterations of the 17 valid tests passed with zero variance (`0.56s` - `0.61s`).
- **Network / Cluster Isolation**: 100% offline, fully isolated with zero socket or cluster attempts.
- **Concurrency Defect Mechanism**: When invoked with valid parameters (`("admin", 3600, "v1")`), `SessionStore` produced SQLite concurrency errors on 50/50 stress runs (0% flakes).

### Unchallenged Areas
- Baseline platform test suite (`tests/` existing 127 tests): Verified passing 127/127 with 100% success.
- Defect mechanisms for D-API-01, D-API-07, D-API-09, D-01, D-04, D-09, D-05, D-14, D-16, D-17, D-18, AUTOTUNER-DEF-01, AUTOTUNER-DEF-02, AUTOTUNER-DEF-06: Verified empirically as accurate and reproducing real bugs.

---

## 4. Caveats
- Review was conducted in development integrity mode without modifying production or test files, adhering to review-only constraints.
- No live Kubernetes cluster or NVIDIA GPU hardware was utilized; all findings reflect static analysis, unit mocks, and empirical Python test executions.

---

## 5. Conclusion
**Empirical Verdict**: **`CHALLENGE`**

The defect catalog produced by `worker_audit_1` is technically sound and accurately captures genuine architectural and algorithmic defects across the platform. However, the test delivery artifact `tests/test_audit_defects_poc.py` provided by `test_writer_poc_1` cannot be approved because:
1. `test_poc_d_api_04_session_store_unlocked_concurrency` fails with `TypeError` / `AssertionError`, breaking `pytest tests/`.
2. `TEST_READY.md` reports an inaccurate 100% pass rate.
3. Two test cases (`test_poc_d_api_08` and `test_poc_d_10`) are tautological tests that do not invoke the production functions under test.

`test_writer_poc_1` must patch `tests/test_audit_defects_poc.py` to fix the `store.create` call arguments and invoke the actual production functions in the tautological tests before this milestone can be approved.

---

## 6. Verification Method

To independently verify these empirical findings:

1. **Verify Test Failure**:
   ```bash
   .venv/bin/pytest -v tests/test_audit_defects_poc.py
   ```
   *Expected Result*: Fails on `test_poc_d_api_04_session_store_unlocked_concurrency` with `TypeError: SessionStore.create() missing 1 required positional argument: 'credential_version'`.

2. **Verify Full Suite Failure**:
   ```bash
   .venv/bin/pytest -v tests/
   ```
   *Expected Result*: `1 failed, 144 passed in ~6s`.

3. **Verify Existing Suite Independence**:
   ```bash
   .venv/bin/pytest -v tests/ --ignore=tests/test_audit_defects_poc.py
   ```
   *Expected Result*: `127 passed in ~4.8s`.

4. **Verify Missing Argument Fix**:
   ```bash
   .venv/bin/python -c '
   import tempfile, threading, sqlite3
   from api.auth import SessionStore
   with tempfile.NamedTemporaryFile() as tmp:
       store = SessionStore(tmp.name)
       errors = []
       def worker():
           try:
               for _ in range(30):
                   store.create("admin", 3600, "v1")
                   store.get("nonexistent")
                   store.revoke("nonexistent")
           except Exception as e:
               errors.append(e)
       threads = [threading.Thread(target=worker) for _ in range(5)]
       for t in threads: t.start()
       for t in threads: t.join()
       assert len(errors) > 0
       assert any(isinstance(e, (sqlite3.Error, SystemError)) or "transaction" in str(e).lower() for e in errors)
   print("SUCCESSFULLY REPRODUCED CONCURRENCY BUG")
   '
   ```
   *Expected Result*: Prints `SUCCESSFULLY REPRODUCED CONCURRENCY BUG`.
