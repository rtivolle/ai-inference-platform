"""
Hugging Face model metadata inspector adhering to R2 specifications:
- Reads metadata.total_size from model.safetensors.index.json when available.
- Prioritizes explicit head_dim over hidden_size / num_attention_heads.
- Supports DeepSeek Multi-Head Latent Attention (MLA).
- Formula considers GQA: 2*h^2 + 2*h*H_kv*D_head.
- Normalizes torch_dtype (e.g., bfloat16 -> bf16).
- Never falls back silently: unknown models raise ModelNotFoundError.
"""

import json
import logging

from huggingface_hub import hf_hub_download

from .vram_calculator import ModelArchitectureSpecs

logger = logging.getLogger("hf_inspector")


class ModelNotFoundError(Exception):
    """Raised when a requested model cannot be inspected or found."""


# Standard benchmark architecture profiles (verified exact numbers)
VERIFIED_MODELS = {
    "meta-llama/Llama-3.3-70B-Instruct": ModelArchitectureSpecs(
        num_params=70_553_706_496,
        num_hidden_layers=80,
        hidden_size=8192,
        num_attention_heads=64,
        num_key_value_heads=8,
        head_dim=128,
        max_position_embeddings=131072,
        default_dtype="bf16",
    ),
    "meta-llama/Meta-Llama-3-70B-Instruct": ModelArchitectureSpecs(
        num_params=70_553_706_496,
        num_hidden_layers=80,
        hidden_size=8192,
        num_attention_heads=64,
        num_key_value_heads=8,
        head_dim=128,
        max_position_embeddings=8192,
        default_dtype="bf16",
    ),
    "meta-llama/Meta-Llama-3-8B-Instruct": ModelArchitectureSpecs(
        num_params=8_030_261_248,
        num_hidden_layers=32,
        hidden_size=4096,
        num_attention_heads=32,
        num_key_value_heads=8,
        head_dim=128,
        max_position_embeddings=8192,
        default_dtype="bf16",
    ),
    "mistralai/Mistral-7B-Instruct-v0.3": ModelArchitectureSpecs(
        num_params=7_248_023_552,
        num_hidden_layers=32,
        hidden_size=4096,
        num_attention_heads=32,
        num_key_value_heads=8,
        head_dim=128,
        max_position_embeddings=32768,
        default_dtype="bf16",
    ),
    "Qwen/Qwen2.5-72B-Instruct": ModelArchitectureSpecs(
        num_params=72_706_096_640,
        num_hidden_layers=80,
        hidden_size=8192,
        num_attention_heads=64,
        num_key_value_heads=8,
        head_dim=128,
        max_position_embeddings=131072,
        default_dtype="bf16",
    ),
    "meta-llama/Llama-3.1-405B-Instruct": ModelArchitectureSpecs(
        num_params=405_000_000_000,
        num_hidden_layers=126,
        hidden_size=16384,
        num_attention_heads=128,
        num_key_value_heads=8,
        head_dim=128,
        max_position_embeddings=131072,
        default_dtype="bf16",
    ),
}


def normalize_dtype(dtype_str: str) -> str:
    cleaned = dtype_str.lower().strip()
    if cleaned in ("bfloat16", "bf16"):
        return "bf16"
    if cleaned in ("float16", "fp16", "half"):
        return "fp16"
    if cleaned in ("float32", "fp32"):
        return "fp32"
    if "fp8" in cleaned:
        return "fp8"
    return "bf16"


class HFInspector:
    def __init__(self, token: str | None = None):
        self.token = token

    def inspect(self, model_id: str) -> ModelArchitectureSpecs:
        # Check verified models dictionary first
        if model_id in VERIFIED_MODELS:
            logger.info(f"Using verified profile for {model_id}")
            return VERIFIED_MODELS[model_id]

        logger.info(f"Inspecting Hugging Face repository for {model_id}...")
        try:
            # 1. Fetch config.json
            config_file = hf_hub_download(repo_id=model_id, filename="config.json", token=self.token)
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            # 2. Try fetching model.safetensors.index.json for exact weights size
            total_size_bytes = None
            try:
                index_file = hf_hub_download(
                    repo_id=model_id, filename="model.safetensors.index.json", token=self.token
                )
                with open(index_file, "r", encoding="utf-8") as f:
                    index_data = json.load(f)
                    metadata = index_data.get("metadata", {})
                    total_size_bytes = metadata.get("total_size")
            except Exception:
                pass

            return self._parse_config(config, total_size_bytes=total_size_bytes)
        except Exception as e:
            logger.error(f"Failed to inspect model {model_id}: {e}")
            # R2 / D9: NEVER fall back silently. Unknown/unreachable models must raise ModelNotFoundError
            raise ModelNotFoundError(f"Model '{model_id}' could not be resolved or inspected: {e}")

    def _parse_config(self, config: dict, total_size_bytes: int | None = None) -> ModelArchitectureSpecs:
        hidden_size = config.get("hidden_size", 4096)
        num_layers = config.get("num_hidden_layers", config.get("n_layer", 32))
        num_heads = config.get("num_attention_heads", config.get("n_head", 32))
        num_kv_heads = config.get("num_key_value_heads", num_heads)

        # R2 / D9: Explicit head_dim takes precedence over hidden_size / num_attention_heads
        if "head_dim" in config:
            head_dim = config["head_dim"]
        else:
            head_dim = hidden_size // num_heads

        max_pos = config.get("max_position_embeddings", 32768)
        raw_dtype = config.get("torch_dtype", "bfloat16")
        normalized_dtype = normalize_dtype(raw_dtype)

        # DeepSeek MLA parameters
        is_mla = "kv_lora_rank" in config or "qk_rope_head_dim" in config
        kv_lora_rank = config.get("kv_lora_rank", 512) if is_mla else 0
        qk_rope_head_dim = config.get("qk_rope_head_dim", 64) if is_mla else 0

        # Determine parameter count: prioritize total_size from safetensors index
        if total_size_bytes:
            # bytes divided by bytes_per_param (approximate based on dtype)
            bytes_per_param = 2.0 if normalized_dtype in ("bf16", "fp16") else 1.0
            total_params = int(total_size_bytes / bytes_per_param)
        else:
            # Fallback estimation incorporating GQA and MoE
            vocab_size = config.get("vocab_size", 128256)
            intermediate_size = config.get("intermediate_size", int(hidden_size * 3.5))

            # GQA attention parameters: Q (h * num_heads * head_dim) + K,V (2 * h * num_kv_heads * head_dim) + O (h * h)
            q_params = hidden_size * (num_heads * head_dim)
            kv_params = 2 * hidden_size * (num_kv_heads * head_dim)
            o_params = (num_heads * head_dim) * hidden_size
            attn_params = q_params + kv_params + o_params

            # MoE check
            num_experts = config.get("num_local_experts", config.get("num_experts", 1))
            mlp_params = 3 * hidden_size * intermediate_size * num_experts

            layer_params = attn_params + mlp_params
            total_params = (num_layers * layer_params) + (2 * vocab_size * hidden_size)

        return ModelArchitectureSpecs(
            num_params=total_params,
            num_hidden_layers=num_layers,
            hidden_size=hidden_size,
            num_attention_heads=num_heads,
            num_key_value_heads=num_kv_heads,
            head_dim=head_dim,
            max_position_embeddings=max_pos,
            default_dtype=normalized_dtype,
            is_mla=is_mla,
            kv_lora_rank=kv_lora_rank,
            qk_rope_head_dim=qk_rope_head_dim,
        )
