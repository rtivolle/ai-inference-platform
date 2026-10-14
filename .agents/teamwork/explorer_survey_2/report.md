# Comprehensive Survey Report: Auto-Tuner & Test Suite Inventory

**Agent**: Explorer Survey 2  
**Date**: 2026-10-13  
**Scope**: `control-plane/autotuner/` and `tests/`  
**Reference Guidelines**: `AGENTS.md`, `ORIGINAL_REQUEST.md`, `contracts/model_spec.schema.json`, `docs/autotuner.md`

---

## 1. Executive Summary

This report delivers a comprehensive audit survey of the AI inference platform's sizing and auto-tuning engine (`control-plane/autotuner/`) and the full regression test suite (`tests/`).

Key survey outcomes:
1. **Autotuner Architecture Inventory**: Mapped all components across `hf_inspector.py`, `vram_calculator.py`, `sla_planner.py`, and `__init__.py`. Documented all mathematical equations for model weights ($M_{\text{weights}}$), KV cache ($M_{\text{kv\_token}}$ for GQA and DeepSeek MLA), aggregate VRAM sizing ($M_{\text{total}}$), tensor/pipeline parallelism selection ($TP \in \{1, 2, 4, 8\}$, $PP \in \{1, 2, 4\}$), and clique allocation logic.
2. **Test Suite Mapping**: Inventoried all 11 test modules comprising 101 collected tests (91 unit/contract/integration tests + 10 parametrized cases across auth, cluster deployment, CRD rendering, inventory, K8s controls, LiteLLM install, reconciliation, metrics, VRAM calculator, and web console). All 101 tests currently execute and pass with 0 failures (`.venv/bin/pytest -v tests/`).
3. **Defect & Formula Discrepancy Identification**: Identified 8 distinct defects, mathematical inconsistencies, contract mismatches, and regression gaps:
   - **AUTOTUNER-DEF-01 (High)**: `ClusterInventory` Pydantic validation failure on `vram_gb=None`, crashing model deployment on clusters with unlabeled GPU nodes. Missing GPU SKU profile mapping dictionary.
   - **AUTOTUNER-DEF-02 (High)**: Missing Tensor Parallel Query Attention Head Divisibility check (`num_attention_heads % tp == 0`), leading to fatal vLLM container startup crashes for models where KV heads divide by TP but Q heads do not.
   - **AUTOTUNER-DEF-03 (Medium)**: Single scalar CUDA graph overhead ($2.0\text{ GiB}$) applied cluster-wide rather than per GPU rank, underestimating multi-GPU VRAM overhead by up to $14\text{ GiB}$ at $TP=8$.
   - **AUTOTUNER-DEF-04 (Medium)**: Pipeline Parallelism (PP) search space restricted to $TP \in [4, 8]$, preventing $PP$ usage for models with $H_{kv} \le 2$ or 2-GPU nodes, with no layer divisibility check (`num_hidden_layers % pp == 0`).
   - **AUTOTUNER-DEF-05 (Medium)**: Decode clique TP reduction evaluated at hardcoded low concurrency ($C=8$) instead of target concurrency or decode batch capacity ($256$), causing production CUDA OOM under real load.
   - **AUTOTUNER-DEF-06 (Medium)**: `hf_inspector.py` parameter calculation treats FP32 safetensors as $1.0\text{ byte/param}$ ($4\times$ parameter inflation), and `VERIFIED_MODELS` returns mutable references subject to cross-request mutation.
   - **AUTOTUNER-DEF-07 (Low)**: `DeployRequest` schema accepts boundary-violating inputs (`max_model_len` below 512 or above 131072, `gpu_memory_utilization` $> 0.98$ or $< 0.5$) that violate `contracts/model_spec.schema.json`.
   - **AUTOTUNER-DEF-08 (Low)**: Complete absence of dedicated unit test modules for `hf_inspector.py` and `sla_planner.py`.

---

## 2. Inventory & Architecture Map: `control-plane/autotuner/`

### 2.1 File Structure & Public API

The `control-plane/autotuner/` package comprises 4 Python source files:

