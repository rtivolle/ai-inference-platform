# Deploy to independent Kubernetes clusters

The release pipeline deploys a separate control-plane API and LiteLLM gateway in
each cluster. It builds and publishes the control-plane and model-warmer images
once to GHCR, then deploys the same image digests to every selected target. Model
deployments, databases, GPU scheduling, and gateway registration remain local to
each cluster. There is no global router or shared model state.

## Prepare each cluster

This pipeline deploys applications to existing clusters. Provision K3s/Kubernetes,
the NVIDIA GPU Operator, a compatible Dynamo operator and its
`dynamographdeployments.nvidia.com` CRD, and any RDMA/ingress/Cloudflare components
separately. Prepare `/mnt/nvme/model-cache` on each GPU node. The existing
single-node bootstrap script is not a multi-cluster provisioner. Its local NVMe
PVC is not shared storage across machines; application templates currently use
node-local host paths.

Create the namespaces `dynamo-control-plane`, `dynamo-workloads`, and `ai-gateway`.
In each namespace, provision a `registry-credentials` image-pull Secret containing
read access to the published GHCR packages. Provision these runtime Secrets
through your secret manager or existing cluster administration process:

| Namespace | Secret | Required keys |
| --- | --- | --- |
| `dynamo-control-plane` | `inference-runtime` | `litellm-master-key`, `api-token` |
| `ai-gateway` | `inference-runtime` | `litellm-master-key`, `litellm-salt-key`, `database-url` |
| `dynamo-workloads` | `hf-token-secret` (optional for public models) | `token` |

Use a stable `litellm-salt-key` for encryption. For an existing gateway that
previously used no explicit salt, set it to its existing master key to preserve
encrypted credentials. The [LiteLLM installer](../gateway/README.md) can provision
these Secrets automatically with an external database.

The two master keys must match **within a cluster**. Use independent keys and
databases for different clusters. `database-url` must point to a persistent,
reachable PostgreSQL database managed separately from this release. The pipeline
deliberately excludes the example ephemeral Postgres Deployment. Switching an
existing installation to this database requires migrating its data first.

The deploy identity needs access to the application namespaces, runtime Secrets,
CRD discovery, Deployments, Services, ConfigMaps, ServiceAccounts, Roles,
RoleBindings, and the included inventory ClusterRole/ClusterRoleBinding. Assigning
these RBAC objects also requires permission to grant their rules. The application
service account only manages Dynamo resources and warmer Jobs in
`dynamo-workloads`, reads EndpointSlices there, and lists cluster nodes and pods
to compute GPU inventory. Neither API contracts nor Dynamo CRD templates change.

## Configure GitHub Actions

1. Copy `deploy/clusters.example.json` to `deploy/clusters.json`, replace the
   example names, kubeconfig contexts, and environment names, and commit it.
   Inventory contains no credentials. Each context must select its intended
   independent cluster; duplicate context names are rejected.
2. Create a GitHub Environment matching every inventory `environment`. Add its
   cluster-specific **raw kubeconfig YAML** as the `KUBECONFIG` environment Secret.
   It must contain the configured context. Use a bounded deploy identity; keep
   kubeconfigs out of Git and artifacts. Configure environment branch restrictions
   and approval rules according to your deployment policy.
3. Set `KUBECTL_VERSION` in each environment to an explicit kubectl version
   compatible with that cluster. Optionally set the repository variable
   `DEPLOY_RUNNER` to a self-hosted runner label with access to private Kubernetes
   API endpoints. The default is `ubuntu-latest`.
4. Enable GHCR package publishing for the repository's `GITHUB_TOKEN`. The cluster
   pull Secret needs separate read access; the workflow token is not copied into
   clusters.
5. Run **Deploy Independent Clusters** from Actions on the desired source revision.
   Select the committed inventory and a cluster name or `all`; supply a tested
   LiteLLM image as `repository@sha256:<digest>`.

Validation runs Python 3.11 tests, Ruff, Mypy, ShellCheck, and JSON Schema checks
before publishing. Deployment jobs use environment-scoped credentials, execute
one at a time, and serialize overlapping deployments to the same cluster. A
failure in one matrix target does not cancel other target jobs.

Each target checks namespaces, pull/runtime Secrets, matching master keys, and
the Dynamo CRD, then performs a server-side dry run. It applies the gateway and
waits for readiness before applying the control plane. Each rollout has a ten
minute timeout. Release YAML and image references are saved as a GitHub artifact
per target, including on failure. Credentials are removed after deployment.

## Local rendering and deployment

Install Python 3.11 and PyYAML. Set `CONTROL_IMAGE`, `LOADER_IMAGE`, and
`GATEWAY_IMAGE` to registry image references with SHA-256 digests, then render:

```bash
python scripts/deploy_clusters.py \
  --inventory deploy/clusters.json --cluster all \
  --control-image "$CONTROL_IMAGE" --loader-image "$LOADER_IMAGE" \
  --gateway-image "$GATEWAY_IMAGE"
```

This writes `release/release.yaml` and `release/release.json` without accessing
clusters. Review the YAML, then repeat the command with `--apply` to deploy using
your local kubeconfig. Every kubectl command specifies the inventory context;
the script never changes your current context. Local `all` checks every cluster
before any writes, deploys sequentially, and stops at the first rollout failure.

## Recovery and limits

No automatic rollback runs: a failing cluster may be partially updated, and
successful clusters retain their releases. Inspect rollout events and the saved
artifact, then fix forward or rerun the local command for that cluster with the
previous release's **three digests** from its `release.json`, using a checkout of
that release's source revision (recorded for Actions releases). This restores the
application manifest and the loader image together. Ensure database schema and
configuration are compatible before rolling back; databases, Secrets, operators,
and deployed models are not reverted. Keep previous image digests available.

Rollout verification uses application readiness probes. It does not prove GPU
inference, operator compatibility, model-cache completeness on all GPU nodes, or
external tunnel reachability. Verify those separately in each cluster before
routing traffic. The release currently targets the existing fixed namespaces and
one platform installation per cluster.

References: [GitHub deployment environments](https://docs.github.com/en/actions/concepts/workflows-and-actions/deployment-environments),
[kubectl rollout status](https://kubernetes.io/docs/reference/kubectl/generated/kubectl/kubectl_rollout/kubectl_rollout_status/).
