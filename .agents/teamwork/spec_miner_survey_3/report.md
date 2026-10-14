# Specification Mining & Codebase Audit Report: Survey 3
**Author**: Spec Miner Survey 3  
**Date**: 2026-10-13  
**Assigned Scope**: `contracts/`, `scripts/`, `loader/`, `infra/`, `monitoring/`, and template alignment with `dynamo/templates/`

---

## 1. Executive Summary

A comprehensive, evidence-based audit was performed across the platform contracts, automation scripts, model loader container, infrastructure manifests, RBAC policies, and Prometheus/KEDA monitoring assets. 

Key high-level findings include:
1. **Contract & Template Mismatch**: The Jinja2 workload template (`dynamo/templates/dgd-components.yaml.j2`) crashes with an `UndefinedError` when the optional `kv_cache_transfer` plan field is omitted, directly violating the optionality guaranteed by `contracts/model_spec.schema.json`. Furthermore, `/api/v1/models/{model_name}/status` lacks authentication enforcement despite being declared authenticated in `contracts/control_plane_openapi.yaml`.
2. **Critical Script Flaws**: The primary bare-metal bootstrap script (`scripts/bootstrap_inference_cluster.sh`) relies on relative file paths without referencing `$REPO_ROOT`, causing all infrastructure deployments to be silently skipped when run from outside the repo directory; it also suppresses critical failures with `|| true` and sets dangerous `0777` world-writable permissions on the NVMe model cache.
3. **Loader Crash & Cache Incompatibility**: The model warmer (`loader/model_warmer.py`) crashes with an `unrecognized arguments: --safe` error because the CLI parser omitted `--safe`. Furthermore, it stores models in a custom non-standard directory rather than the standard Hugging Face Hub cache layout expected by vLLM, which forces runtime re-downloads.
4. **Infra RBAC & Networking Errors**: Control plane deployment manifests omit the `serviceAccountName`, preventing K8s API operations when deployed directly; RBAC roles omit `jobs/status` required by `read_namespaced_job_status`; ExternalDNS lacks an RBAC ServiceAccount; and the Cloudflare ingress manifest attempts invalid cross-namespace service routing to `ai-gateway` and `dynamo-control-plane`.
5. **Monitoring Label Desynchronization**: KEDA ScaledObject trigger queries rely on a non-existent `dynamo_namespace` label on frontend latency metrics, and Prometheus recording rules query DCGM metrics by `model_name`, which is absent from raw node-level exporter telemetry.

---

