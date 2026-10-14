# Handoff Report: Explorer Survey 2 (Auto-Tuner & Test Suite)

**Agent**: Explorer Survey 2  
**Working Directory**: `/home/rtivolle/ai-inference-platform/.agents/teamwork/explorer_survey_2`  
**Parent Orchestrator**: `fbda79c3-ec32-408a-a403-9cabb555b0a1`  
**Milestone**: Survey Phase Completed  
**Handoff Type**: Hard (Task Complete)

---

## 1. Observation

1. **Autotuner Module Inventory**:
   - `control-plane/autotuner/__init__.py` (16 lines): exports `HFInspector`, `ModelArchitectureSpecs`, `SLAPlanner`, `VRAMCalculator`, `compute_deployment_plan`.
   - `control-plane/autotuner/hf_inspector.py` (196 lines): contains `VERIFIED_MODELS` (6 models: Llama-3.3-70B, Meta-Llama-3-70B, Meta-Llama-3-8B, Mistral-7B-v0.3, Qwen-2.5-72B, Llama-3.1-405B), `normalize_dtype()`, `HFInspector.inspect()`, and `HFInspector._parse_config()`.
   - `control-plane/autotuner/vram_calculator.py` (177 lines): defines `DTYPE_BYTES`, `ModelArchitectureSpecs`, `InsufficientCapacityError`, `VRAMCalculator.compute_weights_memory()`, `compute_kv_cache_per_token()`, `compute_total_vram()`, `compute_parallelism()`, `can_decode_reduce_tp()`.
   - `control-plane/autotuner/sla_planner.py` (225 lines): defines `sanitize_deployment_name()`, `DeployRequest`, `GpuPool`, `ClusterInventory`, `SLAPlanner.plan()`, and `compute_deployment_plan()`.

2. **Test Suite Inventory**:
   - Command: `.venv/bin/pytest -v tests/`
   - Output: `101 passed in 2.74s` (101 test cases across 11 files: `conftest.py`, `e2e_deployment_flow.py`, `test_auth.py`, `test_cluster_deployment.py`, `test_cluster_inventory.py`, `test_crd_rendering.py`, `test_kubernetes_controls.py`, `test_litellm_install.py`, `test_reconciliation.py`, `test_usage_metrics.py`, `test_vram_calculator.py`, `test_web_console.py`).
   - Fixtures in `tests/conftest.py`: `vram_calc`, `llama_70b_specs`, `llama_8b_specs`, `mock_k8s`, `mock_litellm`, `reconciler`, `api_client`.

3. **Verbatim Code Observations**:
   - `control-plane/autotuner/sla_planner.py:54`:
     ```python
     class GpuPool(BaseModel):
         gpu_sku: str = "NVIDIA-H100-80GB"
         vram_gb: float = 80.0
     ```
   - `control-plane/api/k8s_client.py:395-397`:
     ```python
     try:
         memory = round(float(memory_label) / 1024, 1) if memory_label else None
     except ValueError:
         memory = None
     ```
   - Execution of `ClusterInventory(gpu_pools=[{'gpu_sku': 'H100', 'vram_gb': None, 'total_count': 1, 'available_count': 1}])`:
     ```
     pydantic_core._pydantic_core.ValidationError: 1 validation error for ClusterInventory
     gpu_pools.0.vram_gb
       Input should be a valid number [type=float_type, input_value=None, input_type=NoneType]
     ```
   - `control-plane/autotuner/vram_calculator.py:133`:
     ```python
     if (tp * gpu_vram_bytes >= m_total_bytes) and (specs.num_key_value_heads % tp == 0):
     ```
     Execution on model with `num_attention_heads=12` and `num_key_value_heads=8` requiring TP=8:
     ```
     Selected TP=8, PP=1. Divisible by attention heads (12 % 8)? False
     ```
   - `control-plane/autotuner/vram_calculator.py:55, 99`:
     ```python
     self.cuda_graphs_bytes = 2.0 * (1024**3)
     ...
     m_total = (m_weights + m_kv_total + self.cuda_graphs_bytes) / utilization
     ```
   - `control-plane/autotuner/hf_inspector.py:163`:
     ```python
     bytes_per_param = 2.0 if normalized_dtype in ("bf16", "fp16") else 1.0
     total_params = int(total_size_bytes / bytes_per_param)
     ```
   - `control-plane/autotuner/sla_planner.py:161`:
     ```python
     if self.calculator.can_decode_reduce_tp(
         ...,
         concurrency=8,
     ):
         decode_tp = candidate_decode_tp
     ```

---

## 2. Logic Chain

1. **Step 1 (GPU Profile Mapping & Validation Crash)**:
   - Observation 3 shows `k8s_client.py:395` emits `memory = None` when node labels omit `nvidia.com/gpu.memory`.
   - `tests/test_cluster_inventory.py:33` directly tests that missing VRAM is returned as `None`.
   - `GpuPool.vram_gb` in `sla_planner.py:54` rejects `None` with `ValidationError`.
   - In `main.py:160`, `ClusterInventory(**inventory_dict)` crashes immediately with `ValidationError`.
   - In `sla_planner.py:81`, `pool.available_count * pool.vram_gb` crashes with `TypeError` when `vram_gb` is `None`.
   - *Inference*: The platform lacks a GPU profile mapping dictionary to resolve VRAM for known GPU models when node labels are absent, causing deployment failures on real Kubernetes clusters.

2. **Step 2 (Tensor Parallel Attention Head Divisibility)**:
   - Observation 3 shows line 133 only verifies `specs.num_key_value_heads % tp == 0`.
   - Megatron-LM and vLLM attention projection sharding partitions query attention heads as `num_attention_heads / TP`.
   - Observation 3 confirmed that for a model with 12 query attention heads and 8 KV heads, `compute_parallelism` selects $TP=8$.
   - *Inference*: When scheduled on vLLM, workers crash on startup because 12 is not divisible by 8. Line 133, 140, and 176 must check both `num_key_value_heads % tp == 0` and `num_attention_heads % tp == 0`.

