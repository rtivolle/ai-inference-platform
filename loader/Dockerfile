FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    HF_HOME=/mnt/nvme/model-cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir huggingface_hub[hf_transfer]==0.25.0 hf_transfer==0.1.8
RUN useradd -u 1000 -m appuser

WORKDIR /app
COPY model_warmer.py /app/model_warmer.py
USER 1000:1000

ENTRYPOINT ["python", "/app/model_warmer.py"]