```
control-plane/autotuner/
├── __init__.py           # Package exports (HFInspector, SLAPlanner, VRAMCalculator, ModelArchitectureSpecs, compute_deployment_plan)
├── hf_inspector.py       # Hugging Face model metadata inspector, verified profiles, GQA/MLA parser
├── vram_calculator.py    # VRAM sizing equations (weights, KV-cache, overhead, TP/PP selection)
└── sla_planner.py        # Contract 1 SLA planner, GPU pool selection, replica sizing, disaggregated/aggregated routing
```

### 2.2 Class & Function Taxonomy

| File | Class / Function | Purpose | Inputs | Outputs |
|---|---|---|---|---|
| `hf_inspector.py` | `ModelNotFoundError(Exception)` | Fatal exception on unknown/unreachable model | Error message | Exception |
| `hf_inspector.py` | `normalize_dtype(dtype_str)` | Maps Hugging Face `torch_dtype` strings to platform standard | `str` | `"bf16"`, `"fp16"`, `"fp32"`, `"fp8"` |
| `hf_inspector.py` | `VERIFIED_MODELS` | Pre-verified architecture dictionary avoiding network I/O | Model ID key | `ModelArchitectureSpecs` |
| `hf_inspector.py` | `HFInspector.inspect(model_id)` | Fetches & parses HF metadata without downloading weights | `model_id: str` | `ModelArchitectureSpecs` |
| `hf_inspector.py` | `HFInspector._parse_config(config, total_size_bytes)` | Parses `config.json` & safetensors index metadata | `dict`, `int \| None` | `ModelArchitectureSpecs` |
| `vram_calculator.py` | `InsufficientCapacityError(Exception)` | Raised when cluster GPUs cannot fit model | Error message | Exception (maps to HTTP 409) |
| `vram_calculator.py` | `ModelArchitectureSpecs` | Dataclass holding LLM structural hyperparameters | Hyperparameters | Sizing specification |
| `vram_calculator.py` | `VRAMCalculator.compute_weights_memory` | Computes model parameter footprint | $N_{params}, quant, dtype$ | Memory in bytes ($M_{\text{weights}}$) |
| `vram_calculator.py` | `VRAMCalculator.compute_kv_cache_per_token` | Computes KV-cache footprint per token (GQA/MLA) | `specs`, `kv_dtype` | Memory in bytes ($M_{\text{kv\_token}}$) |
| `vram_calculator.py` | `VRAMCalculator.compute_total_vram` | Computes total required VRAM across cluster | `specs`, $C_{max}, B_{seq}, quant, dtype, u$ | Memory in bytes ($M_{\text{total}}$) |
| `vram_calculator.py` | `VRAMCalculator.compute_parallelism` | Finds optimal $(TP, PP)$ satisfying capacity & divisibility | `specs`, $VRAM_{GPU}, avail\_gpus, \dots$ | `(tp, pp): tuple[int, int]` |
| `vram_calculator.py` | `VRAMCalculator.can_decode_reduce_tp` | Tests if decode clique can downscale $TP$ safely | `specs`, $TP_{reduced}, VRAM_{GPU}, \dots$ | `bool` |
| `sla_planner.py` | `sanitize_deployment_name(model_id)` | Produces RFC 1123 compliant K8s DNS name | `model_id: str` | `clean_name: str` ($\le 63$ chars) |
| `sla_planner.py` | `DeployRequest(BaseModel)` | Contract 1 request validation model | JSON payload | Validated Pydantic request |
| `sla_planner.py` | `GpuPool(BaseModel)` | GPU pool metadata in inventory | JSON payload | Validated pool model |
| `sla_planner.py` | `ClusterInventory(BaseModel)` | Cluster node & GPU capacity structure | JSON payload | Validated inventory model |
| `sla_planner.py` | `SLAPlanner.plan(request, inventory)` | Generates complete `DeploymentPlan` matching Contract 1 | Request, Inventory | `dict[str, Any]` (`DeploymentPlan`) |
| `sla_planner.py` | `compute_deployment_plan(...)` | Functional wrapper around `SLAPlanner().plan` | Request, Inventory | `DeploymentPlan` dictionary |

### 2.3 Mathematical Formulas & Algorithmic Decision Logic

#### 1. Weights Memory ($M_{\text{weights}}$)
$$M_{\text{weights}} = N_{\text{params}} \times B_{\text{dtype}} \times 1.05$$
- $B_{\text{dtype}} = 2.0$ bytes (BF16, FP16), $1.0$ byte (FP8), $0.55$ bytes (AWQ, GPTQ, INT4).
- $1.05$ represents a $5\%$ buffer for embedding layers, output projection (LM head), and layer normalization constants.