## 2. Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| F01 | Contracts | Dynamo CRD Schema Validation | Strict JSON Schema (draft 2020-12) validating `nvidia.com/v1beta1` `DynamoGraphDeployment` manifests | Rendered YAML / JSON manifest | Validated boolean | `jsonschema.ValidationError` on schema violation | `contracts/dynamo_crd.schema.json` |
| F02 | Contracts | Model Deployment Specification | Defs for `DeployRequest`, `ClusterInventory`, and `DeploymentPlan` | Request or Plan JSON object | Validated schema instance | `jsonschema.ValidationError` | `contracts/model_spec.schema.json` |
| F03 | Contracts | Control Plane OpenAPI 3.1.0 | REST API contract defining authenticated endpoints for deployments, inventory, and health | HTTP requests with Bearer or CF tokens | JSON status responses | 400, 401, 404, 409, 422 error payloads | `contracts/control_plane_openapi.yaml` |
| F04 | Templates | Aggregated Workload Rendering | Jinja2 template rendering single worker component with frontend | Plan dictionary with `worker_clique` | K8s `DynamoGraphDeployment` manifest | Jinja2 syntax/rendering errors | `dynamo/templates/dgd-aggregated.yaml.j2` |
| F05 | Templates | Disaggregated Workload Rendering | Jinja2 template rendering disaggregated `prefill` and `decode` cliques | Plan dictionary with `prefill_clique`, `decode_clique`, `kv_cache_transfer` | K8s `DynamoGraphDeployment` manifest | Jinja2 syntax/rendering errors | `dynamo/templates/dgd-disaggregated.yaml.j2` |
| F06 | Scripts | Bare-Metal Cluster Bootstrap | Bash script configuring sysctl, K3s, GPU operator, RDMA operator, Ingress, and Cloudflare | Environment variables (`K3S_VERSION`, `MODEL_CACHE_DIR`, `LITELLM_IMAGE`) | Configured bare-metal node | `set -e` exit on uncaught command failure | `scripts/bootstrap_inference_cluster.sh` |
| F07 | Scripts | Multi-Cluster Release Renderer | Deploys immutable SHA256-pinned releases across inventory clusters | Inventory JSON, image digests, `--apply` flag | Rendered multi-cluster YAML/JSON | Raises `ValueError` or `RuntimeError` | `scripts/deploy_clusters.py` |
| F08 | Scripts | LiteLLM Automated Installer | Provisions LiteLLM StatefulSet/Deployment and creates shared credentials | Kubeconfig context, image digest, storage class | `ai-gateway` and `dynamo-control-plane` K8s secrets and deployments | Raises `ValueError` or `RuntimeError` | `scripts/install_litellm.py` |
| F09 | Scripts | Manifest Validation with Kubeconform | Downloads pinned upstream CRD schemas and runs strict `kubeconform` validation | `--kubeconform` binary path | Kubeconform validation output | Subprocess exit code != 0 on invalid YAML | `scripts/validate_manifests.py` |
| F10 | Loader | Parallel Model Cache Warmer | High-speed Hugging Face snapshot downloader using `hf_transfer` | `--model-id`, `--cache-dir`, `--token`, `--revision` | Downloaded weights on NVMe storage | Exits code 1 on missing model or download failure | `loader/model_warmer.py` |
| F11 | Loader | Model Warmer K8s Job Template | Jinja2 template rendering node-pinned Kubernetes Job for model warming | `warmer_job_name`, `target_node`, `model_id` | `batch/v1` Job manifest | Template syntax errors | `loader/warmer-job.yaml.j2` |
| F12 | Loader | Model Warmer Container Runner | Docker container packaging Python 3.11 with `huggingface_hub[hf_transfer]` | Docker build context | OCI container image | Docker build failure | `loader/Dockerfile` |
| F13 | Infra | Control Plane Deployment & Service | Manifests declaring deployment and ClusterIP service in `dynamo-control-plane` | K8s cluster credentials | Running control plane pod | CrashLoopBackOff if secrets or image missing | `infra/control-plane-deployment.yaml` |
| F14 | Infra | Control Plane RBAC Policy | Roles, RoleBindings, and ClusterRoles for managing DGDs, jobs, pods, and nodes | K8s API server | ServiceAccount permissions | HTTP 403 Forbidden on missing verbs | `infra/control-plane-rbac.yaml` |
| F15 | Infra | Cloudflare Tunnel & ExternalDNS | High availability cloudflared deployment and automated DNS record sync | Cloudflare credentials secret, Ingress | Active Cloudflare tunnel | CrashLoopBackOff on invalid token | `infra/cloudflare/` |
| F16 | Infra | GPU & Network Operators | NVIDIA GPU Operator and Mellanox RDMA Network Operator configurations | Helm charts, values files | Operator daemonsets and CRDs | Failed helm release | `infra/gpu-operator/`, `infra/network-rdma/` |
| F17 | Infra | Local NVMe StorageClass | PV and PVC declarations for local NVMe cache at `/mnt/nvme/model-cache` | Host path, StorageClass | Bound PersistentVolumeClaim | Volume binding pending | `infra/storage-nvme/nvme-storageclass.yaml` |
| F18 | Monitoring | Prometheus Alerting & Recording Rules | Recording rules for vLLM request queuing, KV cache usage, and GPU metrics | Prometheus scraper | Prometheus recorded metrics and alerts | PromQL syntax/evaluation errors | `monitoring/prometheus-rules.yaml` |
| F19 | Monitoring | KEDA Autoscaling Triggers | ScaledObject template configuring prefill and decode autoscaling triggers | Plan dictionary, TTFT and queue thresholds | `keda.sh/v1alpha1` ScaledObject manifests | KEDA trigger evaluation failure | `monitoring/keda-scaledobjects.yaml.j2` |

---

## 3. Edge Cases Probed

