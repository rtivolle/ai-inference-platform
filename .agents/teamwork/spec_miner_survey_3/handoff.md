# Handoff Report — Spec Miner Survey 3

## 1. Observation
1. **Jinja2 Template Missing Field Handling**:
   - In `dynamo/templates/dgd-components.yaml.j2:112, 120, 125`:
     ```jinja2
     {% if kv_cache_transfer.protocol == 'rdma' %}
     ```
   - Running `.venv/bin/python` to render `dgd-aggregated.yaml.j2` without `kv_cache_transfer` in the plan produces:
     ```
     jinja2.exceptions.UndefinedError: 'kv_cache_transfer' is undefined
     ```
   - In `contracts/model_spec.schema.json:125-133`, `kv_cache_transfer` is NOT included in the `required` properties list for `DeploymentPlan`.

2. **Unauthenticated Route in Control Plane API**:
   - In `control-plane/api/main.py:200`:
     ```python
     @app.get("/api/v1/models/{model_name}/status", tags=["Deployments"])
     ```
     Notice that `dependencies=[Depends(verify_auth)]` is missing, whereas lines 105, 111, 118, 123, 129, 134, 139, 152, 180, and 195 all include `dependencies=[Depends(verify_auth)]`.
   - In `contracts/control_plane_openapi.yaml:149-155`:
     ```yaml
     /api/v1/models/{model_name}/status:
       get:
         security:
           - BearerAuth: []
           - CloudflareAccess: []
     ```

3. **Loader `--safe` CLI Argument Crash**:
   - In `loader/model_warmer.py:33`:
     ```python
     ignore_patterns=["*.pt", "*.bin", "*.msgpack"] if "--safe" in sys.argv else None
     ```
   - Running `.venv/bin/python loader/model_warmer.py --model-id dummy --safe` yields:
     ```
     usage: model_warmer.py [-h] [--model-id MODEL_ID] [--cache-dir CACHE_DIR]
                            [--token TOKEN] [--revision REVISION]
     model_warmer.py: error: unrecognized arguments: --safe
     ```

4. **Bootstrap Script Working Directory Dependence and Error Suppression**:
   - In `scripts/bootstrap_inference_cluster.sh:19`, `REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"` is declared, but lines 83, 99, 108, 117, 127, 130, 133 use relative paths without `$REPO_ROOT` (e.g. `[[ -f "infra/storage-nvme/nvme-storageclass.yaml" ]]`).
   - Lines 84, 103, 110, 121, 128, 131, 134 append `|| true` to critical commands, masking failures.
   - Line 52 sets `chmod 0777 "${CACHE_DIR}"`.

5. **Infra RBAC and Ingress Defects**:
   - In `infra/control-plane-deployment.yaml:22-26`, `serviceAccountName: control-plane` is missing from `spec.template.spec`.
   - In `infra/control-plane-rbac.yaml:34-36`, the Role lists `resources: [jobs]`, but `control-plane/api/k8s_client.py:282` calls `batch_api.read_namespaced_job_status(name=job_name, namespace=namespace)`.
   - In `infra/cloudflare/external-dns.yaml:1-39`, ExternalDNS runs in namespace `cloudflare` without any ServiceAccount, ClusterRole, or ClusterRoleBinding.
   - In `infra/cloudflare/ingress-rules.yaml:4-5, 22-25, 32-35`, `ai-platform-ingress` is in namespace `ingress-nginx`, but targets `backend.service.name: litellm-service` (in `ai-gateway`) and `control-plane-service` (in `dynamo-control-plane`).
   - In `infra/storage-nvme/nvme-storageclass.yaml:19-20, 38-39`, the local PersistentVolume specifies `accessModes: [ReadWriteMany]`.

6. **Monitoring Trigger and Label Incompatibilities**:
   - In `monitoring/keda-scaledobjects.yaml.j2:44`, PromQL query uses `dynamo_namespace="{{ namespace }}-{{ deployment_name }}"`, which does not exist on scraped telemetry.
   - In `monitoring/prometheus-rules.yaml:18-19`, `record: dcgm:gpu_utilization_avg` groups by `(gpu, model_name)`, but raw `DCGM_FI_DEV_GPU_UTIL` does not contain `model_name`.

---

## 2. Logic Chain
1. *Template crash on optional plan attributes*:
   - Observation 1 establishes that `contracts/model_spec.schema.json` permits `DeploymentPlan` without `kv_cache_transfer`.
   - Direct execution of `tmpl.render(plan)` without `kv_cache_transfer` raises `jinja2.exceptions.UndefinedError`.
   - Therefore, any API caller submitting a minimal plan or plan without RDMA/network transfer causes the reconciler to crash.
