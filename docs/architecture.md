# Architecture

## System overview

```mermaid
flowchart TD
    CF[Cloudflare Tunnel\ncloudflared 2 replicas] --> Ingress[Ingress-NGINX\nClusterIP + SSE streaming]
    Ingress --> CP[Control Plane API\nFastAPI :8000]
    Ingress --> GW[LiteLLM Proxy\n:4000]
    CP --> AT[Auto-Tuner & VRAM Engine\nHF inspect + sizing]
    CP --> Warmer[Warmer Jobs\none per GPU hostname -> /mnt/nvme]
    CP --> Reconciler[Dynamo Reconciler\nDGD + KEDA ScaledObjects]
    Reconciler --> CRD[DynamoGraphDeployment\nnvidia.com/v1beta1]
    CRD --> PW[Prefill clique\nTP + chunked prefill]
    CRD --> DW[Decode clique\nTP + NIXL KV transfer]
    DW -. EndpointSlice Ready .-> Sync[LiteLLM sync\nPOST /model/new]
    Sync --> GW
    KEDA[KEDA ScaledObjects\nvia DGDScalingAdapter] --> PW
    KEDA --> DW
```

## Components

| Component | Code | Role |
|---|---|---|
| Control Plane API | `control-plane/api/main.py` | REST + SPA at `/`, reconcile-on-startup, deploy/delete orchestration |
| Auth | `control-plane/api/auth.py` | Bearer tokens, Argon2 admin login, signed cookie sessions, CSRF + rate limits |
| K8s client | `control-plane/api/k8s_client.py` | Async CRUD for DGDs, Jobs, ScaledObjects, EndpointSlices, GPU inventory |
| Reconciler | `control-plane/api/dynamo_reconciler.py` | Warmer → DGD → KEDA → EndpointSlice watch → LiteLLM; graceful teardown |
| LiteLLM sync | `control-plane/api/litellm_sync.py` | `POST /model/new` / `/model/delete` with 3-attempt exponential backoff |
| HF inspector | `control-plane/autotuner/hf_inspector.py` | Offline verified profiles + live `config.json` / `safetensors.index.json` reads |
| VRAM calculator | `control-plane/autotuner/vram_calculator.py` | Weight/KV/total VRAM math, TP/PP selection |
| SLA planner | `control-plane/autotuner/sla_planner.py` | Profile → replicas, P/D ratio, capacity gate (`InsufficientCapacityError` → 409) |
| Model warmer | `loader/model_warmer.py` | `hf_transfer` snapshot download into `/mnt/nvme/model-cache` |
| Metrics | `control-plane/api/metrics.py` | Prometheus-backed usage snapshot + drain checks |

## Namespaces

| Namespace | Contents |
|---|---|
| `dynamo-control-plane` | Control Plane Deployment + Service, `inference-runtime` Secret, RBAC (`infra/control-plane-rbac.yaml`) |
| `dynamo-workloads` | Warmer Jobs, `DynamoGraphDeployment`s, KEDA ScaledObjects, `hf-token-secret` |
| `ai-gateway` | LiteLLM Proxy, `litellm-config` ConfigMap, Postgres StatefulSet, `inference-runtime` Secret |
| `monitoring` | `PrometheusRule` alerts/recording rules; Prometheus + KEDA installed separately |

## Deploy lifecycle

1. `POST /api/v1/models/deploy` → inventory (`get_cluster_gpu_inventory`) →
   `SLAPlanner.plan` (Contract 1).
2. `get_target_gpu_nodes` selects Ready, schedulable GPU hostnames matching the
   plan's SKU and per-pod GPU requirement.
3. One warmer Job per hostname is applied and awaited (30 min timeout each). Any
   failure aborts the deploy.
4. DGD (`nvidia.com/v1beta1`, `spec.components`) is applied; for disaggregated
   plans the two KEDA ScaledObjects are applied.
5. Background task polls the `<name>-frontend` EndpointSlice (up to 1800 s),
   then `register_model` in LiteLLM. Status becomes `running`.
6. `DELETE /api/v1/models/{name}` → `deregister_model` (failure **blocks**
   deletion) → dynamic drain (poll `num_requests_running` until 0, max 120 s) →
   delete ScaledObjects → delete DGD.

State is recovered from cluster CRs on startup (`reconcile_existing`); the
in-memory cache is only a status overlay.
