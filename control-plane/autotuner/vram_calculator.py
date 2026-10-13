"""
VRAM Calculator implementing exact mathematical sizing formulas to prevent CUDA OOM.
"""

from dataclasses import dataclass
from typing import Literal

DTYPE_BYTES = {
    "fp16": 2.0,
    "bf16": 2.0,
    "fp8": 1.0,
    "int4": 0.55,
    "awq": 0.55,
}

@dataclass
class ModelArchitectureSpecs:
    num_params: int
    num_hidden_layers: int
    hidden_size: int
    num_attention_heads: int
    num_key_value_heads: int
    max_position_embeddings: int = 32768
    default_dtype: str = "bf16"

    @property
    def head_dim(self) -> int:
        return self.hidden_size // self.num_attention_heads


class VRAMCalculator:
    """
    Computes weight memory, KV-cache footprint per token, total VRAM requirement,
    and determines optimal tensor parallel (TP) size and serving topology.
    """

    def __init__(
        self,
        cuda_graphs_bytes: float = 2.0 * (1024 ** 3), # 2.0 GB overhead
        default_gpu_memory_utilization: float = 0.90,
    ):
        self.cuda_graphs_bytes = cuda_graphs_bytes
        self.default_gpu_memory_utilization = default_gpu_memory_utilization

    def compute_weights_memory(self, num_params: int, quantization: str = "bf16") -> float:
        """
        M_weights = N_params * B_dtype * 1.05 (+5% embedding/LM head buffers)
        """
        dtype_key = quantization.lower()
        b_dtype = DTYPE_BYTES.get(dtype_key, 2.0)
        return float(num_params) * b_dtype * 1.05

    def compute_kv_cache_per_token(
        self,
        specs: ModelArchitectureSpecs,
        kv_dtype: str = "fp16"
    ) -> float:
        """
        M_kv_token = 2 * L * H_kv * D_head * B_kv_dtype
        """
        b_kv = 1.0 if kv_dtype.lower() in ("fp8", "int8") else 2.0
        return 2.0 * specs.num_hidden_layers * specs.num_key_value_heads * specs.head_dim * b_kv

    def compute_total_vram(
        self,
        specs: ModelArchitectureSpecs,
        quantization: str = "bf16",
        context_len: int = 32768,
        concurrency: int = 32,
        gpu_memory_utilization: float | None = None,
        kv_dtype: str = "fp16",
    ) -> float:
        """
        M_total = (M_weights + (M_kv_token * C_max * B_seq) + M_cuda_graphs) / gpu_memory_utilization
        """
        utilization = gpu_memory_utilization or self.default_gpu_memory_utilization
        m_weights = self.compute_weights_memory(specs.num_params, quantization)
        m_kv_token = self.compute_kv_cache_per_token(specs, kv_dtype)
        m_kv_total = m_kv_token * context_len * concurrency

        m_total = (m_weights + m_kv_total + self.cuda_graphs_bytes) / utilization
        return m_total

    def compute_tensor_parallel_size(
        self,
        specs: ModelArchitectureSpecs,
        gpu_vram_gb: float,
        quantization: str = "bf16",
        context_len: int = 32768,
        concurrency: int = 32,
        gpu_memory_utilization: float | None = None,
    ) -> int:
        """
        Selects TP in {1, 2, 4, 8} such that:
        TP * VRAM_GPU >= M_total AND num_key_value_heads % TP == 0
        """
        m_total_bytes = self.compute_total_vram(
            specs,
            quantization=quantization,
            context_len=context_len,
            concurrency=concurrency,
            gpu_memory_utilization=gpu_memory_utilization
        )
        gpu_vram_bytes = gpu_vram_gb * (1024 ** 3)

        valid_tp_candidates = [1, 2, 4, 8]
        for tp in valid_tp_candidates:
            if (tp * gpu_vram_bytes >= m_total_bytes) and (specs.num_key_value_heads % tp == 0):
                return tp

        # Fallback: largest supported TP where num_key_value_heads % tp == 0
        for tp in reversed(valid_tp_candidates):
            if specs.num_key_value_heads % tp == 0:
                return tp
        return 8

    def decide_serving_mode(
        self,
        tp_size: int,
        requested_profile: str,
        available_gpus: int,
    ) -> Literal["disaggregated", "aggregated"]:
        """
        Activates Disaggregated Serving (PrefillWorker + DecodeWorker) if:
        TP >= 2 and profile is low-latency or high-throughput, and available_gpus >= 2 * TP.
        Otherwise falls back to aggregated serving.
        """
        profile = requested_profile.lower()
        if (
            tp_size >= 2
            and profile in ("low-latency", "high-throughput")
            and available_gpus >= (2 * tp_size)
        ):
            return "disaggregated"
        return "aggregated"