| # | Feature | Input | Observed Behavior |
|---|---------|-------|-------------------|
| E01 | `dgd-components.yaml.j2` | Plan without `kv_cache_transfer` attribute | Template rendering crashes: `jinja2.exceptions.UndefinedError: 'kv_cache_transfer' is undefined` |
| E02 | `loader/model_warmer.py` | CLI execution with `--safe` flag | CLI crashes: `error: unrecognized arguments: --safe` (exit code 2) |
| E03 | `control-plane/api/main.py` | `GET /api/v1/models/test/status` without Auth | Endpoint enters route handler unauthenticated, bypassing auth checks (returns 404 instead of 401) |
| E04 | `bootstrap_inference_cluster.sh` | Executed from `/tmp` (`cd /tmp && ./bootstrap...`) | Silently skips all `infra/` manifests because `[[ -f "infra/..." ]]` evaluates against `$PWD` |
| E05 | `bootstrap_inference_cluster.sh` | Execution with faulty/invalid helm chart | Script swallows error via `|| true` and exits with code 0 reporting full success |
| E06 | `infra/control-plane-rbac.yaml` | Worker calling `read_namespaced_job_status` | Fails with HTTP 403 Forbidden because `jobs/status` subresource is omitted from the Role |
| E07 | `infra/cloudflare/ingress-rules.yaml` | External request routed through Ingress-NGINX | Fails with HTTP 503 because `backend.service.name` targets services in other namespaces (`ai-gateway`, `dynamo-control-plane`) |
| E08 | `monitoring/keda-scaledobjects.yaml.j2` | Metric query with `dynamo_namespace` filter | Returns no metric series (NaN) because `dynamo_namespace` label does not exist on scraped metrics |
| E09 | `contracts/model_spec.schema.json` | Request with `max_model_len: 256` | Rejected by `model_spec.schema.json` (min 512) but accepted by `control_plane_openapi.yaml` (min 1) and `sla_planner.py` (`gt=0`) |

---

## 4. Comprehensive Defect Analysis by Area

### Area 1: `contracts/`, API Schemas, and Template Alignment

#### Finding D-01 (High): Jinja2 Crashes on Optional `kv_cache_transfer` in `dgd-components.yaml.j2`
- **Location**: `dynamo/templates/dgd-components.yaml.j2`, lines 112, 120, 125
- **Problem**: In `contracts/model_spec.schema.json`, `kv_cache_transfer` is NOT in the `required` list of `DeploymentPlan` (lines 125–133). However, `dgd-components.yaml.j2` unconditionally evaluates:
  ```jinja2
  {% if kv_cache_transfer.protocol == 'rdma' %}
  ```
- **Mechanism of Failure**: When rendering a plan where `kv_cache_transfer` is not provided (or set to `None`), Jinja2 raises:
  `jinja2.exceptions.UndefinedError: 'kv_cache_transfer' is undefined`.
- **Impact**: Any valid deployment plan that omits `kv_cache_transfer` crashes the reconciler during template evaluation, failing the entire deployment.
- **Remediation**: Guard attribute access:
  ```jinja2
  {% if kv_cache_transfer and kv_cache_transfer.protocol == 'rdma' %}
  ```

#### Finding D-02 (High - Security / Contract): Unauthenticated Deployment Status Route in `control-plane/api/main.py`
- **Location**: `control-plane/api/main.py`, line 200
- **Contract Reference**: `contracts/control_plane_openapi.yaml`, lines 149–155; `AGENTS.md`
- **Problem**: The route `@app.get("/api/v1/models/{model_name}/status", tags=["Deployments"])` lacks the `dependencies=[Depends(verify_auth)]` dependency present on all other `/api/v1/*` endpoints.
- **Impact**: Any unauthenticated client on the network can probe `/api/v1/models/{model_name}/status` to inspect model statuses, model IDs, namespaces, and internal cluster plans without a valid Bearer token or Cloudflare Access header.
- **Remediation**: Add `dependencies=[Depends(verify_auth)]` to `@app.get("/api/v1/models/{model_name}/status")`.

#### Finding D-03 (Medium): OpenAPI Specification Out of Sync with Implemented Endpoints
- **Location**: `contracts/control_plane_openapi.yaml`, lines 1–241 vs `control-plane/api/main.py`, lines 111–142, and `control-plane/api/data_routes.py`
- **Problem**: Several endpoints implemented and active in the codebase are missing from the OpenAPI specification:
  - `/api/v1/metrics/usage`
  - `/api/v1/cluster/nodes`
  - `/api/v1/models/{model_name}/workload`
  - `/api/v1/models/{model_name}/components/{component_name}/scale`
  - `/api/v1/models/{model_name}/pods/{pod_name}/logs`
  - `/api/v1/models/{model_name}/pods/{pod_name}/evict`
  - `/api/v1/data/*` (data capture, reviews, datasets, training, promotion)
- **Impact**: Client SDK generators, API gateways, and contract tests referencing `control_plane_openapi.yaml` have an incomplete view of the API surface.
- **Remediation**: Add the missing operation paths, parameters, and response schemas to `contracts/control_plane_openapi.yaml`.

#### Finding D-04 (Medium): Schema Constraint Inconsistencies Across Contracts and Code
- **Location**:
  - `contracts/model_spec.schema.json:36-45`
  - `contracts/control_plane_openapi.yaml:54-62`
  - `control-plane/autotuner/sla_planner.py:41-43`