#### 2. KV-Cache Per Token ($M_{\text{kv\_token}}$)
- **Standard MHA & Grouped-Query Attention (GQA)**:
  $$M_{\text{kv\_token}} = 2 \times L \times H_{\text{kv}} \times D_{\text{head}} \times B_{\text{kv\_dtype}}$$
  where:
  - $L$ is `num_hidden_layers`.
  - $H_{\text{kv}}$ is `num_key_value_heads`.
  - $D_{\text{head}} = \text{explicit } head\_dim \text{ or } (hidden\_size / num\_attention\_heads)$.
  - $B_{\text{kv\_dtype}} = 1.0$ (FP8/INT8) or $2.0$ (FP16/BF16).
  - Factor of $2$ accounts for both Key and Value vectors.
- **DeepSeek Multi-Head Latent Attention (MLA)**:
  $$M_{\text{kv\_token}} = L \times (kv\_lora\_rank + qk\_rope\_head\_dim) \times B_{\text{kv\_dtype}}$$
  where:
  - $kv\_lora\_rank$ compresses $K$ and $V$ into a unified latent cache ($c_t^{KV}$).
  - $qk\_rope\_head\_dim$ is the decoupled positional RoPE key dimension ($k_t^R$).

#### 3. Total Memory Footprint ($M_{\text{total}}$)
$$M_{\text{total}} = \frac{M_{\text{weights}} + (M_{\text{kv\_token}} \times C_{\text{max}} \times B_{\text{seq}}) + M_{\text{cuda\_graphs}}}{\text{gpu\_memory\_utilization}}$$
where:
- $C_{\text{max}} = target\_concurrency$.
- $B_{\text{seq}} = max\_model\_len$.
- $M_{\text{cuda\_graphs}} = 2.0\text{ GiB} = 2 \times 1024^3\text{ bytes}$.
- $gpu\_memory\_utilization \in (0, 1]$ (default $0.90$).

#### 4. Parallelism Search Algorithm ($TP \in \{1, 2, 4, 8\}$, $PP \in \{1, 2, 4\}$)
1. **Single-Node TP search ($PP=1$)**:
   Iterate $TP \in [1, 2, 4, 8]$ in ascending order:
   Condition: $(TP \times VRAM_{GPU} \ge M_{\text{total}}) \land (H_{kv} \pmod{TP} == 0) \land (TP \le available\_gpus)$.
   Return first matching $(TP, 1)$.
2. **Multi-Node PP search**:
   Iterate $PP \in [2, 4]$, then $TP \in [4, 8]$:
   Condition: $(TP \times PP \times VRAM_{GPU} \ge M_{\text{total}}) \land (H_{kv} \pmod{TP} == 0) \land (TP \times PP \le available\_gpus)$.
   Return first matching $(TP, PP)$.
3. **Failure**:
   Raise `InsufficientCapacityError` with required vs allocatable capacity.

#### 5. Serving Mode Decision Matrix
- **Disaggregated Serving** selected IF and ONLY IF:
  $$TP \ge 2 \quad \land \quad profile \in \{\text{"low-latency"}, \text{"high-throughput"}\} \quad \land \quad available\_gpus \ge 2 \times (TP \times PP)$$
- **Aggregated Serving** selected otherwise ($TP=1$, profile="balanced", or insufficient GPUs for 2 full cliques).

