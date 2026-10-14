"""
SLA Planner adhering to Contract 1 (R1/R2 specifications):
- RFC 1123 deployment name sanitization & validation.
- Separation of native dtype and quantization.
- Global capacity constraint enforcement (sum of replicas * TP * PP <= available GPUs).
- Strict InsufficientCapacityError (HTTP 409) when workload cannot fit.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .hf_inspector import HFInspector, normalize_dtype
from .vram_calculator import InsufficientCapacityError, VRAMCalculator

RFC_1123_REGEX = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")


def sanitize_deployment_name(model_id: str) -> str:
    """Sanitizes model identifier to a strictly RFC 1123 compliant Kubernetes name."""
    name = model_id.split("/")[-1].lower()
    # Replace non-alphanumeric characters with hyphens
    clean = re.sub(r"[^a-z0-9-]", "-", name)
    # Collapse multiple consecutive hyphens
    clean = re.sub(r"-+", "-", clean).strip("-")
    if not clean or not clean[0].isalpha():
        clean = "dgd-" + clean
    clean = clean[:63].rstrip("-")
    if not RFC_1123_REGEX.match(clean):
        clean = "model-" + clean[:55].rstrip("-")
    return clean


GPU_HARDWARE_CATALOG: dict[str, float] = {
    "H100": 80.0,
    "NVIDIA-H100": 80.0,
    "A100": 80.0,
    "NVIDIA-A100": 80.0,
    "A100-40GB": 40.0,
    "L40S": 48.0,
    "A10G": 24.0,
    "L4": 24.0,
    "T4": 16.0,
    "V100": 32.0,
}


class DeployRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    model_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    deployment_name: str | None = None
    profile: Literal["low-latency", "high-throughput", "balanced"] = "balanced"
    dtype: Literal["auto", "bfloat16", "float16", "bf16", "fp16"] = "auto"
    quantization: Literal["fp8", "awq", "gptq"] | None = None
    max_model_len: int = Field(default=32768, ge=512, le=131072)
    gpu_memory_utilization: float = Field(default=0.90, ge=0.5, le=0.98)
    target_concurrency: int = Field(default=16, gt=0)

    @field_validator("deployment_name")
    def validate_name(cls, v):
        if v and not RFC_1123_REGEX.match(v):
            raise ValueError(f"deployment_name '{v}' violates RFC 1123 rules.")
        return v


class GpuPool(BaseModel):
    gpu_sku: str = "NVIDIA-H100-80GB"
    vram_gb: float | None = 80.0
    total_count: int = 8
    available_count: int = 8
    rdma_supported: bool = True


class ClusterInventory(BaseModel):
    nodes_count: int = 1
    gpu_pools: list[GpuPool] = Field(default_factory=lambda: [GpuPool()])
    rdma_network_active: bool = True


class SLAPlanner:
    def __init__(self, inspector: HFInspector | None = None, calculator: VRAMCalculator | None = None):
        self.inspector = inspector or HFInspector()
        self.calculator = calculator or VRAMCalculator()

    def plan(self, request: DeployRequest | dict, inventory: ClusterInventory | dict) -> dict[str, Any]:
        if isinstance(request, dict):
            request = DeployRequest(**request)
        if isinstance(inventory, dict):
            inventory = ClusterInventory(**inventory)

        # 1. Inspect Model (strictly fails if model not found)
        specs = self.inspector.inspect(request.model_id, revision=request.model_revision) if request.model_revision else self.inspector.inspect(request.model_id)

        # 2. Select primary GPU pool and determine available capacity
        for pool in inventory.gpu_pools:
            if pool.vram_gb is None or pool.vram_gb <= 0:
                matched = next((vram for key, vram in GPU_HARDWARE_CATALOG.items() if key.lower() in pool.gpu_sku.lower()), 80.0)
                pool.vram_gb = matched

        primary_pool = max(inventory.gpu_pools, key=lambda pool: pool.available_count * (pool.vram_gb or 80.0)) if inventory.gpu_pools else GpuPool(available_count=0)
        # Workers are pinned to this GPU SKU; other pools cannot satisfy that selector.
        total_available_gpus = primary_pool.available_count

        if total_available_gpus < 1:
            raise InsufficientCapacityError("Zero GPUs available in cluster.")

        # 3. Determine native dtype and quantization (D2 fix)
        native_dtype = specs.default_dtype
        if request.dtype != "auto":
            native_dtype = normalize_dtype(request.dtype)

        chosen_quant = request.quantization
        if chosen_quant in ("bf16", "fp16", "none", ""):
            chosen_quant = None

        # 4. Compute parallelism (TP, PP) with capacity checks
        tp_size, pp_size = self.calculator.compute_parallelism(
            specs=specs,
            gpu_vram_gb=primary_pool.vram_gb,
            quantization=chosen_quant,
            dtype=native_dtype,
            context_len=request.max_model_len,
            concurrency=request.target_concurrency,
            gpu_memory_utilization=request.gpu_memory_utilization,
            available_gpus=total_available_gpus,
        )

        # 5. Determine Serving Mode & Clique Replicas (D4 fix)
        gpus_per_replica = tp_size * pp_size
        deployment_name = request.deployment_name or sanitize_deployment_name(request.model_id)

        # Disaggregated requires TP >= 2, profile low-latency / high-throughput, and enough GPUs
        can_disaggregate = (
            tp_size >= 2
            and request.profile in ("low-latency", "high-throughput")
            and total_available_gpus >= (2 * gpus_per_replica)
        )

        use_rdma = inventory.rdma_network_active and primary_pool.rdma_supported

        # Total VRAM estimate
        total_vram_bytes = self.calculator.compute_total_vram(
            specs,
            quantization=chosen_quant,
            dtype=native_dtype,
            context_len=request.max_model_len,
            concurrency=request.target_concurrency,
            gpu_memory_utilization=request.gpu_memory_utilization,
        )
        total_vram_gb = total_vram_bytes / (1024**3)
        vram_per_gpu = round(total_vram_gb / tp_size, 1)

        plan: dict[str, Any] = {
            "model_id": request.model_id,
            "deployment_name": deployment_name,
            "serving_mode": "disaggregated" if can_disaggregate else "aggregated",
            "dtype": "bfloat16" if native_dtype == "bf16" else ("float16" if native_dtype == "fp16" else native_dtype),
            "quantization": chosen_quant,
            "max_model_len": request.max_model_len,
            "gpu_memory_utilization": request.gpu_memory_utilization,
            "estimated_vram_gb_per_replica": min(vram_per_gpu, primary_pool.vram_gb),
            "kv_cache_transfer": {
                "protocol": "rdma" if use_rdma else "tcp",
                "engine": "nixl" if use_rdma else "default",
            },
        }
        if request.model_revision:
            plan["model_revision"] = request.model_revision

        if can_disaggregate:
            # Check if decode TP can be safely reduced without OOM
            decode_tp = tp_size
            if tp_size >= 4 and (tp_size // 2) >= 1:
                candidate_decode_tp = tp_size // 2
                if self.calculator.can_decode_reduce_tp(
                    specs=specs,
                    reduced_tp=candidate_decode_tp,
                    gpu_vram_gb=primary_pool.vram_gb,
                    quantization=chosen_quant,
                    dtype=native_dtype,
                    context_len=request.max_model_len,
                    concurrency=max(request.target_concurrency, 16),
                    gpu_memory_utilization=request.gpu_memory_utilization,
                ):
                    decode_tp = candidate_decode_tp

            decode_gpus_per_unit = decode_tp * pp_size
            prefill_gpus_per_unit = tp_size * pp_size

            # Sizing based on profile ratio (D4 fix)
            if request.profile == "high-throughput":
                # Default ratio 1:2 prefill:decode
                prefill_replicas = 1
                decode_replicas = 2
            else:
                # Default ratio 1:1 for low-latency
                prefill_replicas = 1
                decode_replicas = 1

            total_needed = (prefill_replicas * prefill_gpus_per_unit) + (decode_replicas * decode_gpus_per_unit)
            if total_needed > total_available_gpus:
                # Scale down to minimum viable 1 prefill + 1 decode
                prefill_replicas = 1
                decode_replicas = 1
                total_needed = (prefill_replicas * prefill_gpus_per_unit) + (decode_replicas * decode_gpus_per_unit)

            if total_needed > total_available_gpus:
                raise InsufficientCapacityError(
                    f"Disaggregated serving requires {total_needed} GPUs, only {total_available_gpus} available."
                )

            plan["prefill_clique"] = {
                "replicas": prefill_replicas,
                "tensor_parallel_size": tp_size,
                "pipeline_parallel_size": pp_size,
                "gpu_sku": primary_pool.gpu_sku,
                "max_num_batched_tokens": 16384,
                "enable_chunked_prefill": True,
            }
            plan["decode_clique"] = {
                "replicas": decode_replicas,
                "tensor_parallel_size": decode_tp,
                "pipeline_parallel_size": pp_size,
                "gpu_sku": primary_pool.gpu_sku,
                "max_num_seqs": 256,
            }
        else:
            # Aggregated serving: fit as many worker replicas as available
            worker_replicas = max(1, total_available_gpus // gpus_per_replica)
            plan["worker_clique"] = {
                "replicas": worker_replicas,
                "tensor_parallel_size": tp_size,
                "pipeline_parallel_size": pp_size,
                "gpu_sku": primary_pool.gpu_sku,
                "max_num_seqs": 128,
            }

        return plan


def compute_deployment_plan(
    request: DeployRequest | dict, cluster_gpu_inventory: ClusterInventory | dict
) -> dict[str, Any]:
    planner = SLAPlanner()
    return planner.plan(request, cluster_gpu_inventory)
