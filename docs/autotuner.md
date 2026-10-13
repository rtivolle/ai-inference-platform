# Auto-Tuner & VRAM Sizing

Code: `control-plane/autotuner/{hf_inspector,vram_calculator,sla_planner}.py`.
Contract: `contracts/model_spec.schema.json`.

## HF inspection (`hf_inspector.py`)

1. Exact match in `VERIFIED_MODELS` (Llama 3.3/3.1 70B, Llama 3 8B, Mistral 7B
   v0.3, Qwen 2.5 72B, Llama 3.1 405B) → returned directly, no network.
2. Otherwise fetch `config.json` via `hf_hub_download`, plus
   `model.safetensors.index.json` → `metadata.total_size` when present.
3. Parse: `head_dim` wins over `hidden_size / num_attention_heads`; MLA detected
   by `kv_lora_rank` / `qk_rope_head_dim`; dtype normalized (`bfloat16→bf16`,
   `float16→fp16`, …); params from `total_size / bytes_per_param`, else GQA+MoE
   estimate (`num_local_experts` aware).
4. Any failure raises `ModelNotFoundError` → API `404`. There is **no silent
   fallback**.

## VRAM math (`vram_calculator.py`)

Weights (5 % embedding/LM-head overhead):

```text
M_weights = N_params × B_dtype × 1.05
B_dtype: bf16/fp16 = 2.0, fp8 = 1.0, awq/gptq/int4 = 0.55
```

KV-cache per token:

```text
GQA:  M_kv_token = 2 × L × H_kv × D_head × B_kv
MLA:  M_kv_token = L × (kv_lora_rank + qk_rope_head_dim) × B_kv
B_kv = 1.0 for fp8/int8 else 2.0; CUDA graphs reserve 2.0 GiB
```

Total (usable fraction `u = gpu_memory_utilization`, default 0.90):

```text
M_total = (M_weights + M_kv_token × C_max × B_seq + M_cuda_graphs) / u
```

Parallelism: smallest `TP ∈ {1,2,4,8}` with `TP × VRAM_GPU ≥ M_total` and
`H_kv mod TP == 0` and `TP ≤ available_gpus`; else try `PP ∈ {2,4}` with
`TP ∈ {4,8}`. No fit → `InsufficientCapacityError` → API `409`.

`can_decode_reduce_tp` allows `decode_tp = TP/2` only if capacity and the
`H_kv` divisibility check still hold.

## SLA planner (`sla_planner.py`)

- `dtype`: `auto` keeps the inspected native dtype; explicit value overrides.
  `quantization` is separate (`fp8/awq/gptq/null`) — `bf16/fp16` are dtypes, and
  only non-null quantization is rendered as `--quantization` (D2 fix).
- `deployment_name` defaults to `sanitize_deployment_name(model_id)` (lowercase,
  hyphen-collapsed, RFC 1123, ≤63 chars); explicit names are validated.
- Serving mode: `disaggregated` only if `TP ≥ 2`, profile is
  `low-latency|high-throughput`, and GPUs ≥ `2 × TP × PP`. Otherwise aggregated.
- Replicas: `high-throughput` → 1 prefill + 2 decode; `low-latency` → 1 + 1;
  scaled down to 1+1 if over capacity, else `409`. Aggregated fills
  `available // (TP×PP)` worker replicas.
- `estimated_vram_gb_per_replica = min(total_vram/TP, pool_vram)`.
- `kv_cache_transfer`: `rdma/nixl` when inventory reports RDMA, else `tcp/default`.

## Worked example

Llama-3.3-70B (70.55 B params, 80 layers, `H_kv=8`, `D=128`), bf16, 32 k ctx:

- `M_weights ≈ 70.55e9 × 2 × 1.05 ≈ 148 GB`
- `M_kv_token ≈ 2 × 80 × 8 × 128 × 2 B ≈ 328 kB`; × 32 k × 16 ≈ 168 GB
- `M_total ≈ (148 + 168 + 2) / 0.9 ≈ 353 GB` → needs `TP=8` on 80 GB GPUs for
  this context/concurrency; smaller `max_model_len`/concurrency or `fp8`
  reduces it. Tests: `tests/test_vram_calculator.py`.
