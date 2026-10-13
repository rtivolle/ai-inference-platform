FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application and templates
COPY autotuner/ /app/autotuner/
COPY api/ /app/api/
COPY ../contracts/ /contracts/
COPY ../dynamo/templates/ /dynamo/templates/

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