2. *Security gap on status endpoint*:
   - Observation 2 establishes that `main.py` omitted `dependencies=[Depends(verify_auth)]` on `/api/v1/models/{model_name}/status`.
   - The authoritative OpenAPI contract specifies `BearerAuth` and `CloudflareAccess` security schemes on this endpoint.
   - Therefore, the endpoint is exposed publicly without authorization, leaking internal workload metadata.
3. *Warmer CLI failure*:
   - Observation 3 establishes that `model_warmer.py` attempts to inspect `sys.argv` for `--safe`, but `argparse.ArgumentParser` rejects any unregistered option.
   - Therefore, calling the warmer with `--safe` always fails before executing any download logic.
4. *Bootstrap script failure outside root*:
   - Observation 4 shows that all manifest paths inside `bootstrap_inference_cluster.sh` are relative (`infra/...`) rather than prefixed with `$REPO_ROOT/`.
   - If executed from `/root`, `/tmp`, or a service runner, every file test returns false and skips deployment.
   - The use of `|| true` causes the script to report success even when all steps fail.
5. *RBAC and cross-namespace ingress failures*:
   - Observation 5 confirms that without `jobs/status`, the Kubernetes API server denies `read_namespaced_job_status` requests.
   - Without ServiceAccount and ClusterRole, ExternalDNS cannot list cluster ingresses.
   - Without `serviceAccountName: control-plane` in `control-plane-deployment.yaml`, the control plane runs as the unprivileged `default` service account.
   - Ingress-NGINX rejects cross-namespace service routing by default, resulting in 503 Service Unavailable for all ingress traffic.
6. *Autoscaling and metric evaluation failure*:
   - Observation 6 demonstrates that Prometheus queries in KEDA trigger specifications filter on labels (`dynamo_namespace`) not emitted by the workloads.
   - Therefore, KEDA trigger evaluation evaluates to no data and fails to scale pods.

---

## 3. Caveats
- Production behavior of NVIDIA GPU Operator and Mellanox RDMA Operator was inspected via static manifest analysis and configuration schemas; bare-metal hardware drivers (NVLink, InfiniBand NICs) were not exercised on this Linux node.
- Upstream `kubeconform` validation against remote URLs in `scripts/validate_manifests.py` requires outbound internet access to raw GitHub usercontent.

---

## 4. Conclusion
The audit discovered 20 concrete defects across `contracts/`, `scripts/`, `loader/`, `infra/`, and `monitoring/`. The most critical issues are:
1. Jinja2 template crash on plans omitting `kv_cache_transfer` (`dgd-components.yaml.j2`).
2. Authentication bypass on `/api/v1/models/{model_name}/status` (`main.py`).
3. CLI crash in `loader/model_warmer.py` when passed `--safe`.
4. Silent skipping of all infra manifests in `bootstrap_inference_cluster.sh` when run outside the repo directory.
5. Missing RBAC for `jobs/status` and ExternalDNS, and invalid cross-namespace ingress rules.
6. Broken KEDA PromQL queries targeting non-existent metric labels.

All findings have been fully documented with exact line numbers, reproduction proofs of concept, and suggested diffs in `.agents/teamwork/spec_miner_survey_3/report.md`.

---

## 5. Verification Method
1. **Run full existing test suite**:
   ```bash
   .venv/bin/pytest -v tests/
   ```
   (Must maintain passing status across all existing unit and lifecycle tests).

2. **Verify `--safe` argument failure**:
   ```bash
   .venv/bin/python loader/model_warmer.py --model-id "example/model" --safe
   ```

3. **Verify template rendering without `kv_cache_transfer`**:
   ```bash
   .venv/bin/python -c "
   from jinja2 import Environment, FileSystemLoader
   env = Environment(loader=FileSystemLoader(['dynamo/templates']))
   tmpl = env.get_template('dgd-aggregated.yaml.j2')
   tmpl.render({'deployment_name': 'm', 'namespace': 'ns', 'model_id': 'm', 'dynamo_image': 'img', 'dtype': 'auto', 'max_model_len': 2048, 'gpu_memory_utilization': 0.9, 'worker_components': [('worker', {'replicas': 1, 'tensor_parallel_size': 1, 'gpu_sku': 'L40S'})]})
   "
   ```

4. **Verify unauthenticated status endpoint**:
   Inspect route definition in `control-plane/api/main.py:200` vs line 195.

5. **Verify ShellCheck on scripts**:
   ```bash
   shellcheck scripts/*.sh
   ```
