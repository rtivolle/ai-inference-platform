"""
VRAM Calculator implementing exact mathematical sizing formulas to prevent CUDA OOM.
Adheres to R2 specifications:
- Explicit head_dim and MLA (DeepSeek) KV-cache formulas.
- Pipeline parallelism (PP) support when TP is insufficient.
- Strict InsufficientCapacityError raising when resources cannot fit the workload.
"""

from dataclasses import dataclass


class InsufficientCapacityError(Exception):
    """Raised when cluster GPUs or VRAM cannot fit the requested model."""


DTYPE_BYTES = {
    "fp16": 2.0,
    "bf16": 2.0,
    "float16": 2.0,
    "bfloat16": 2.0,
    "fp8": 1.0,
    "int4": 0.55,
    "awq": 0.55,
    "gptq": 0.55,
}


@dataclass
class ModelArchitectureSpecs:
    num_params: int
    num_hidden_layers: int
    hidden_size: int
    num_attention_heads: int
    num_key_value_heads: int
    head_dim: int | None = None
    max_position_embeddings: int = 32768
    default_dtype: str = "bf16"
    is_mla: bool = False
    kv_lora_rank: int = 512
    qk_rope_head_dim: int = 64

    def __post_init__(self):
        if self.head_dim is None:
            self.head_dim = self.hidden_size // self.num_attention_heads


class VRAMCalculator:
    """
    Computes weight memory, KV-cache footprint per token, total VRAM requirement,
    and determines optimal tensor parallel (TP) and pipeline parallel (PP) sizes.
    """

    def __init__(
        self,
        cuda_graphs_bytes: float = 2.0 * (1024**3),  # 2.0 GB overhead
        default_gpu_memory_utilization: float = 0.90,
    ):
        self.cuda_graphs_bytes = cuda_graphs_bytes
        self.default_gpu_memory_utilization = default_gpu_memory_utilization

    def compute_weights_memory(self, num_params: int, quantization: str | None = None, dtype: str = "bf16") -> float:
        """
        M_weights = N_params * B_dtype * 1.05 (+5% embedding/LM head buffers)
        """
        dtype_key = (quantization or dtype).lower()
        b_dtype = DTYPE_BYTES.get(dtype_key, 2.0)
        return float(num_params) * b_dtype * 1.05

    def compute_kv_cache_per_token(self, specs: ModelArchitectureSpecs, kv_dtype: str = "fp16") -> float:
        """
        Standard GQA: M_kv_token = 2 * L * H_kv * D_head * B_kv_dtype
        DeepSeek MLA: M_kv_token = L * (kv_lora_rank + qk_rope_head_dim) * B_kv_dtype
        """
        b_kv = 1.0 if kv_dtype.lower() in ("fp8", "int8") else 2.0
        if specs.is_mla:
            return float(specs.num_hidden_layers) * (specs.kv_lora_rank + specs.qk_rope_head_dim) * b_kv

        head_dim = specs.head_dim if specs.head_dim is not None else (specs.hidden_size // specs.num_attention_heads)
        return 2.0 * specs.num_hidden_layers * specs.num_key_value_heads * head_dim * b_kv

    def compute_total_vram(
        self,
        specs: ModelArchitectureSpecs,
        quantization: str | None = None,
        dtype: str = "bf16",
        context_len: int = 32768,
        concurrency: int = 16,
        gpu_memory_utilization: float | None = None,
        kv_dtype: str = "fp16",
    ) -> float:
        """
        M_total = (M_weights + (M_kv_token * C_max * B_seq) + M_cuda_graphs) / gpu_memory_utilization
        """
        utilization = gpu_memory_utilization or self.default_gpu_memory_utilization
        m_weights = self.compute_weights_memory(specs.num_params, quantization=quantization, dtype=dtype)
        m_kv_token = self.compute_kv_cache_per_token(specs, kv_dtype)
        m_kv_total = m_kv_token * context_len * concurrency

        m_total = (m_weights + m_kv_total + self.cuda_graphs_bytes) / utilization
        return m_total

    def compute_parallelism(
        self,
        specs: ModelArchitectureSpecs,
        gpu_vram_gb: float,
        quantization: str | None = None,
        dtype: str = "bf16",
        context_len: int = 32768,
        concurrency: int = 16,
        gpu_memory_utilization: float | None = None,
        available_gpus: int = 8,
    ) -> tuple[int, int]:
        """
        Finds optimal (TP, PP) where:
        TP in {1, 2, 4, 8}, PP in {1, 2, 4}
        TP * PP * VRAM_GPU >= M_total AND num_key_value_heads % TP == 0
        AND TP * PP <= available_gpus
        Raises InsufficientCapacityError if no combination satisfies the constraints.
        """
        m_total_bytes = self.compute_total_vram(
            specs,
            quantization=quantization,
            dtype=dtype,
            context_len=context_len,
            concurrency=concurrency,
            gpu_memory_utilization=gpu_memory_utilization,
        )
        gpu_vram_bytes = gpu_vram_gb * (1024**3)

        # 1. Try single-node TP first (PP = 1)
        valid_tps = [1, 2, 4, 8]
        for tp in valid_tps:
            if (tp * gpu_vram_bytes >= m_total_bytes) and (specs.num_key_value_heads % tp == 0):
                if tp <= available_gpus:
                    return tp, 1

        # 2. Try multi-node with Pipeline Parallelism (PP in {2, 4})
        for pp in [2, 4]:
            for tp in [4, 8]:
                if (tp * pp * gpu_vram_bytes >= m_total_bytes) and (specs.num_key_value_heads % tp == 0):
                    if (tp * pp) <= available_gpus:
                        return tp, pp

        # No valid configuration fits: raise InsufficientCapacityError
        required_gb = round(m_total_bytes / (1024**3), 1)
        max_allocatable_gb = round(available_gpus * gpu_vram_gb, 1)
        raise InsufficientCapacityError(
            f"Cannot schedule model requiring {required_gb} GB VRAM on cluster with "
            f"{available_gpus} GPUs ({max_allocatable_gb} GB total capacity)."
        )

    def can_decode_reduce_tp(
        self,
        specs: ModelArchitectureSpecs,
        reduced_tp: int,
        gpu_vram_gb: float,
        quantization: str | None = None,
        dtype: str = "bf16",
        context_len: int = 32768,
        concurrency: int = 8,
        gpu_memory_utilization: float | None = None,
    ) -> bool:
        """
        Verifies if decode clique can safely reduce its TP without causing CUDA OOM:
        M_total(TP_decode) <= TP_decode * VRAM_GPU
        """
        m_total = self.compute_total_vram(
            specs,
            quantization=quantization,
            dtype=dtype,
            context_len=context_len,
            concurrency=concurrency,
            gpu_memory_utilization=gpu_memory_utilization,
        )
        capacity = reduced_tp * gpu_vram_gb * (1024**3)
        return capacity >= m_total and (specs.num_key_value_heads % reduced_tp == 0)