- **Problem**:
  - `max_model_len`: `model_spec.schema.json` enforces `minimum: 512, maximum: 131072`; `control_plane_openapi.yaml` enforces `minimum: 1, default: 32768`; `sla_planner.py` uses `Field(default=32768, gt=0)`.
  - `gpu_memory_utilization`: `model_spec.schema.json` enforces `minimum: 0.5, maximum: 0.98`; `control_plane_openapi.yaml` enforces `exclusiveMinimum: 0, maximum: 1`; `sla_planner.py` enforces `gt=0, le=1`.
- **Impact**: Discrepancies lead to situations where valid API inputs fail JSON Schema contract validation or vice versa.
- **Remediation**: Align `model_spec.schema.json`, `control_plane_openapi.yaml`, and `sla_planner.py` validation constraints.

---

### Area 2: `scripts/` (Automation and Bootstrap Scripts)

#### Finding D-05 (High): Relative Paths in `scripts/bootstrap_inference_cluster.sh` Cause Silent Failure
- **Location**: `scripts/bootstrap_inference_cluster.sh`, lines 83, 99, 108, 117, 127, 130, 133
- **Problem**: The script computes `REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"` on line 19, but then executes:
  ```bash
  if [[ -f "infra/storage-nvme/nvme-storageclass.yaml" ]]; then
      kubectl apply -f infra/storage-nvme/nvme-storageclass.yaml || true
  fi
  ```
- **Impact**: If invoked from any directory other than repository root, every `[[ -f "infra/..." ]]` test returns false. All storage classes, GPU operator charts, RDMA configurations, Ingress controllers, and Cloudflare deployments are silently skipped.
- **Remediation**: Prefix all file paths with `"${REPO_ROOT}/"`.

#### Finding D-06 (High): Unconditional `|| true` Masking Bootstrap Failures
- **Location**: `scripts/bootstrap_inference_cluster.sh`, lines 84, 103, 110, 121, 128, 131, 134
- **Problem**: Critical commands use `|| true` (e.g., `kubectl apply -f ... || true`, `helm upgrade --install ... || true`).
- **Impact**: Any error during storage configuration, CRD installation, operator deployment, or Helm installation is completely swallowed. The script reports success despite a broken cluster.
- **Remediation**: Remove `|| true` on required operations or add explicit error checking.

#### Finding D-07 (High - Security): Insecure World-Writable Cache Directory Permissions
- **Location**: `scripts/bootstrap_inference_cluster.sh`, line 52
- **Problem**: `chmod 0777 "${CACHE_DIR}"` sets world-writable permissions without sticky bit on `/mnt/nvme/model-cache`.
- **Impact**: Any unprivileged local user or container running on the node can overwrite, truncate, or inject malicious payloads into cached model weights.
- **Remediation**: Use `chmod 0775 "${CACHE_DIR}"` and ensure appropriate group ownership (e.g. `chown root:inference`).

#### Finding D-08 (Medium): Unexported `KUBECONFIG` in Bootstrap Script
- **Location**: `scripts/bootstrap_inference_cluster.sh`, lines 66–70
- **Problem**: `/etc/rancher/k3s/k3s.yaml` is copied to `/root/.kube/config`, but `export KUBECONFIG=/etc/rancher/k3s/k3s.yaml` is never issued.
- **Impact**: If run via `sudo` preserving a non-root user environment, `kubectl` cannot locate `/root/.kube/config`, causing subsequent commands to fail.
- **Remediation**: Add `export KUBECONFIG=/etc/rancher/k3s/k3s.yaml` after K3s installation.

---

### Area 3: `loader/` (Model Warmer Container & Job Template)

#### Finding D-09 (High): Unhandled `--safe` Flag in `loader/model_warmer.py`
- **Location**: `loader/model_warmer.py`, lines 33, 44–55
- **Problem**: Line 33 inspects `sys.argv`:
  ```python
  ignore_patterns=["*.pt", "*.bin", "*.msgpack"] if "--safe" in sys.argv else None
  ```
  However, `main()` does not register `--safe` in `argparse.ArgumentParser`.
- **Impact**: Executing `python3 model_warmer.py --model-id ... --safe` crashes immediately with `error: unrecognized arguments: --safe` (exit code 2).
- **Remediation**: Add `parser.add_argument("--safe", action="store_true", help="Download only safetensors weights")` to `main()`.

