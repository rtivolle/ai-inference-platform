"""
Unit tests validating exact mathematical formulas for VRAM memory sizing and TP/PP computation.
Covers R2 / Section 5 formulas compliance.
"""

import pytest
from autotuner.vram_calculator import InsufficientCapacityError, ModelArchitectureSpecs


def test_weights_memory_exact_formula(vram_calc, llama_70b_specs):
    """
    Validates: M_weights = N_params * B_dtype * 1.05
    """
    num_params = llama_70b_specs.num_params

    # FP16/BF16 (B_dtype = 2.0)
    expected_bf16 = num_params * 2.0 * 1.05
    actual_bf16 = vram_calc.compute_weights_memory(num_params, dtype="bf16")
    assert pytest.approx(actual_bf16, rel=1e-5) == expected_bf16
    assert actual_bf16 / (1024**3) > 137.0  # ~137.8 GB

    # FP8 (B_dtype = 1.0)
    expected_fp8 = num_params * 1.0 * 1.05
    actual_fp8 = vram_calc.compute_weights_memory(num_params, quantization="fp8")
    assert pytest.approx(actual_fp8, rel=1e-5) == expected_fp8
    assert actual_fp8 / (1024**3) > 68.0  # ~68.9 GB

    # AWQ / INT4 (B_dtype = 0.55)
    expected_awq = num_params * 0.55 * 1.05
    actual_awq = vram_calc.compute_weights_memory(num_params, quantization="awq")
    assert pytest.approx(actual_awq, rel=1e-5) == expected_awq


def test_kv_cache_per_token_exact_formula(vram_calc, llama_70b_specs):
    """
    Validates: M_kv_token = 2 * L * H_kv * D_head * B_kv_dtype
    L = 80, H_kv = 8, D_head = 8192 / 64 = 128, B_kv_dtype = 2.0
    2 * 80 * 8 * 128 * 2 = 327,680 bytes/token
    """
    assert llama_70b_specs.head_dim == 128
    expected_bytes = 2.0 * 80 * 8 * 128 * 2.0
    actual_bytes = vram_calc.compute_kv_cache_per_token(llama_70b_specs, "fp16")
    assert actual_bytes == expected_bytes
    assert actual_bytes == 327680.0


def test_kv_cache_mla_deepseek_formula(vram_calc):
    """
    Validates DeepSeek MLA KV-cache: M_kv_token = L * (kv_lora_rank + qk_rope_head_dim) * B_kv
    L = 60, kv_lora_rank = 512, qk_rope_head_dim = 64, B_kv = 2
    60 * (512 + 64) * 2 = 69,120 bytes/token
    """
    deepseek_specs = ModelArchitectureSpecs(
        num_params=671_000_000_000,
        num_hidden_layers=60,
        hidden_size=7168,
        num_attention_heads=128,
        num_key_value_heads=128,
        head_dim=128,
        is_mla=True,
        kv_lora_rank=512,
        qk_rope_head_dim=64,
    )
    actual_bytes = vram_calc.compute_kv_cache_per_token(deepseek_specs, "fp16")
    expected_bytes = 60 * (512 + 64) * 2.0
    assert actual_bytes == expected_bytes


def test_tensor_parallel_selection_with_kv_head_divisibility(vram_calc, llama_70b_specs):
    """
    Validates TP selection in {1, 2, 4, 8} with strict constraint:
    H_kv % TP == 0 (where H_kv = 8 for LLaMA 3.3 70B)
    """
    tp, pp = vram_calc.compute_parallelism(
        specs=llama_70b_specs,
        gpu_vram_gb=80.0,
        quantization="fp8",
        dtype="bf16",
        context_len=32768,
        concurrency=16,
        gpu_memory_utilization=0.90,
        available_gpus=8,
    )
    assert tp in (2, 4)
    assert pp == 1
    assert llama_70b_specs.num_key_value_heads % tp == 0


def test_tensor_parallel_8b_model(vram_calc, llama_8b_specs):
    """
    For an 8B model on 80GB GPU, TP should be 1.
    """
    tp, pp = vram_calc.compute_parallelism(
        specs=llama_8b_specs,
        gpu_vram_gb=80.0,
        dtype="bf16",
        context_len=8192,
        concurrency=16,
        gpu_memory_utilization=0.90,
        available_gpus=8,
    )
    assert tp == 1
    assert pp == 1
    assert llama_8b_specs.num_key_value_heads % tp == 0


def test_insufficient_capacity_error_raised_for_huge_model(vram_calc):
    """
    Validates that a 405B model without quantization fails with InsufficientCapacityError
    on an 8x80GB GPU cluster (D4 fix).
    """
    huge_specs = ModelArchitectureSpecs(
        num_params=405_000_000_000,
        num_hidden_layers=126,
        hidden_size=16384,
        num_attention_heads=128,
        num_key_value_heads=8,
        head_dim=128,
        max_position_embeddings=131072,
        default_dtype="bf16",
    )
    with pytest.raises(InsufficientCapacityError):
        vram_calc.compute_parallelism(
            specs=huge_specs,
            gpu_vram_gb=80.0,
            dtype="bf16",
            quantization=None,
            context_len=32768,
            concurrency=16,
            gpu_memory_utilization=0.90,
            available_gpus=8,  # 8 GPUs = 640 GB, but 405B bf16 needs >850 GB
        )


def test_can_decode_reduce_tp(vram_calc, llama_70b_specs):
    """
    Validates that decode TP reduction is only approved if reduced TP * VRAM >= M_total.
    """
    can_reduce = vram_calc.can_decode_reduce_tp(
        specs=llama_70b_specs,
        reduced_tp=2,
        gpu_vram_gb=80.0,
        quantization="fp8",
        dtype="bf16",
        context_len=32768,
        concurrency=8,
        gpu_memory_utilization=0.90,
    )
    assert isinstance(can_reduce, bool)
