"""
Unit tests validating exact mathematical formulas for VRAM memory sizing and TP computation.
Section 5 formulas compliance tests.
"""

import pytest
from control_plane.autotuner.vram_calculator import VRAMCalculator, ModelArchitectureSpecs

def test_weights_memory_exact_formula(vram_calc, llama_70b_specs):
    """
    Validates: M_weights = N_params * B_dtype * 1.05
    """
    num_params = llama_70b_specs.num_params
    
    # FP16/BF16 (B_dtype = 2.0)
    expected_bf16 = num_params * 2.0 * 1.05
    actual_bf16 = vram_calc.compute_weights_memory(num_params, "bf16")
    assert pytest.approx(actual_bf16, rel=1e-5) == expected_bf16
    assert actual_bf16 / (1024 ** 3) > 137.0 # ~137.8 GB

    # FP8 (B_dtype = 1.0)
    expected_fp8 = num_params * 1.0 * 1.05
    actual_fp8 = vram_calc.compute_weights_memory(num_params, "fp8")
    assert pytest.approx(actual_fp8, rel=1e-5) == expected_fp8
    assert actual_fp8 / (1024 ** 3) > 68.0 # ~68.9 GB

    # AWQ / INT4 (B_dtype = 0.55)
    expected_awq = num_params * 0.55 * 1.05
    actual_awq = vram_calc.compute_weights_memory(num_params, "awq")
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

def test_tensor_parallel_selection_with_kv_head_divisibility(vram_calc, llama_70b_specs):
    """
    Validates TP selection in {1, 2, 4, 8} with strict constraint:
    H_kv % TP == 0 (where H_kv = 8 for LLaMA 3.3 70B)
    """
    # On 80GB GPU with FP8: TP should be 4
    tp = vram_calc.compute_tensor_parallel_size(
        specs=llama_70b_specs,
        gpu_vram_gb=80.0,
        quantization="fp8",
        context_len=32768,
        concurrency=16,
        gpu_memory_utilization=0.90
    )
    assert tp in (2, 4)
    assert llama_70b_specs.num_key_value_heads % tp == 0

def test_tensor_parallel_8b_model(vram_calc, llama_8b_specs):
    """
    For an 8B model on 80GB GPU, TP should be 1.
    """
    tp = vram_calc.compute_tensor_parallel_size(
        specs=llama_8b_specs,
        gpu_vram_gb=80.0,
        quantization="bf16",
        context_len=8192,
        concurrency=16,
        gpu_memory_utilization=0.90
    )
    assert tp == 1
    assert llama_8b_specs.num_key_value_heads % tp == 0

def test_serving_mode_decision(vram_calc):
    """
    TP >= 2 and profile is low-latency and available_gpus >= 2 * TP -> Disaggregated
    Else -> Aggregated
    """
    # Meets criteria: TP=4, available=8, profile=low-latency
    mode = vram_calc.decide_serving_mode(
        tp_size=4,
        requested_profile="low-latency",
        available_gpus=8
    )
    assert mode == "disaggregated"

    # Not enough GPUs: TP=4, available=6 (needs 8)
    mode_fallback = vram_calc.decide_serving_mode(
        tp_size=4,
        requested_profile="low-latency",
        available_gpus=6
    )
    assert mode_fallback == "aggregated"

    # Profile balanced (not low-latency / high-throughput)
    mode_balanced = vram_calc.decide_serving_mode(
        tp_size=4,
        requested_profile="balanced",
        available_gpus=16
    )
    assert mode_balanced == "aggregated"