#### Finding D-10 (High): Cache Layout Incompatibility Between Warmer and vLLM
- **Location**: `loader/model_warmer.py:30` vs `dynamo/templates/dgd-components.yaml.j2:71, 92, 104-105`
- **Problem**: `model_warmer.py` passes:
  ```python
  local_dir=os.path.join(cache_dir, model_id.replace("/", "--"))
  ```
  This dumps raw files to `/mnt/nvme/model-cache/<org>--<repo>`. In contrast, `dgd-components.yaml.j2` passes `--model {{ model_id }}` (e.g. `meta-llama/Llama-3.3-70B-Instruct`) and `--download-dir=/mnt/nvme/model-cache` with `HF_HOME=/mnt/nvme/model-cache`. vLLM expects weights in the standard Hugging Face Hub cache layout (`$HF_HOME/hub/models--<org>--<repo>/snapshots/<commit_hash>`).
- **Impact**: vLLM does not find the cached weights and re-downloads the model weights over the network upon pod startup.
- **Remediation**: Use `snapshot_download(repo_id=model_id, cache_dir=cache_dir, ...)` without `local_dir`, or configure vLLM's `--model` argument to point directly to the warmed local directory path.

#### Finding D-11 (High): Lack of Concurrency Control and Corrupted Cache Recovery
- **Location**: `loader/model_warmer.py`, lines 22–35
- **Problem**: `warm_model` downloads directly into target directories without file locking or atomic staging.
- **Impact**: Concurrent warmer jobs targeting the same model or node-level interruptions create partial files that result in silent model loading corruption or failed restarts.
- **Remediation**: Implement a file lock mechanism (e.g. via `filelock`) and download into a `.tmp` staging folder before atomic rename.

#### Finding D-12 (Low): Unpinned Dependencies and Default Root User in `loader/Dockerfile`
- **Location**: `loader/Dockerfile`, lines 1–18
- **Problem**: `RUN pip install --no-cache-dir huggingface_hub[hf_transfer]` does not pin version constraints, and container executes as `root`.
- **Impact**: Vulnerable to supply-chain breaking changes and violates container security best practices.
- **Remediation**: Pin `huggingface_hub` and `hf_transfer` versions; add a dedicated non-root user.

---

### Area 4: `infra/` (Kubernetes Manifests, RBAC & Cloudflare Ingress)

#### Finding D-13 (High): Missing `serviceAccountName` in Control Plane Deployment
- **Location**: `infra/control-plane-deployment.yaml`, lines 22–26
- **Problem**: `infra/control-plane-deployment.yaml` does not specify `serviceAccountName: control-plane` in `spec.template.spec`.
- **Impact**: Applying `infra/control-plane-deployment.yaml` directly runs the control plane with the unprivileged `default` service account, causing immediate 403 Forbidden errors on all Kubernetes operations.
- **Remediation**: Add `serviceAccountName: control-plane` to `spec.template.spec` in `infra/control-plane-deployment.yaml`.

#### Finding D-14 (High): Missing `jobs/status` Permission in `infra/control-plane-rbac.yaml`
- **Location**: `infra/control-plane-rbac.yaml`, lines 34–36 vs `control-plane/api/k8s_client.py:282`
- **Problem**: The Role allows:
  ```yaml
  - apiGroups: [batch]
    resources: [jobs]
    verbs: [get, create, delete]
  ```
  However, `control-plane/api/k8s_client.py:282` calls `batch_api.read_namespaced_job_status(name=job_name, namespace=namespace)`.
- **Impact**: In Kubernetes RBAC, subresources are independent. Accessing `jobs/status` fails with HTTP 403 Forbidden on real clusters.
- **Remediation**: Update rule to:
  ```yaml
  - apiGroups: [batch]
    resources: [jobs, jobs/status]
    verbs: [get, create, delete]
  ```

#### Finding D-15 (High): Missing RBAC Configuration for ExternalDNS
- **Location**: `infra/cloudflare/external-dns.yaml`, lines 1–39
- **Problem**: ExternalDNS is deployed with arguments `--source=ingress` and `--source=service` in namespace `cloudflare`, but no ServiceAccount, ClusterRole, or ClusterRoleBinding is provided.
- **Impact**: ExternalDNS cannot list Ingresses or Services across namespaces, resulting in CrashLoopBackOff or recurring 403 authorization errors.
- **Remediation**: Add a ServiceAccount and ClusterRole granting `get`, `list`, `watch` on `services` and `ingresses`, and bind it to ExternalDNS.

