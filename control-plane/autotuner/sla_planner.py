"""
SLA Planner: Computes full deployment topology conforming to Contract 1.
"""

import math
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from .hf_inspector import HFInspector
from .vram_calculator import VRAMCalculator, ModelArchitectureSpecs

class DeployRequest(BaseModel):
    model_id: str
    deployment_name: Optional[str] = None
    profile: str = "balanced" # "low-latency", "high-throughput", "balanced"
    quantization: str = "auto" # "auto", "fp16", "bf16", "fp8", "awq", "int4"
    max_model_len: int = 32768
    gpu_memory_utilization: float = 0.90
    target_concurrency: int = 16

class GpuPool(BaseModel):
    gpu_sku: str = "NVIDIA-H100-80GB"
    vram_gb: float = 80.0
    total_count: int = 8
    available_count: int = 8
    rdma_supported: bool = True

class ClusterInventory(BaseModel):
    nodes_count: int = 1
    gpu_pools: list[GpuPool] = Field(default_factory=lambda: [GpuPool()])
    rdma_network_active: bool = True

def sanitize_deployment_name(model_id: str) -> str:
    name = model_id.split("/")[-1].lower()
    return name.replace(".", "-").replace("_", "-")

class SLAPlanner:
    def __init__(self, inspector: Optional[HFInspector] = None, calculator: Optional[VRAMCalculator] = None):
        self.inspector = inspector or HFInspector()
        self.calculator = calculator or VRAMCalculator()

    def plan(self, request: DeployRequest | dict, inventory: ClusterInventory | dict) -> Dict[str, Any]:
        if isinstance(request, dict):
            request = DeployRequest(**request)
        if isinstance(inventory, dict):
            inventory = ClusterInventory(**inventory)

        # 1. Inspect Model
        specs = self.inspector.inspect(request.model_id)

        # 2. Select primary GPU pool
        primary_pool = inventory.gpu_pools[0] if inventory.gpu_pools else GpuPool()

        # 3. Determine Quantization
        chosen_quant = request.quantization.lower()
        if chosen_quant == "auto":
            # If 70B+ model on 80GB GPUs, fp8 gives high performance & fitting
            if specs.num_params >= 60_000_000_000:
                chosen_quant = "fp8"
            else:
                chosen_quant = "bf16"

        # 4. Compute TP size
        tp_size = self.calculator.compute_tensor_parallel_size(
            specs=specs,
            gpu_vram_gb=primary_pool.vram_gb,
            quantization=chosen_quant,
            context_len=request.max_model_len,
            concurrency=request.target_concurrency,
            gpu_memory_utilization=request.gpu_memory_utilization
        )

        # 5. Decide serving mode (Aggregated vs Disaggregated)
        total_available_gpus = sum(p.available_count for p in inventory.gpu_pools)
        serving_mode = self.calculator.decide_serving_mode(
            tp_size=tp_size,
            requested_profile=request.profile,
            available_gpus=total_available_gpus
        )

        # Compute estimated VRAM per GPU
        total_vram_bytes = self.calculator.compute_total_vram(
            specs,
            quantization=chosen_quant,
            context_len=request.max_model_len,
            concurrency=request.target_concurrency,
            gpu_memory_utilization=request.gpu_memory_utilization
        )
        total_vram_gb = total_vram_bytes / (1024 ** 3)
        vram_per_gpu = round(total_vram_gb / tp_size, 1)

        deployment_name = request.deployment_name or sanitize_deployment_name(request.model_id)

        # 6. Build Cliques based on Serving Mode
        use_rdma = inventory.rdma_network_active and primary_pool.rdma_supported

        plan: Dict[str, Any] = {
            "model_id": request.model_id,
            "deployment_name": deployment_name,
            "serving_mode": serving_mode,
            "quantization": chosen_quant,
            "max_model_len": request.max_model_len,
            "gpu_memory_utilization": request.gpu_memory_utilization,
            "estimated_vram_gb_per_replica": min(vram_per_gpu, primary_pool.vram_gb),
            "kv_cache_transfer": {
                "protocol": "rdma" if use_rdma else "tcp",
                "engine": "nixl" if use_rdma else "default",
                "Engine": "nixl" if use_rdma else "default"
            }
        }

        if serving_mode == "disaggregated":
            # Prefill Clique: optimized for high batch tokens and TTFT
            plan["prefill_clique"] = {
                "replicas": 2,
                "tensor_parallel_size": tp_size,
                "pipeline_parallel_size": 1,
                "gpu_sku": primary_pool.gpu_sku,
                "max_num_batched_tokens": 16384,
                "enable_chunked_prefill": True
            }
            # Decode Clique: optimized for inter-token latency (TP can be tp_size or tp_size/2 if H_kv allows)
            decode_tp = tp_size if tp_size <= 2 else (tp_size // 2 if specs.num_key_value_heads % (tp_size // 2) == 0 else tp_size)
            plan["decode_clique"] = {
                "replicas": 4,
                "tensor_parallel_size": decode_tp,
                "pipeline_parallel_size": 1,
                "gpu_sku": primary_pool.gpu_sku,
                "max_num_seqs": 256
            }
        else:
            # Aggregated monobloc clique
            plan["worker_clique"] = {
                "replicas": 2,
                "tensor_parallel_size": tp_size,
                "pipeline_parallel_size": 1,
                "gpu_sku": primary_pool.gpu_sku,
                "max_num_seqs": 128
            }

        return plan

def compute_deployment_plan(
    request: DeployRequest | dict,
    cluster_gpu_inventory: ClusterInventory | dict
) -> Dict[str, Any]:
    planner = SLAPlanner()
    return planner.plan(request, cluster_gpu_inventory)
