FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 HF_HOME=/work/hf HOME=/work
WORKDIR /app
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
COPY training/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir torch==2.8.0 --index-url ${TORCH_INDEX_URL} \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home trainer
COPY training/train.py /app/train.py
COPY training/smoke_test.py /app/smoke_test.py
USER 10001
ENTRYPOINT ["python", "/app/train.py"]
