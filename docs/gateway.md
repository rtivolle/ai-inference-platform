# LiteLLM Gateway

Full installer guide: `gateway/README.md`. Manifests: `gateway/litellm-deployment.yaml`,
`gateway/config.yaml`, `gateway/config.example.yaml`. Sync: `control-plane/api/litellm_sync.py`.

## Install (one cluster)

```bash
python -m scripts.install_litellm --context "$KUBE_CONTEXT" --image "$LITELLM_IMAGE"
```

`$LITELLM_IMAGE` must be `repository@sha256:<64-hex>` with Postgres support.
The installer:

- Creates `ai-gateway` + `dynamo-control-plane`, generates matching
  `litellm-master-key` + control-plane `api-token`, preserves them on reruns,
  keeps the legacy salt fallback (salt defaults to old master key). Never rotate
  the salt after models exist; mismatched keys or DB changes fail fast.
- Bundles Postgres 16 StatefulSet (10 Gi PVC, default StorageClass; `--storage-class`
  to override) unless `DATABASE_URL` (`postgresql://…`) is set for an external DB.
- Renders LiteLLM from `config.example.yaml`: **empty `model_list`** (models live
  in DB via `store_model_in_db: true`), `least-busy` routing, 600 s timeout.
- Sets `STORE_MODEL_IN_DB=True`, `PROXY_CONFIG_RELOAD_INTERVAL_SECONDS=10`,
  `DATABASE_URL`, `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`; waits ≤10 min per rollout.
- The control-plane manifest consumes the same master key/token from
  `inference-runtime` — deploy it after gateway readiness.

Bootstrap hook: set `LITELLM_IMAGE` (+ optional `LITELLM_KUBE_CONTEXT`,
`PYTHON_BIN`) and `bootstrap_inference_cluster.sh` installs the gateway.

## Runtime sync lifecycle

- Ready trigger: `<deployment>-frontend` EndpointSlice `ready=true` →
  `POST {LITELLM_PROXY_URL}/model/new` (3 attempts, `2^n` backoff). Deregister:
  `POST /model/delete {"id": "dgd-<deployment>"}` — deregistration failure
  **blocks** Kubernetes deletion.
- Model payload uses `openai/<hf-id>`, `api_base:
  http://<svc>.dynamo-workloads.svc.cluster.local:8000/v1`, `rpm: 1000`,
  `timeout: 600`, `model_info.id: dgd-<name>`.
- All LiteLLM replicas reload DB models every 10 s; deletion drains until
  `num_requests_running == 0` (max 120 s) to avoid 502s.
- Probes: liveness `/health/liveliness`, readiness `/health/readiness`.

## Multi-cluster note

Each cluster has its own DB + credentials (`deploy/README.md`). The release
pipeline drops the ephemeral Postgres Deployment/Service and requires a
persistent `database-url` in `ai-gateway/inference-runtime`.