#### Finding D-16 (High): Cross-Namespace Service Routing in `infra/cloudflare/ingress-rules.yaml`
- **Location**: `infra/cloudflare/ingress-rules.yaml`, lines 4–5, 22–25, 32–35
- **Problem**: `ai-platform-ingress` is in namespace `ingress-nginx`, but routes to `litellm-service` (in `ai-gateway`) and `control-plane-service` (in `dynamo-control-plane`).
- **Impact**: Kubernetes Ingress resources cannot target backend services in another namespace without intermediate `ExternalName` services. Ingress-NGINX logs errors that the services do not exist and returns 503 Service Unavailable for all ingress traffic.
- **Remediation**: Deploy `ExternalName` services in `ingress-nginx` pointing to `litellm-service.ai-gateway.svc.cluster.local` and `control-plane-service.dynamo-control-plane.svc.cluster.local`, or deploy separate Ingress resources in the respective application namespaces.

#### Finding D-17 (Medium): Invalid `ReadWriteMany` on Local PersistentVolume
- **Location**: `infra/storage-nvme/nvme-storageclass.yaml`, lines 19–20, 38–39
- **Problem**: `nvme-model-cache-pv` uses `local: path: /mnt/nvme/model-cache` with `accessModes: [ReadWriteMany]`.
- **Impact**: Kubernetes does not support `ReadWriteMany` for local storage plugins; local volumes only support `ReadWriteOnce`. The PV/PVC binding fails or is rejected by strict K8s admission controllers.
- **Remediation**: Change `accessModes` to `ReadWriteOnce` for local volume PV/PVC, or use hostPath volume mounts.

---

### Area 5: `monitoring/` (Prometheus Rules & KEDA Autoscaling)

#### Finding D-18 (High): Non-Existent `dynamo_namespace` Label in KEDA TTFT Query
- **Location**: `monitoring/keda-scaledobjects.yaml.j2`, line 44
- **Problem**: The TTFT autoscaling trigger query specifies:
  ```promql
  histogram_quantile(0.95, sum(rate(dynamo_frontend_time_to_first_token_seconds_bucket{dynamo_namespace="{{ namespace }}-{{ deployment_name }}"}[2m])) by (le))
  ```
  Neither the workload manifests nor the frontend components emit a metric label named `dynamo_namespace` with value `{{ namespace }}-{{ deployment_name }}`.
- **Impact**: The Prometheus query returns no data (NaN), preventing KEDA from triggering scale-out actions based on TTFT latency spikes.
- **Remediation**: Use standard Kubernetes labels: `{namespace="{{ namespace }}", app_kubernetes_io_name="{{ deployment_name }}"}`.

#### Finding D-19 (Medium): Missing `model_name` Label in DCGM GPU Utilization Metric
- **Location**: `monitoring/prometheus-rules.yaml`, lines 18–19
- **Problem**: Recording rule specifies:
  ```yaml
  - record: dcgm:gpu_utilization_avg
    expr: avg by (gpu, model_name) (DCGM_FI_DEV_GPU_UTIL)
  ```
- **Impact**: DCGM metrics exported by `dcgm-exporter` only contain hardware device attributes (`gpu`, `UUID`, `device`), not `model_name`. Grouping by `model_name` drops the label or yields nothing, breaking downstream alerts and dashboards.
- **Remediation**: Perform a vector match with kube-state-metrics pod labels:
  `avg by (gpu, model_name) (DCGM_FI_DEV_GPU_UTIL * on(pod, namespace) group_left(model_name) ...)` or group by `(gpu, pod, namespace)`.

#### Finding D-20 (Medium): PromQL Metric Name Mismatch with Native vLLM Exporter
- **Location**: `monitoring/prometheus-rules.yaml`, lines 14–17
- **Problem**: Recording rule uses `expr: sum by (model_name, namespace) (vllm_num_requests_waiting)`.
- **Impact**: Native vLLM exports `vllm:num_requests_waiting` (colon delimited), while the rule queries `vllm_num_requests_waiting` (underscore delimited). The rule produces empty series unless an unconfigured relabeling pipeline rewrites colons.
- **Remediation**: Update PromQL expression to query `vllm:num_requests_waiting or vllm_num_requests_waiting`.

---

## 5. Defect Summary Table

