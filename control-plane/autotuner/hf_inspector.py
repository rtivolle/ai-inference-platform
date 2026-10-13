"""
Hugging Face model metadata inspector.
Downloads config.json without downloading weights and extracts model architecture specifications.
"""

import json
import logging
from typing import Optional
from huggingface_hub import hf_hub_download
from .vram_calculator import ModelArchitectureSpecs

logger = logging.getLogger("hf_inspector")

# Predefined architecture profiles for well-known models (guarantees offline test reliability)
KNOWN_MODELS = {
    "meta-llama/Llama-3.3-70B-Instruct": ModelArchitectureSpecs(
        num_params=70_553_706_496,
        num_hidden_layers=80,
        hidden_size=8192,
        num_attention_heads=64,
        num_key_value_heads=8,
        max_position_embeddings=131072,
        default_dtype="bf16",
    ),
    "meta-llama/Meta-Llama-3-70B-Instruct": ModelArchitectureSpecs(
        num_params=70_553_706_496,
        num_hidden_layers=80,
        hidden_size=8192,
        num_attention_heads=64,
        num_key_value_heads=8,
        max_position_embeddings=8192,
        default_dtype="bf16",
    ),
    "meta-llama/Meta-Llama-3-8B-Instruct": ModelArchitectureSpecs(
        num_params=8_030_261_248,
        num_hidden_layers=32,
        hidden_size=4096,
        num_attention_heads=32,
        num_key_value_heads=8,
        max_position_embeddings=8192,
        default_dtype="bf16",
    ),
    "mistralai/Mistral-7B-Instruct-v0.3": ModelArchitectureSpecs(
        num_params=7_248_023_552,
        num_hidden_layers=32,
        hidden_size=4096,
        num_attention_heads=32,
        num_key_value_heads=8,
        max_position_embeddings=32768,
        default_dtype="bf16",
    ),
    "Qwen/Qwen2.5-72B-Instruct": ModelArchitectureSpecs(
        num_params=72_706_096_640,
        num_hidden_layers=80,
        hidden_size=8192,
        num_attention_heads=64,
        num_key_value_heads=8,
        max_position_embeddings=131072,
        default_dtype="bf16",
    ),
}

class HFInspector:
    def __init__(self, token: Optional[str] = None):
        self.token = token

    def inspect(self, model_id: str) -> ModelArchitectureSpecs:
        if model_id in KNOWN_MODELS:
            logger.info(f"Using known profile for {model_id}")
            return KNOWN_MODELS[model_id]

        logger.info(f"Fetching config.json from Hugging Face for {model_id}...")
        try:
            config_file = hf_hub_download(
                repo_id=model_id,
                filename="config.json",
                token=self.token
            )
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            return self._parse_config(config)
        except Exception as e:
            logger.warning(f"Could not download config.json for {model_id} ({e}). Estimating based on model ID.")
            return self._estimate_from_id(model_id)

    def _parse_config(self, config: dict) -> ModelArchitectureSpecs:
        hidden_size = config.get("hidden_size", 4096)
        num_layers = config.get("num_hidden_layers", config.get("n_layer", 32))
        num_heads = config.get("num_attention_heads", config.get("n_head", 32))
        num_kv_heads = config.get("num_key_value_heads", num_heads)
        max_pos = config.get("max_position_embeddings", 32768)
        dtype = config.get("torch_dtype", "bf16")

        # Estimate parameters if not explicitly provided
        vocab_size = config.get("vocab_size", 128256)
        intermediate_size = config.get("intermediate_size", int(hidden_size * 3.5))

        # Transformer parameter approximation
        attn_params = 4 * (hidden_size ** 2)
        mlp_params = 3 * hidden_size * intermediate_size
        layer_params = attn_params + mlp_params
        total_params = (num_layers * layer_params) + (2 * vocab_size * hidden_size)

        return ModelArchitectureSpecs(
            num_params=total_params,
            num_hidden_layers=num_layers,
            hidden_size=hidden_size,
            num_attention_heads=num_heads,
            num_key_value_heads=num_kv_heads,
            max_position_embeddings=max_pos,
            default_dtype=dtype,
        )

    def _estimate_from_id(self, model_id: str) -> ModelArchitectureSpecs:
        id_lower = model_id.lower()
        if "70b" in id_lower or "72b" in id_lower:
            return KNOWN_MODELS["meta-llama/Llama-3.3-70B-Instruct"]
        elif "8b" in id_lower:
            return KNOWN_MODELS["meta-llama/Meta-Llama-3-8B-Instruct"]
        else:
            return KNOWN_MODELS["mistralai/Mistral-7B-Instruct-v0.3"]
