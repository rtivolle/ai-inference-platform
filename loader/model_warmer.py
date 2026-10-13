#!/usr/bin/env python3
"""
Model Warmer: Fast parallel model downloader for local NVMe cache.
Utilizes huggingface_hub with hf_transfer to maximize NVMe and network throughput.
"""

import argparse
import logging
import os
import sys

from huggingface_hub import snapshot_download

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] [MODEL-WARMER] %(message)s")
logger = logging.getLogger("model_warmer")


def warm_model(model_id: str, cache_dir: str, token: str | None = None) -> str:
    logger.info(f"Initiating NVMe pre-caching for model: {model_id}")
    logger.info(f"Target cache directory: {cache_dir}")

    os.makedirs(cache_dir, exist_ok=True)

    # Enable high-speed hf_transfer if installed
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

    try:
        download_path = snapshot_download(
            repo_id=model_id,
            local_dir=os.path.join(cache_dir, model_id.replace("/", "--")),
            token=token or os.environ.get("HF_TOKEN"),
            ignore_patterns=["*.pt", "*.bin", "*.msgpack"] if "--safe" in sys.argv else None,
            max_workers=16,
        )
        logger.info(f"Model {model_id} successfully cached at: {download_path}")
        return download_path
    except Exception as e:
        logger.error(f"Failed to cache model {model_id}: {e}")
        raise


def main():
    parser = argparse.ArgumentParser(description="Pre-cache Hugging Face weights to NVMe storage")
    parser.add_argument("--model-id", type=str, default=os.environ.get("MODEL_ID"), help="HF Model ID")
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=os.environ.get("HF_HOME", "/mnt/nvme/model-cache"),
        help="Target NVMe directory",
    )
    parser.add_argument("--token", type=str, default=os.environ.get("HF_TOKEN"), help="Hugging Face API token")

    args = parser.parse_args()
    if not args.model_id:
        logger.error("No model_id specified. Use --model-id or set MODEL_ID environment variable.")
        sys.exit(1)

    warm_model(args.model_id, args.cache_dir, args.token)


if __name__ == "__main__":
    main()
