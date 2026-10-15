# Follow-Up Execution Plan & Upstream Integration Roadmap

This document synthesizes the findings from a full-repository code review of the AI Inference Platform (`control-plane/`, `dynamo/`, `loader/`, `gateway/`, `infra/`, `monitoring/`, `training/`, `scripts/`, `contracts/`, and `tests/`) and an online verification of modern upstream integrations (2025–2026).

---

## 1. Current Baseline Summary

- **Completed Milestones**:
  - **Waves 1–3 & Wave R5 (Pre-Cluster Readiness)**: Unified `pyproject.toml` packaging, analytical VRAM/SLA sizing (`control-plane/autotuner/`), NVIDIA Dynamo `v1.5.1` (`nvidia.com/v1beta1`) Jinja2 templates with `NixlConnector` (`dynamo/templates/`), per-node NVMe warmer Jobs (`loader/`), KEDA `ScaledObject` lifecycle management (`monitoring/`), secret-backed LiteLLM proxy manifests (`gateway/`), and `kubeconform v0.7.0` schema validation (`scripts/validate_manifests.py`).
  - **Platform Extensions**: Vanilla JS SPA web console (`control-plane/api/web/`), Argon2id/CSRF session authentication (`control-plane/api/auth.py`), live Prometheus usage telemetry (`control-plane/api/metrics.py`), Kubernetes pod/scale operational controls (`control-plane/api/k8s_controls.py`), multi-cluster immutable digest deployment (`scripts/deploy_clusters.py`), and opt-in inference capture + single-GPU LoRA fine-tuning (`control-plane/api/data_*.py`, `control-plane/api/training*.py`, `training/train.py`).
  - **Forensic Audit & Test Suite**: 43 defects cataloged in [`docs/audit-defect-catalog.md`](audit-defect-catalog.md), with 18 High/Critical PoC tests in `tests/test_audit_defects_poc.py` and **145/145 total pytest tests passing**.

---

## 2. Phase 1 — Codebase Consistency & Remaining Audit Remediations

Full-codebase inspection identified two cross-module regressions introduced between recent feature additions and audit patches, plus several medium/low defense-in-depth items from [`docs/audit-defect-catalog.md`](audit-defect-catalog.md) that should be closed before Wave 4 cluster rollout.

| ID | Priority | Target Files | Issue Discovered | Actionable Remediation |
|---|---|---|---|---|
| **P1-01** | **Critical** | `dynamo/templates/dgd-components.yaml.j2:70`<br>`loader/model_warmer.py:26`<br>`tests/test_training_promotion.py:34` | **Pinned revision cache path mismatch**: `model_warmer.py` (after `DEF-LOADER-02`) writes pinned revisions to `/mnt/nvme/model-cache/hub/models--<org>--<repo>/snapshots/<revision>`, whereas `dgd-components.yaml.j2` still passes `/mnt/nvme/model-cache/<org>--<repo>/<revision>` to `--model`. Promoted LoRA runs fail to locate weights on startup. | Update `dgd-components.yaml.j2` when `model_revision` is set to pass `--model {{ model_id | tojson }} --revision {{ model_revision | tojson }}` (relying on `HF_HOME=/mnt/nvme/model-cache`), or point `--model` to `'/mnt/nvme/model-cache/hub/models--' ~ (model_id \| replace('/', '--')) ~ '/snapshots/' ~ model_revision`. Update `test_training_promotion.py` accordingly. |
| **P1-02** | **High** | `infra/data-workflow.yaml:28`<br>`control-plane/api/training.py:90` | **Missing `jobs/status` RBAC in `inference-training`**: `TrainingService.status()` calls `BatchV1Api.read_namespaced_job_status()`, but `Role/training-controller` in `infra/data-workflow.yaml` only grants `resources: [jobs]`. Live clusters will reject status polling with HTTP `403 Forbidden`. | Add `jobs/status` to `Role/training-controller` under `apiGroups: [batch]` (`resources: [jobs, jobs/status]`, `verbs: [create, get, delete]`), matching `infra/control-plane-rbac.yaml`. |
| **P1-03** | **High** | `control-plane/api/auth.py:155-158` | **Shared-IP login lockout (`DEF-API-04`)**: Rate limiting hashes only `client_ip`. Behind a shared NAT or misconfigured tunnel without `cf-connecting-ip`, 5 failed login attempts lock out all administrators globally. | Scope the rate-limit key by both client IP and normalized username (`hashlib.sha256(f"{client_ip}\0{payload.username.strip().lower()}".encode()).hexdigest()`). |
| **P1-04** | **Medium** | `control-plane/api/auth.py:99-106` | **Permissive empty-Origin CSRF check (`DEF-API-05`)**: `check_origin()` only validates `Origin`/`Referer` when present (`if origin:`). Cookie-authenticated `POST`/`DELETE` requests that strip both headers bypass origin verification. | Require `Origin` or `Referer` to be present on all cookie-authenticated non-`GET`/`HEAD`/`OPTIONS` requests (`if not origin: raise HTTPException(403, "Request origin header is required.")`). |
| **P1-05** | **Medium** | `control-plane/autotuner/vram_calculator.py:55, 99, 138` | **Multi-GPU CUDA graph scaling & PP divisibility (`DEF-AUTOTUNER-03/04`)**: `cuda_graphs_bytes` (2.0 GiB) is added once globally rather than per TP rank (`tp * cuda_graphs_bytes`), and PP search does not require `num_hidden_layers % pp == 0`. | Pass `tp` into per-rank overhead calculations and enforce `specs.num_hidden_layers % pp == 0` in `compute_parallelism()`. |
| **P1-06** | **Medium** | `control-plane/api/main.py:209`<br>`control-plane/api/dynamo_reconciler.py:114`<br>`control-plane/api/web/app.js:451` | **Synchronous 30-min deploy request vs Cloudflare timeout (`DEF-WEB-01`)**: `POST /api/v1/models/deploy` awaits warmer Job completion inline (up to 1800s), which will be terminated by Cloudflare Tunnel's ~100s HTTP idle timeout in production. | Return `202 Accepted` with `status: "warming"` immediately after node selection and move warmer Job execution + DGD creation into the background reconciliation task tracked via `GET /api/v1/models/{name}/status`. |
| **P1-07** | **Medium** | `monitoring/prometheus-rules.yaml:12-26` | **PromQL metric scale & label alignment (`DEF-MON-02/03`)**: Recording rule `vllm:gpu_cache_usage_perc` records `vllm_gpu_cache_usage_factor` (0–1 ratio) while `metrics.py` handles both `vllm:kv_cache_usage_perc` and `vllm:gpu_cache_usage_perc` alongside `dynamo_component` labels. | Preserve `dynamo_component` in `prometheus-rules.yaml` `sum by` / `max by` aggregations and include `vllm:kv_cache_usage_perc` fallback for vLLM v1 compatibility. |