| Finding ID | Severity | Category | File | Line(s) | Summary |
|------------|----------|----------|------|---------|---------|
| **D-01** | High | Contract / Template | `dynamo/templates/dgd-components.yaml.j2` | 112, 120, 125 | Jinja2 crashes with `UndefinedError` when optional `kv_cache_transfer` is omitted |
| **D-02** | High | Security / Contract | `control-plane/api/main.py` | 200 | `/api/v1/models/{model_name}/status` lacks authentication dependency |
| **D-03** | Medium | Contract / OpenAPI | `contracts/control_plane_openapi.yaml` | 1–241 | Multiple active K8s control, usage metric, and data endpoints omitted from OpenAPI spec |
| **D-04** | Medium | Contract / Schema | `contracts/model_spec.schema.json` | 36–45 | `max_model_len` and `gpu_memory_utilization` validation constraints diverge from OpenAPI and code |
| **D-05** | High | Script / Bug | `scripts/bootstrap_inference_cluster.sh` | 83, 99, 108, 117 | Relative file paths cause silent failure to deploy infra manifests when run from any other directory |
| **D-06** | High | Script / Error Trap | `scripts/bootstrap_inference_cluster.sh` | 84, 103, 110, 121 | Universal `\|\| true` swallows critical bootstrap failures, masking broken cluster state |
| **D-07** | High | Security / Infra | `scripts/bootstrap_inference_cluster.sh` | 52 | World-writable `chmod 0777` on `/mnt/nvme/model-cache` allows arbitrary model file tampering |
| **D-08** | Medium | Script / Env | `scripts/bootstrap_inference_cluster.sh` | 66–70 | `KUBECONFIG` is not exported, breaking subsequent `kubectl` calls in subshells or sudo environments |
| **D-09** | High | Loader / Functional | `loader/model_warmer.py` | 33, 44–55 | Missing `--safe` argument in `argparse` triggers immediate crash when `--safe` flag is passed |
| **D-10** | High | Loader / Cache | `loader/model_warmer.py` | 30 | Warmer writes to flat directory rather than HF Hub cache layout, causing vLLM to re-download weights |
| **D-11** | High | Loader / Concurrency | `loader/model_warmer.py` | 22–35 | Lack of atomic staging and file locks allows race conditions and corrupted cache on interruptions |
| **D-12** | Low | Loader / Security | `loader/Dockerfile` | 1–18 | Unpinned pip dependencies and container execution as `root` without security context |
| **D-13** | High | Infra / RBAC | `infra/control-plane-deployment.yaml` | 22–26 | Missing `serviceAccountName: control-plane` causes pod to run unprivileged and fail K8s calls |
| **D-14** | High | Infra / RBAC | `infra/control-plane-rbac.yaml` | 34–36 | Missing `jobs/status` subresource verb blocks reconciler's `read_namespaced_job_status` with 403 |
| **D-15** | High | Infra / RBAC | `infra/cloudflare/external-dns.yaml` | 1–39 | ExternalDNS deployed without ServiceAccount or ClusterRole, causing 403 on listing Ingress/Services |
| **D-16** | High | Infra / Network | `infra/cloudflare/ingress-rules.yaml` | 4–5, 22–25, 32–35 | Ingress in `ingress-nginx` cannot route cross-namespace to services in `ai-gateway` and `dynamo-control-plane` |
| **D-17** | Medium | Infra / Storage | `infra/storage-nvme/nvme-storageclass.yaml` | 19–20, 38–39 | `ReadWriteMany` is invalid for local PersistentVolumes, causing volume binding failure |
| **D-18** | High | Monitoring / KEDA | `monitoring/keda-scaledobjects.yaml.j2` | 44 | PromQL query queries non-existent `dynamo_namespace` label, disabling TTFT autoscaling |
| **D-19** | Medium | Monitoring / PromQL | `monitoring/prometheus-rules.yaml` | 18–19 | `DCGM_FI_DEV_GPU_UTIL` lacks `model_name` label; grouping by `model_name` breaks recorded metric |
| **D-20** | Medium | Monitoring / PromQL | `monitoring/prometheus-rules.yaml` | 14–17 | PromQL queries `vllm_num_requests_waiting` instead of native exported `vllm:num_requests_waiting` |

---

## 6. Reproduction Scenarios & Proofs of Concept

### PoC 1: Reproducing D-01 (Jinja2 Crash on Missing `kv_cache_transfer`)
```python
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader(["dynamo/templates"]))
tmpl = env.get_template("dgd-aggregated.yaml.j2")
plan = {
    "deployment_name": "test-model",
    "namespace": "dynamo-workloads",
    "model_id": "meta-llama/Llama-3-8b",
    "dynamo_image": "vllm:latest",
    "dtype": "auto",
    "max_model_len": 2048,
    "gpu_memory_utilization": 0.9,
    "worker_components": [("worker", {"replicas": 1, "tensor_parallel_size": 1, "gpu_sku": "L40S"})],
    # kv_cache_transfer omitted
}
# Output: jinja2.exceptions.UndefinedError: 'kv_cache_transfer' is undefined
tmpl.render(plan)
```

