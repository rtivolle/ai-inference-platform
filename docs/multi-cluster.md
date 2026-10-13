# Multi-Cluster Releases

Source: `deploy/README.md`, `scripts/deploy_clusters.py`, `deploy/clusters.example.json`,
`.github/workflows/deploy.yaml`.

## Model

One immutable release (control-plane + warmer + gateway images by digest) is
built once and deployed **per independent cluster**. No global router or shared
model/DB state — models, databases, GPU scheduling, and LiteLLM registration
stay local to each cluster.

## Prerequisites per cluster

- Existing Kubernetes + GPU Operator + Dynamo operator
  (`dynamographdeployments.nvidia.com` CRD) + RDMA/ingress/Cloudflare pieces;
  `/mnt/nvme/model-cache` on every GPU node.
- Namespaces `dynamo-control-plane`, `dynamo-workloads`, `ai-gateway` +
  `registry-credentials` pull Secret in each.
- Runtime Secrets (via secret manager, never git):

| Namespace | Secret | Keys |
|---|---|---|
| `dynamo-control-plane` | `inference-runtime` | `litellm-master-key`, `api-token` |
| `ai-gateway` | `inference-runtime` | `litellm-master-key`, `litellm-salt-key`, `database-url` |
| `dynamo-workloads` | `hf-token-secret` | `token` (optional for public models) |

Master keys must match **within** a cluster, differ **across** clusters;
`database-url` is a separately managed persistent Postgres (installer or
external). Ephemeral example Postgres is excluded from releases.

## Configure and run (GitHub Actions)

1. Copy `deploy/clusters.example.json` → `deploy/clusters.json` (names, contexts,
   environments; no credentials), commit it.
2. Per environment: raw kubeconfig YAML as `KUBECONFIG` secret, `KUBECTL_VERSION`,
   optional `DEPLOY_RUNNER`; bounded deploy identity with rights on app
   namespaces, Secrets, CRDs, Deployments, Services, ConfigMaps, RBAC.
3. Dispatch **Deploy Independent Clusters** with inventory path, cluster (`all`
   or name), and tested `repository@sha256:<digest>` gateway image.

Each target: preflight (namespaces, Secrets, key match, Dynamo CRD) →
server-side dry run → apply gateway → wait readiness → apply control plane
(10 min per rollout). Jobs are serialized per cluster; one failure doesn't cancel
others. Release YAML/JSON saved as artifacts (credentials stripped).

## Local render/deploy

```bash
python scripts/deploy_clusters.py --inventory deploy/clusters.json --cluster all \
  --control-image "$CONTROL_IMAGE" --loader-image "$LOADER_IMAGE" \
  --gateway-image "$GATEWAY_IMAGE"   # all digests: repository@sha256:<hex>
# review release/release.yaml + release.json, then add --apply
```

Every kubectl call uses the inventory context; current-context is untouched.

## Recovery

No auto-rollback: rerun the local command (or workflow) with the previous
release's **three digests** from its `release.json` at that revision. Databases,
Secrets, operators, and deployed models are not reverted — check schema/config
compatibility first. Readiness probes don't prove GPU inference, cache
completeness, or tunnel reachability; verify those per cluster.
Tests: `tests/test_cluster_deployment.py`.