---

## 3. Phase 2 — Verified Upstream Integration Upgrades (2025–2026)

Online verification against current upstream documentation confirms several higher-performance, lower-maintenance integrations that can replace or simplify custom platform glue code.

### 3.1 Summary Matrix of Current vs. Recommended Integrations

| Subsystem | Current Implementation | Verified Upstream Integration | Primary Operational Benefit |
|---|---|---|---|
| **Hugging Face Weight Downloader (`loader/`)** | `huggingface_hub==0.25.0` + legacy `hf_transfer==0.1.8` (`HF_HUB_ENABLE_HF_TRANSFER=1`) | **`hf_xet` (`HF_XET_HIGH_PERFORMANCE=1`)** in `huggingface_hub>=0.30+` ([HF Download Guide](https://huggingface.co/docs/huggingface_hub/en/guides/download), [HF Upload Guide](https://huggingface.co/docs/huggingface_hub/en/guides/upload)) | Replaces deprecated LFS-only `hf_transfer` with Rust Xet chunk-deduplicated transfers. |
| **vLLM Cold-Start & Weight Streaming (`dynamo/templates/`)** | Custom per-node warmer `Job` downloading full repo before DGD creation; no `torch.compile` cache | **vLLM Native Run:ai Model Streamer** (`--load-format runai_streamer`) + persistent `VLLM_CACHE_ROOT` ([AWS EKS Fast Model Loading](https://aws.amazon.com/blogs/containers/fast-model-loading-for-ai-inference-on-amazon-eks/), [EKS User Guide](https://docs.aws.amazon.com/eks/latest/userguide/ml-inference-fast-model-loading.html), [vLLM Compile Caching](https://route179.dev/2026/06/30/optimizing-vllm-cold-start-with-model-streaming-and-compile-caching/)) | Streams SafeTensors shards in parallel directly from S3 or local NVMe into GPU VRAM and caches `torch.compile` artifacts on NVMe, cutting 67–203 GiB cold starts from 82–463s to **16–32s**. |
| **Disaggregated KV-Cache & Prefix Offloading (`dynamo/templates/`)** | `NixlConnector` with symmetric `"kv_role":"kv_both"` on both prefill and decode | **Explicit `kv_producer`/`kv_consumer` roles + LMCache (`MultiConnector`)** ([vLLM NixlConnector Guide](https://docs.vllm.ai/en/stable/features/nixl_connector_usage/), [LMCache Disaggregated Prefill](https://docs.lmcache.ai/mp/disaggregated_prefill.html), [LMCache K8s Operator](https://docs.lmcache.ai/mp/operator.html)) | Enforces directional P2P RDMA transfers and adds cross-request KV prefix caching + NVMe offloading to reduce prefill recomputation. |
| **Disaggregated Autoscaling (`monitoring/` & `dynamo/`)** | Independent KEDA `ScaledObject`s for prefill and decode polling Prometheus every 15s | **NVIDIA Dynamo SLA Planner (`type: planner`)** targeting `DynamoGraphDeploymentScalingAdapter` ([Dynamo DGD Reference](https://docs.nvidia.com/dynamo/dev/reference/api/kubernetes/dynamo-graph-deployment), [Dynamo API Reference](https://docs.nvidia.com/dynamo/reference/api/kubernetes/full-api-reference)) | Jointly optimizes Prefill and Decode replica ratios from live ISL/OSL telemetry and TTFT/ITL targets, preventing independent P/D scaling oscillation. |
| **Inference Routing & Endpoint Selection (`dynamo/` & `infra/`)** | `dynamo.frontend` service behind `Ingress-NGINX` | **Kubernetes Gateway API Inference Extension (GAIE) + Dynamo EPP** ([NVIDIA GAIE Overview](https://docs.nvidia.com/dynamo/v1.3.0/kubernetes-deployment/request-routing/gateway-api-inference-extension/overview), [kubernetes-sigs/gateway-api-inference-extension](https://github.com/kubernetes-sigs/gateway-api-inference-extension)) | Standardizes `InferencePool` and `InferenceModel` CRDs with Endpoint Picker (EPP) routing based on real-time KV-cache locality, queue depth, and LoRA adapter residency. |
| **LiteLLM Multi-Replica Coordination (`gateway/`)** | 2 LiteLLM replicas polling Postgres every 10s (`least-busy` without Redis); imperative HTTP sync | **LiteLLM + Redis Coordination & Resilience Flags** ([LiteLLM Prod Best Practices](https://docs.litellm.ai/docs/proxy/prod), [LiteLLM Redis Requirements](https://docs.litellm.ai/docs/proxy/redis_requirements), [High Throughput Guide](https://docs.litellm.ai/docs/proxy/high_throughput)) + optional [PalenaAI/litellm-operator](https://github.com/PalenaAI/litellm-operator) | Shares `least-busy` in-flight request counters and RPM state across replicas via Redis and keeps serving when Postgres is unreachable (`allow_requests_on_db_unavailable: true`). |
| **Cloudflare Edge & Cluster Ingress (`infra/cloudflare/`)** | Static `cloudflared` ConfigMap + `Ingress-NGINX` + cross-namespace `ExternalName` Service shims | **Kubernetes Gateway API + Cloudflare Tunnel Gateway Controller** ([lexfrei/cloudflare-tunnel-gateway-controller](https://github.com/lexfrei/cloudflare-tunnel-gateway-controller), [pl4nty/cloudflare-kubernetes-gateway](https://github.com/pl4nty/cloudflare-kubernetes-gateway)) | Eliminates `Ingress-NGINX` and `ExternalName` workarounds via native cross-namespace `HTTPRoute` + `ReferenceGrant` and automated Cloudflare tunnel route reconciliation. |
| **Fine-Tuned Model Promotion (`training/` & `control-plane/api/`)** | Full weight merge (`merge_and_unload()`) + new Hub repo + separate full-GPU DGD deployment | **Dynamic vLLM LoRA Adapter Serving (`--enable-lora`)** | Serves multiple fine-tuned LoRA adapters on a single shared base-model DGD without duplicating base weights or GPU cliques. |

### 3.2 Concrete Integration Upgrade Specifications

#### Upgrade 1: `hf_xet`, Run:ai Model Streamer, and Persistent `torch.compile` Cache
1. **Loader Container (`loader/Dockerfile` & `loader/model_warmer.py`)**:
   - Upgrade `huggingface_hub` to `>=0.30.0` with `hf_xet` (`pip install "huggingface_hub[hf_xet]>=0.30.0"`) and set `HF_XET_HIGH_PERFORMANCE=1` alongside `HF_HUB_ENABLE_HF_TRANSFER=1` for backwards compatibility ([Hugging Face Download Guide](https://huggingface.co/docs/huggingface_hub/en/guides/download)).
2. **vLLM Worker Args & Compile Cache (`dynamo/templates/dgd-components.yaml.j2`)**:
   - Add `--load-format=runai_streamer` and `--model-loader-extra-config={"concurrency":16}` when `runai-model-streamer` is present in the Dynamo runtime image ([Amazon EKS Fast Model Loading](https://docs.aws.amazon.com/eks/latest/userguide/ml-inference-fast-model-loading.html)).
   - Mount `/mnt/nvme/model-cache/vllm-compile-cache` via `env: [{name: VLLM_CACHE_ROOT, value: /mnt/nvme/model-cache/vllm-compile-cache}]` so `torch.compile` and CUDA graph artifacts persist across pod restarts on the same node ([AWS Containers Blog](https://aws.amazon.com/blogs/containers/fast-model-loading-for-ai-inference-on-amazon-eks/)).

#### Upgrade 2: Explicit NIXL Roles & Optional LMCache Prefix Offloading
1. **Directional NIXL Roles (`dynamo/templates/dgd-components.yaml.j2:96-100`)**:
   - Replace hardcoded `"kv_role":"kv_both"` with role-specific values per [vLLM NixlConnector Usage Guide](https://docs.vllm.ai/en/stable/features/nixl_connector_usage/):
     - Prefill component: `--kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_producer","kv_buffer_device":"cuda"}'`
     - Decode component: `--kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_consumer","kv_buffer_device":"cuda"}'`
2. **LMCache MultiConnector (Optional High-Reuse Profile)**:
   - For workloads with shared system prompts or multi-turn RAG prefixes, support `MultiConnector` combining `NixlConnector` and `LMCacheMPConnector` backed by local NVMe ([LMCache Disaggregated Prefill](https://docs.lmcache.ai/mp/disaggregated_prefill.html)).

#### Upgrade 3: LiteLLM Redis State & Database Fault Tolerance
1. **Gateway Configuration (`gateway/config.example.yaml` & `gateway/litellm-deployment.yaml`)**:
   - Per [LiteLLM Production Best Practices](https://docs.litellm.ai/docs/proxy/prod) and [What Needs Redis](https://docs.litellm.ai/docs/proxy/redis_requirements), add optional Redis configuration and DB resilience settings:
     ```yaml
     general_settings:
       master_key: "os.environ/LITELLM_MASTER_KEY"
       database_url: "os.environ/DATABASE_URL"
       store_model_in_db: true
       allow_requests_on_db_unavailable: true
       proxy_batch_write_at: 10

     router_settings:
       routing_strategy: "least-busy"
       redis_host: "os.environ/REDIS_HOST"
       redis_port: 6379
     ```

---

## 4. Phase 3 — Waves 4 & 5 Execution Plan (CI Kind Gate, Bare-Metal Rollout & Live E2E)

### Wave 4A: CI Kind Integration Gate (Completing `D11`)
1. Add a `kind-integration` job in `.github/workflows/ci.yaml`:
   - Provision a single-node Kind cluster (`v1.31.0`).
   - Apply the upstream CRDs (`nvidia.com_dynamographdeployments.yaml`, `nvidia.com_dynamographdeploymentscalingadapters.yaml`, `keda.sh_scaledobjects.yaml`) and `infra/control-plane-rbac.yaml`.
   - Label the Kind node with simulated GFD labels (`nvidia.com/gpu.product=NVIDIA-H100-80GB`, `nvidia.com/gpu.memory=81920`) and patch node status allocatable GPUs (`nvidia.com/gpu: "8"`).
   - Run the Control Plane with `CONTROL_PLANE_MOCK=0` against the real Kind API server to verify live CRD creation, Job status polling, RBAC permissions, KEDA `ScaledObject` creation, and teardown.

### Wave 4B: Bare-Metal Cluster Bootstrap & Multi-Cluster Release
1. Execute `sudo ./scripts/bootstrap_inference_cluster.sh` on Ubuntu 22.04/24.04 GPU nodes.
2. Verify hardware prerequisites:
   - `nvidia-smi topo -m` (NVLink + NIC affinity).
   - `ibv_devices` and `ibv_devinfo` (`PORT_ACTIVE`, MTU 4096).
   - `kubectl get nodes -o json | jq '.items[].status.allocatable["rdma/rdma_shared_device"]'`.
3. Install NVIDIA Dynamo Platform v1.5.1 Helm chart with `dynamo/helm-values/dynamo-platform-values.yaml`.
4. Provision `inference-runtime` and `registry-credentials` Secrets and execute `scripts/deploy_clusters.py --apply`.

### Wave 5: Live Disaggregated E2E & Streaming Load Validation
1. Deploy `meta-llama/Llama-3.3-70B-Instruct` (`profile: "low-latency"`, `quantization: "fp8"`) via `POST /api/v1/models/deploy`.
2. Confirm multi-node NVMe warming completion, DGD `v1beta1` pod startup, NIXL RDMA KV transfer initialization, and automatic registration in LiteLLM (`GET /v1/models`).
3. Run SSE streaming load tests through `https://llm.<domain>/v1/chat/completions` to verify:
   - `TTFT p95 <= 350 ms` and target `TPOT` under concurrent load.
   - KEDA `ScaledObject` (or Dynamo Planner) scale-up and scale-down transitions on `DynamoGraphDeploymentScalingAdapter`.
   - Zero dropped requests (`0` HTTP `502` errors) when calling `DELETE /api/v1/models/{name}` during active generation.