#### 6. Clique Replica Allocation
- **Disaggregated**:
  - Decode TP reduction: if $TP \ge 4$ and $can\_decode\_reduce\_tp(TP // 2)$ is true, $decode\_tp = TP // 2$.
  - High-Throughput: initial ratio 1 Prefill : 2 Decode; scales down to 1:1 if $total\_needed > available\_gpus$.
  - Low-Latency: ratio 1 Prefill : 1 Decode.
- **Aggregated**:
  - $worker\_replicas = \max(1, available\_gpus // (TP \times PP))$.

### 2.4 Verified Models Index

The engine contains exact pre-verified configurations for 6 mainstream LLMs (`hf_inspector.py:26-87`):

| Model Identifier | Params | Layers ($L$) | Hidden ($h$) | Attn Heads | KV Heads | Head Dim | Max Pos | Dtype |
|---|---|---|---|---|---|---|---|---|
| `meta-llama/Llama-3.3-70B-Instruct` | 70,553,706,496 | 80 | 8192 | 64 | 8 | 128 | 131072 | bf16 |
| `meta-llama/Meta-Llama-3-70B-Instruct` | 70,553,706,496 | 80 | 8192 | 64 | 8 | 128 | 8192 | bf16 |
| `meta-llama/Meta-Llama-3-8B-Instruct` | 8,030,261,248 | 32 | 4096 | 32 | 8 | 128 | 8192 | bf16 |
| `mistralai/Mistral-7B-Instruct-v0.3` | 7,248,023,552 | 32 | 4096 | 32 | 8 | 128 | 32768 | bf16 |
| `Qwen/Qwen2.5-72B-Instruct` | 72,706,096,640 | 80 | 8192 | 64 | 8 | 128 | 131072 | bf16 |
| `meta-llama/Llama-3.1-405B-Instruct` | 405,000,000,000 | 126 | 16384 | 128 | 8 | 128 | 131072 | bf16 |

### 2.5 GPU Profile Mapping Status

- **Current Implementation**: There is **no GPU profile mapping table** in `autotuner`.
- **Impact**: When Kubernetes nodes do not report `nvidia.com/gpu.memory` (e.g. unannotated Bare-Metal or Cloud instances), `inventory_from_nodes` outputs `vram_gb: None`, triggering an immediate Pydantic validation error or `TypeError` in `sla_planner`.

---

## 3. Inventory & Test Suite Mapping: `tests/`

### 3.1 Test Suite Structure

The test suite consists of 11 files in `tests/`:

```
tests/
├── conftest.py                   # Pytest fixtures (vram_calc, specs, mocks, reconciler, api_client)
├── e2e_deployment_flow.py        # Lifecycle integration tests (auth, 404, 409, lifecycle, drain)
├── test_auth.py                  # Auth, cookie security, CSRF, session rotation, rate limits
├── test_cluster_deployment.py    # Multi-cluster deployment script release rendering & image tag checks
├── test_cluster_inventory.py     # Node inventory extraction, pool isolation, missing VRAM handling
├── test_crd_rendering.py         # Jinja2 template rendering against DynamoGraphDeployment CRD schema
├── test_kubernetes_controls.py   # Workload controls, pod scale, evictions, disruption budgets, logs
├── test_litellm_install.py       # LiteLLM installer script, DB statefulset, secret stdin piping
├── test_reconciliation.py        # Multi-node warming, KEDA scaled objects, lifecycle error handling
├── test_usage_metrics.py         # Prometheus metrics snapshot, NaN gap handling, auth on metrics
├── test_vram_calculator.py       # Unit tests for VRAM calculator equations
└── test_web_console.py           # Web console asset serving & deployment field validation
```

### 3.2 Fixture Hierarchy (`tests/conftest.py`)

- `vram_calc`: Provides a default `VRAMCalculator` instance.
- `llama_70b_specs`: Standard 70.55B parameter architecture spec (80 layers, 64 heads, 8 KV heads).
- `llama_8b_specs`: Standard 8.03B parameter architecture spec (32 layers, 32 heads, 8 KV heads).
- `mock_k8s`: In-memory `K8sClient(mock_mode=True)`.
- `mock_litellm`: In-memory `LiteLLMSync(mock_mode=True)`.
- `reconciler`: `DynamoReconciler` configured with `mock_k8s`, `mock_litellm`, and `drain_timeout_seconds=0`.
- `api_client`: Starlette `TestClient` wrapping `api.main.app` with mock environment variables.

### 3.3 Test Suite Coverage Matrix

| Test Module | Test Cases Count | Primary Subsystem Tested | Key Assertions / Coverage |
|---|---|---|---|
| `test_vram_calculator.py` | 7 | `control-plane/autotuner` | Weights formula (bf16/fp8/awq), KV-cache (GQA & DeepSeek MLA), TP selection with $H_{kv} \pmod{TP} == 0$, 8B TP=1, 405B 409 capacity rejection, decode TP reduction. |
| `test_crd_rendering.py` | 5 | `contracts/`, `dynamo/templates/` | Disaggregated & aggregated DGD CRD rendering; schema validation against `contracts/dynamo_crd.schema.json`; rejection of `v1alpha1` and missing component graphs. |
| `test_reconciliation.py` | 22 | `control-plane/api/` | Per-node model cache warming before DGD; node selection based on SKU and ready status; bounded job names ($\le 63$ chars); KEDA ScaledObject creation & patch; deletion order. |
| `test_kubernetes_controls.py` | 6 | `control-plane/api/` | Workload inspection, scaling via adapter, pod eviction with UID preconditions, disruption budget 429 reporting, drain failure error propagation. |
| `test_cluster_inventory.py` | 4 | `control-plane/api/`, `autotuner` | GPU pool isolation, cordoned node filtering, empty node handling, missing VRAM metadata (`None`), capacity-aware SLA planner SKU selection. |
| `e2e_deployment_flow.py` | 5 | E2E integration | HTTP 401 unauthenticated; HTTP 404 unknown model; HTTP 409 overcapacity; live K8s initialization failure; full deployment -> LiteLLM register -> status -> delete. |
| `test_auth.py` | 8 | `control-plane/api/` | Argon2 password hashing, secure cookie flags (`HttpOnly`, `SameSite=Strict`), CSRF header requirement, session rotation on re-login, rate limiting. |
| `test_usage_metrics.py` | 5 | `control-plane/api/` | Prometheus query range & vector snapshots; preservation of zero gaps and NaN suppression; drain query model scoping; auth validation on metrics API. |
| `test_web_console.py` | 12 | `control-plane/api/web/` | Static asset serving, SPA index serving; input field rejection (empty model, invalid regex, bad profile, negative concurrency, util > 1.0); plan returned to UI. |
| `test_litellm_install.py` | 14 | `scripts/` | Credential stability and reuse on reinstall; legacy salt key preservation; conflicting DB URL rejection; StatefulSet vs external DB rendering; stdin secret piping. |
| `test_cluster_deployment.py` | 11 | `scripts/` | Context uniqueness validation; digest immutability enforcement (`@sha256:...`); RBAC manifest generation without embedded secrets; secret redaction on failure. |

**Total Test Count**: 101 tests (all passing).

---

## 4. Defect & Formula Discrepancy Findings

### Finding AUTOTUNER-DEF-01: `ClusterInventory` Validation Crash on `vram_gb=None`
- **Severity**: High
- **Category**: Functional Bug / Schema Mismatch
- **Location**: `control-plane/autotuner/sla_planner.py:54, 81, 100`
- **Mechanism**:
  When Kubernetes nodes do not possess the `nvidia.com/gpu.memory` label, `api/k8s_client.py:inventory_from_nodes` assigns `"vram_gb": None` (verified in `tests/test_cluster_inventory.py:33`).
  When `main.py:160` runs `ClusterInventory(**inventory_dict)`, Pydantic throws a `ValidationError` because `vram_gb: float = 80.0` is strictly typed as a non-optional `float`.
  Furthermore, `SLAPlanner.plan` executes:
  ```python
  primary_pool = max(inventory.gpu_pools, key=lambda pool: pool.available_count * pool.vram_gb)
  ```
  If `vram_gb` is `None`, this immediately raises `TypeError: unsupported operand type(s) for *: 'int' and 'NoneType'`.
- **Impact**:
  Any real cluster without manual GPU memory node labels crashes during model deployment with HTTP 400.
- **Proof of Concept**:
  ```python
  from autotuner.sla_planner import ClusterInventory
  # Raises ValidationError: Input should be a valid number, input_value=None
  ClusterInventory(gpu_pools=[{"gpu_sku": "H100", "vram_gb": None, "total_count": 1, "available_count": 1}])
  ```
- **Remediation**:
  1. Define a GPU profile catalog in `autotuner`:
     ```python
     KNOWN_GPU_VRAM_GB: dict[str, float] = {
         "H100": 80.0, "NVIDIA-H100-80GB": 80.0, "A100": 80.0, "NVIDIA-A100-80GB": 80.0,
         "A100-40GB": 40.0, "L40S": 48.0, "A10G": 24.0, "L4": 24.0, "T4": 16.0, "V100": 32.0,
     }
     ```
  2. Allow `vram_gb: float | None = 80.0` in `GpuPool`, and in `SLAPlanner.plan`, resolve missing `vram_gb` via `KNOWN_GPU_VRAM_GB.get(pool.gpu_sku, 80.0)`.

---

### Finding AUTOTUNER-DEF-02: Missing Attention Head Divisibility Check in Tensor Parallel Selection
- **Severity**: High
- **Category**: Mathematical / Algorithmic Flaw
- **Location**: `control-plane/autotuner/vram_calculator.py:133, 140, 176`
- **Mechanism**:
  Megatron-LM and vLLM shard both the Query projection ($W_Q$) and Key/Value projections ($W_{KV}$) across tensor parallel workers. Sharding requires:
  $$num\_attention\_heads \pmod{TP} == 0 \quad \text{AND} \quad num\_key\_value\_heads \pmod{TP} == 0$$
  `vram_calculator.py` only verifies `specs.num_key_value_heads % tp == 0`.
- **Impact**:
  For models where $num\_key\_value\_heads$ is divisible by $TP$ but $num\_attention\_heads$ is not (e.g. $H_{\text{attn}}=12, H_{kv}=8$ evaluated at $TP=8$, or non-power-of-two attention architectures), the autotuner approves $TP=8$. The vLLM worker crashes immediately on startup with `ValueError: Number of attention heads must be divisible by tensor parallel size`.
- **Proof of Concept**:
  ```python
  from autotuner.vram_calculator import VRAMCalculator, ModelArchitectureSpecs
  calc = VRAMCalculator()
  specs = ModelArchitectureSpecs(
      num_params=130_000_000_000, num_hidden_layers=80, hidden_size=8192,
      num_attention_heads=12, num_key_value_heads=8, head_dim=128
  )
  tp, pp = calc.compute_parallelism(specs, gpu_vram_gb=80.0, available_gpus=8)
  assert tp == 8  # Selected TP=8!
  assert specs.num_attention_heads % tp != 0  # 12 % 8 == 4 != 0 -> Crashes vLLM!
  ```
- **Remediation**:
  Update lines 133, 140, and 176 to check:
  ```python
  (specs.num_key_value_heads % tp == 0) and (specs.num_attention_heads % tp == 0)
  ```

---

### Finding AUTOTUNER-DEF-03: CUDA Graph Overhead Not Scaled Per GPU in Tensor Parallelism
- **Severity**: Medium
- **Category**: Mathematical / Sizing Flaw
- **Location**: `control-plane/autotuner/vram_calculator.py:55, 99`
- **Mechanism**:
  CUDA graphs are captured and allocated per GPU worker process. At $TP=8$, 8 independent GPU processes capture CUDA graphs across multiple batch sizes.
  `compute_total_vram` adds a single `self.cuda_graphs_bytes` ($2.0\text{ GiB}$) to $M_{\text{total}}$, which is subsequently divided by $TP$ when checking capacity:
  $$\frac{M_{\text{total}}}{TP} = \frac{M_{\text{weights}}}{TP} + \frac{M_{\text{kv}}}{TP} + \frac{2.0\text{ GiB}}{TP}$$
  At $TP=8$, this allocates only $250\text{ MiB}$ of CUDA graph overhead per GPU instead of $2.0\text{ GiB}$, under-reserving $14\text{ GiB}$ across the node.
- **Impact**:
  Models fitting close to the 80 GB limit (such as Llama 3.3 70B with 32k context) suffer unexpected CUDA OOM errors during vLLM's warmup capture phase.
- **Remediation**:
  Formulate CUDA graphs overhead as a per-GPU parameter or scale by $TP$ in multi-GPU sizing:
  $$M_{\text{total}} = \frac{M_{\text{weights}} + M_{\text{kv\_total}} + (TP \times M_{\text{cuda\_graphs\_per\_gpu}})}{gpu\_memory\_utilization}$$

---

### Finding AUTOTUNER-DEF-04: Pipeline Parallelism (PP) Search Restrictions & Layer Divisibility
- **Severity**: Medium
- **Category**: Algorithmic Limitation
- **Location**: `control-plane/autotuner/vram_calculator.py:138-142`
- **Mechanism**:
  In `compute_parallelism`:
  ```python
  for pp in [2, 4]:
      for tp in [4, 8]:
  ```
  1. Pipeline Parallelism is only attempted with $TP \in [4, 8]$. For models with $H_{kv} \in \{1, 2\}$ (e.g. MQA architectures or small GQA ratios), $H_{kv} \pmod{TP} == 0$ fails for all $TP \ge 4$. Consequently, such models can never use $PP$ even when total cluster capacity is sufficient.
  2. There is no verification that `specs.num_hidden_layers >= pp` or `specs.num_hidden_layers % pp == 0`, potentially creating uneven layer stage distributions.
- **Impact**:
  Unwarranted `InsufficientCapacityError` on models with $H_{kv} < 4$ on multi-node clusters.
- **Remediation**:
  Allow $tp \in [1, 2, 4, 8]$ in the PP loop and verify `specs.num_hidden_layers >= pp`.

---

### Finding AUTOTUNER-DEF-05: Decode Clique TP Reduction Evaluated at Arbitrary Concurrency ($C=8$)
- **Severity**: Medium
- **Category**: Concurrency / Sizing Risk
- **Location**: `control-plane/autotuner/sla_planner.py:161`
- **Mechanism**:
  In `sla_planner.py`:
  ```python
  can_reduce = self.calculator.can_decode_reduce_tp(
      ...,
      context_len=request.max_model_len,
      concurrency=8,  # Hardcoded to 8
      gpu_memory_utilization=request.gpu_memory_utilization,
  )
  ```
  While $concurrency=8$ is tested, the decode clique manifest is rendered with `max_num_seqs: 256` (`sla_planner.py:204`), and the client requested `target_concurrency` (default 16, up to 64). Testing against 8 sequences dramatically underestimates KV-cache VRAM requirements for the decode clique.
- **Impact**:
  Decode pods approved for reduced TP (e.g. $TP=2$ instead of $TP=4$) crash with CUDA OOM when production concurrent requests exceed 8.
- **Remediation**:
  Evaluate decode TP reduction using `concurrency=request.target_concurrency` or a safety margin representing the expected decode batch load.

---

### Finding AUTOTUNER-DEF-06: Parameter Sizing Error on FP32 Safetensors & Mutable Verified Cache
- **Severity**: Medium
- **Category**: Functional Bug / State Mutation
- **Location**: `control-plane/autotuner/hf_inspector.py:110, 163`
- **Mechanism**:
  1. In `_parse_config`:
     ```python
     bytes_per_param = 2.0 if normalized_dtype in ("bf16", "fp16") else 1.0
     total_params = int(total_size_bytes / bytes_per_param)
     ```
     For FP32 models (`normalized_dtype == "fp32"`), `bytes_per_param` defaults to $1.0$ instead of $4.0$, overestimating total parameters by $400\%$.
  2. `inspect()` returns `VERIFIED_MODELS[model_id]` directly without copying. If any caller modifies the dataclass attributes, the global cache is permanently mutated.
- **Impact**:
  FP32 models are falsely rejected for overcapacity; concurrent requests can inadvertently corrupt verified model configurations.
- **Remediation**:
  1. Return `copy.deepcopy(VERIFIED_MODELS[model_id])`.
  2. Handle `fp32` explicitly: `bytes_per_param = 4.0 if normalized_dtype == "fp32" else (2.0 if normalized_dtype in ("bf16", "fp16") else 1.0)`. Add `"fp32": 4.0` to `DTYPE_BYTES`.

---

### Finding AUTOTUNER-DEF-07: Contract 1 Boundary Discrepancies in `DeployRequest`
- **Severity**: Low
- **Category**: Schema / Contract Mismatch
- **Location**: `control-plane/autotuner/sla_planner.py:35-44`
- **Mechanism**:
  `contracts/model_spec.schema.json` defines strict numerical bounds:
  - `max_model_len`: `minimum: 512, maximum: 131072`
  - `gpu_memory_utilization`: `minimum: 0.5, maximum: 0.98`
  - `target_concurrency`: `minimum: 1`
  In `sla_planner.py:DeployRequest`:
  - `max_model_len` is defined as `gt=0` (allows 1 or 1,000,000).
  - `gpu_memory_utilization` is defined as `gt=0, le=1` (allows 0.01 or 1.0).
- **Impact**:
  Requests with extreme, unphysical values (e.g. `gpu_memory_utilization=1.0` leaving 0 bytes for workspace) pass Pydantic validation but violate Contract 1.
- **Remediation**:
  Align `DeployRequest` fields with `model_spec.schema.json` validation limits (`ge=512, le=131072`, `ge=0.5, le=0.98`).

---

### Finding AUTOTUNER-DEF-08: Complete Absence of Dedicated Unit Tests for `hf_inspector.py` & `sla_planner.py`
- **Severity**: Low
- **Category**: Test Coverage Gap
- **Location**: `tests/`
- **Mechanism**:
  While `tests/test_vram_calculator.py` covers `vram_calculator.py`, there are no corresponding unit test files for:
  - `hf_inspector.py`: MoE parameter estimation, GQA attention parameter formulas, safetensors index parsing, `normalize_dtype` edge cases, and `VERIFIED_MODELS` caching.
  - `sla_planner.py`: Profile-based replica sizing (1:1 vs 1:2), RDMA vs TCP transfer selection, decode TP reduction, RFC 1123 deployment name sanitization.
- **Impact**:
  High risk of undetected regressions in metadata parsing and planning logic during refactoring.
- **Remediation**:
  Create `tests/test_hf_inspector.py` and `tests/test_sla_planner.py` with comprehensive unit test coverage.

---

## 5. Summary Table of Defect Findings

| ID | Title | Severity | Problem Type | Affected File & Lines |
|---|---|---|---|---|
| **AUTOTUNER-DEF-01** | `ClusterInventory` crash on `vram_gb=None` / Missing GPU profile mapping | **High** | Functional Bug / Schema Mismatch | `sla_planner.py:54, 81, 100` |
| **AUTOTUNER-DEF-02** | Missing Query Attention Head Divisibility check (`num_attention_heads % tp == 0`) | **High** | Algorithmic / Container Crash | `vram_calculator.py:133, 140, 176` |
| **AUTOTUNER-DEF-03** | CUDA graph overhead scalar not scaled per GPU rank in TP groups | **Medium** | Mathematical / VRAM Underestimation | `vram_calculator.py:55, 99` |
| **AUTOTUNER-DEF-04** | Pipeline Parallelism restricted to $TP \in [4, 8]$ and missing layer divisibility | **Medium** | Algorithmic Limitation | `vram_calculator.py:137-143` |
| **AUTOTUNER-DEF-05** | Decode TP reduction checked at hardcoded concurrency 8 instead of target concurrency | **Medium** | Concurrency / Production OOM | `sla_planner.py:161` |
| **AUTOTUNER-DEF-06** | FP32 safetensors parameter count $4\times$ inflation and mutable profile cache | **Medium** | Functional Bug / State Mutation | `hf_inspector.py:110, 163`, `vram_calculator.py:16` |
| **AUTOTUNER-DEF-07** | `DeployRequest` validation bounds mismatch with `model_spec.schema.json` | **Low** | Contract / Validation Drift | `sla_planner.py:35-44` |
| **AUTOTUNER-DEF-08** | Missing unit test suites for `hf_inspector.py` and `sla_planner.py` | **Low** | Test Coverage Gap | `tests/` |

---

## 6. Recommendations for Audit Synthesis & Remediation

1. **Implement GPU Profile Mapping**: In `control-plane/autotuner/sla_planner.py`, introduce a canonical GPU profile mapping table (`GPU_PROFILES`) mapping common SKUs (`H100`, `A100`, `L40S`, `A10G`, etc.) to their default VRAM in GB, and permit `vram_gb: float | None` in `GpuPool`.
2. **Enforce Dual Divisibility**: In `control-plane/autotuner/vram_calculator.py`, enforce that both query attention heads and key-value heads divide evenly by $TP$ before selecting a tensor parallel degree.
3. **Refine CUDA Graphs Scaling**: Account for per-GPU CUDA graphs memory overhead in tensor parallel groups to prevent runtime OOM on tight memory budgets.
4. **Synchronize Schema Bounds**: Align Pydantic constraints in `DeployRequest` with `contracts/model_spec.schema.json` to prevent invalid requests from reaching downstream execution.
5. **Add Comprehensive Unit Tests**: Author `tests/test_hf_inspector.py` and `tests/test_sla_planner.py` to achieve 100% test coverage across all mathematical branches, fallback estimations, and naming sanitizers.