### PoC 2: Reproducing D-09 (CLI Crash on `--safe` in `model_warmer.py`)
```bash
.venv/bin/python loader/model_warmer.py --model-id "meta-llama/Llama-3.3-70B-Instruct" --safe
# Output:
# usage: model_warmer.py [-h] [--model-id MODEL_ID] [--cache-dir CACHE_DIR]
#                        [--token TOKEN] [--revision REVISION]
# model_warmer.py: error: unrecognized arguments: --safe
```

### PoC 3: Reproducing D-02 (Unauthenticated `/api/v1/models/{model_name}/status`)
```python
from starlette.testclient import TestClient
from api import main

with TestClient(main.app) as client:
    # No Authorization header provided
    resp = client.get("/api/v1/models/test-deployment/status")
    # Expected: 401 Unauthorized
    # Actual: 404 Not Found (Bypassed auth, entered business logic)
    assert resp.status_code == 404
```

---

## 7. Concrete Remediation Specifications

### Remediation Diff for D-01 (`dynamo/templates/dgd-components.yaml.j2`)
```diff
--- a/dynamo/templates/dgd-components.yaml.j2
+++ b/dynamo/templates/dgd-components.yaml.j2
@@ -109,7 +109,7 @@
                       name: hf-token-secret
                       key: token
                       optional: true
-{% if kv_cache_transfer.protocol == 'rdma' %}
+{% if kv_cache_transfer and kv_cache_transfer.protocol == 'rdma' %}
               securityContext:
                 capabilities:
                   add: [IPC_LOCK]
@@ -117,12 +117,12 @@
               resources:
                 requests:
                   nvidia.com/gpu: {{ clique.tensor_parallel_size * (clique.pipeline_parallel_size | default(1)) }}
-{% if kv_cache_transfer.protocol == 'rdma' %}
+{% if kv_cache_transfer and kv_cache_transfer.protocol == 'rdma' %}
                   rdma/rdma_shared_device: 1
 {% endif %}
                 limits:
                   nvidia.com/gpu: {{ clique.tensor_parallel_size * (clique.pipeline_parallel_size | default(1)) }}
-{% if kv_cache_transfer.protocol == 'rdma' %}
+{% if kv_cache_transfer and kv_cache_transfer.protocol == 'rdma' %}
                   rdma/rdma_shared_device: 1
 {% endif %}
```

### Remediation Diff for D-02 (`control-plane/api/main.py`)
```diff
--- a/control-plane/api/main.py
+++ b/control-plane/api/main.py
@@ -197,7 +197,7 @@ async def list_models():
     return await reconciler.list_deployments()
 
 
-@app.get("/api/v1/models/{model_name}/status", tags=["Deployments"])
+@app.get("/api/v1/models/{model_name}/status", tags=["Deployments"], dependencies=[Depends(verify_auth)])
 async def get_model_status(model_name: str):
     info = await reconciler.get_deployment_status(model_name)
     if not info:
```

### Remediation Diff for D-09 (`loader/model_warmer.py`)
```diff
--- a/loader/model_warmer.py
+++ b/loader/model_warmer.py
@@ -52,6 +52,7 @@ def main():
     parser.add_argument("--token", type=str, default=os.environ.get("HF_TOKEN"), help="Hugging Face API token")
     parser.add_argument("--revision", type=str, default=None, help="Immutable Hugging Face commit")
+    parser.add_argument("--safe", action="store_true", help="Download only safetensors weights")
 
     args = parser.parse_args()
```

### Remediation Diff for D-13 (`infra/control-plane-deployment.yaml`)
```diff
--- a/infra/control-plane-deployment.yaml
+++ b/infra/control-plane-deployment.yaml
@@ -22,6 +22,7 @@ spec:
     spec:
+      serviceAccountName: control-plane
       containers:
         - name: control-plane
```

### Remediation Diff for D-14 (`infra/control-plane-rbac.yaml`)
```diff
--- a/infra/control-plane-rbac.yaml
+++ b/infra/control-plane-rbac.yaml
@@ -33,7 +33,7 @@ rules:
     verbs: [get, list, create, patch, delete]
   - apiGroups: [batch]
-    resources: [jobs]
+    resources: [jobs, jobs/status]
     verbs: [get, create, delete]
   - apiGroups: [discovery.k8s.io]
```