3. **Step 3 (CUDA Graphs Overhead Scaling)**:
   - Observation 3 shows `compute_total_vram` adds a single scalar `self.cuda_graphs_bytes = 2.0 * 1024^3` to `m_total`.
   - Sizing checks capacity as `tp * gpu_vram_bytes >= m_total_bytes`.
   - For $TP=8$, this allocates $2.0 / 8 = 0.25\text{ GiB}$ per GPU.
   - *Inference*: CUDA graph runners run per GPU worker process. Each worker requires ~1.5 to 2.0 GiB of CUDA graphs space. Aggregate sizing under-allocates up to 14 GiB of VRAM across the node.

4. **Step 4 (Decode TP Reduction Concurrency Mismatch)**:
   - Observation 3 shows line 161 evaluates `can_decode_reduce_tp` with hardcoded `concurrency=8`.
   - Line 204 configures decode clique with `max_num_seqs: 256`, and the client requests `target_concurrency` (default 16).
   - *Inference*: A decode clique reduced to a lower TP that only fits 8 concurrent sequences will experience CUDA OOM under real traffic loads.

5. **Step 5 (Metadata Parsing Flaws)**:
   - Observation 3 shows line 163 assigns `bytes_per_param = 1.0` for all dtypes not in `("bf16", "fp16")`.
   - For an FP32 model (`normalized_dtype == "fp32"`), `bytes_per_param` becomes 1.0 instead of 4.0.
   - *Inference*: Total parameter count is inflated by $4\times$, causing premature capacity rejection.

---

## 3. Caveats

- **Network Dependency of Hugging Face Hub**: Live inspection of non-verified models relies on `huggingface_hub.hf_hub_download`, which requires network access and an optional `HF_TOKEN`. The platform provides 6 verified benchmark models that bypass network I/O.
- **Upstream vLLM CUDA Graphs**: While CUDA graph memory is allocated per worker process in standard vLLM, certain shared-memory configurations or multi-process PyTorch allocators may share small portions of pinned buffers. However, reserving 2.0 GiB per GPU is the safe industry standard.
- **No Direct Source Mutations**: Per team rules, all findings are documented in reports and handoffs without altering source files.

---

## 4. Conclusion

The `control-plane/autotuner/` engine implements the core mathematical formulas for GQA/MLA KV-cache and weights sizing, and the test suite is functional with 101 passing tests. However, 8 actionable defects and discrepancies were identified:
1. `ClusterInventory` crashes on `vram_gb=None` due to missing GPU profile mapping.
2. Tensor parallel selection lacks query attention head divisibility checking.
3. CUDA graph memory overhead does not scale per GPU rank.
4. Pipeline parallelism restricts TP to $[4, 8]$ and omits layer divisibility validation.
5. Decode TP reduction uses a hardcoded concurrency of 8, risking OOM.
6. FP32 safetensors parameter count is inflated $4\times$, and `VERIFIED_MODELS` returns mutable references.
7. `DeployRequest` boundary checks do not match `contracts/model_spec.schema.json`.
8. Dedicated unit tests for `hf_inspector.py` and `sla_planner.py` are missing.

Detailed defect analysis and remediation specifications are documented in `/home/rtivolle/ai-inference-platform/.agents/teamwork/explorer_survey_2/report.md`.

---

## 5. Verification Method

To independently verify all findings and test suite integrity:

1. **Verify Full Existing Test Suite**:
   ```bash
   cd /home/rtivolle/ai-inference-platform
   .venv/bin/pytest -v tests/
   ```
   *Expected Result*: 101 passed in ~2.7s.

2. **Verify Finding AUTOTUNER-DEF-01 (Pydantic Crash on vram_gb=None)**:
   ```bash
   .venv/bin/python -c "
   from autotuner.sla_planner import ClusterInventory
   ClusterInventory(gpu_pools=[{'gpu_sku': 'H100', 'vram_gb': None, 'total_count': 1, 'available_count': 1}])
   "
   ```
   *Expected Result*: Raises `pydantic.ValidationError` on `vram_gb`.

3. **Verify Finding AUTOTUNER-DEF-02 (Attention Head Divisibility Bug)**:
   ```bash
   .venv/bin/python -c "
   from autotuner.vram_calculator import VRAMCalculator, ModelArchitectureSpecs
   calc = VRAMCalculator()
   specs = ModelArchitectureSpecs(num_params=130_000_000_000, num_hidden_layers=80, hidden_size=8192, num_attention_heads=12, num_key_value_heads=8, head_dim=128)
   tp, pp = calc.compute_parallelism(specs, gpu_vram_gb=80.0, available_gpus=8)
   assert tp == 8 and (specs.num_attention_heads % tp != 0)
   print('Defect confirmed: Selected TP=8 for 12 attention heads!')
   "
   ```
   *Expected Result*: Prints confirmation that TP=8 was selected despite $12 \pmod 8 \ne 0$.

4. **Verify Finding AUTOTUNER-DEF-06 (FP32 4x Parameter Inflation)**:
   ```bash
   .venv/bin/python -c "
   from autotuner.hf_inspector import HFInspector
   inspector = HFInspector()
   specs = inspector._parse_config({'torch_dtype': 'float32'}, total_size_bytes=100_000_000_000)
   print(f'Computed params: {specs.num_params / 1e9}B (expected 25B)')
   "
   ```
   *Expected Result*: Prints `Computed params: 100.0B (expected 25B)`.
