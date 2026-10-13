# Quickstart

## 1. Prerequisites

- Python 3.11 (matches CI and container images)
- Docker (optional, for containerized mock run)
- No cluster needed for the mock path below.

## 2. Run the control plane locally (mock)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
CONTROL_PLANE_MOCK=1 uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. When login is not configured, use the mock
Bearer token `test-token`. The banner reads "Mock environment".

Docker equivalent:

```bash
docker build -t ai-inference-control-plane:local -f control-plane/Dockerfile .
docker run --rm -p 127.0.0.1:8000:8000 -e CONTROL_PLANE_MOCK=1 ai-inference-control-plane:local
```

Interactive API reference is at `http://127.0.0.1:8000/docs`.

## 3. Deploy your first model (mock)

Console path (see [Web Console](web-console.md)):

1. Sign in with `test-token` (mock) or your admin session.
2. Pick a known profile (works offline, e.g. `meta-llama/Llama-3.3-70B-Instruct`)
   or type any HF id (requires network — `HFInspector` calls Hugging Face).
3. Set profile (`balanced` default), context length, concurrency, memory
   utilization; submit. Status moves `provisioning` → `running`.

API path:

```bash
TOKEN=test-token
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/cluster/gpus | jq .
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"model_id":"mistralai/Mistral-7B-Instruct-v0.3","profile":"balanced"}' \
  http://127.0.0.1:8000/api/v1/models/deploy | jq .
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/models | jq .
```

Delete:

```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v1/models/mistral-7b-instruct-v0-3 | jq .
```

## 4. Run the tests

```bash
CONTROL_PLANE_MOCK=1 pytest -v tests/
shellcheck scripts/*.sh
```

## 5. Next steps

- Real cluster: [Infrastructure & Bootstrap](infrastructure.md), then
  [LiteLLM Gateway](gateway.md) and [Multi-Cluster Releases](multi-cluster.md).
- Understand sizing failures (HTTP 409): [Auto-Tuner](autotuner.md).
- Understand what gets created in Kubernetes: [Dynamo Deployments](dynamo-deployments.md).
