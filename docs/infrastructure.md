# Infrastructure & Bootstrap

Script: `scripts/bootstrap_inference_cluster.sh` (run as root; `shellcheck`-clean).
This provisions a **single** bare-metal/K3s node; multi-cluster app rollout is
separate (see [Multi-Cluster](multi-cluster.md)).

## What the script does

1. **System**: loads `nvme mlx5_core mlx5_ib ib_uverbs overlay br_netfilter`,
   writes `/etc/sysctl.d/99-ai-inference.conf`, creates `/mnt/nvme/model-cache`
   (`MODEL_CACHE_DIR` override).
2. **K3s** (`K3S_VERSION`, default `v1.31.1+k3s1`): installs with Traefik +
   ServiceLB disabled, `flannel-backend=host-gw`; copies kubeconfig to root.
3. **Storage**: applies `infra/storage-nvme/nvme-storageclass.yaml`.
4. **GPU + network**: `helm install gpu-operator` (`infra/gpu-operator/…`),
   applies `infra/network-rdma/rdma-network-operator.yaml`.
5. **Ingress + edge**: Helm-installs Ingress-NGINX as **ClusterIP only**
   (`infra/cloudflare/ingress-nginx-values.yaml`: `proxy-read-timeout: 3600`,
   `proxy-buffering: off`), applies `cloudflared-deployment.yaml` (2 replicas),
   `external-dns.yaml`, and `cloudflare-secret.yaml` if present.
6. **Gateway (optional)**: if `LITELLM_IMAGE` is set, runs
   `python -m scripts.install_litellm` with `LITELLM_KUBE_CONTEXT`/`PYTHON_BIN`.

```bash
sudo ./scripts/bootstrap_inference_cluster.sh
LITELLM_IMAGE='ghcr.io/org/litellm@sha256:<digest>' sudo -E ./scripts/bootstrap_inference_cluster.sh
```

## Key manifests

| Path | Purpose |
|---|---|
| `infra/gpu-operator/gpu-operator-values.yaml` | DCGM + GPUDirect RDMA settings |
| `infra/network-rdma/rdma-network-operator.yaml` | NIC policy for RoCE/InfiniBand |
| `infra/storage-nvme/` | Local StorageClass + PVC for model cache |
| `infra/cloudflare/` | Ingress rules, NGINX values, cloudflared, ExternalDNS, secret example |
| `infra/control-plane-{rbac,deployment}.yaml` | ServiceAccount/ClusterRole/Binding + Deployment/Service |
| `dynamo/helm-values/dynamo-platform-values.yaml` | Dynamo Platform, Grove, KAI scheduler |

Cloudflare notes: no inbound ports — egress-only `cloudflared`; ExternalDNS
manages `<tunnel-id>.cfargotunnel.com` CNAMEs; Cloudflare idle timeout (~100 s)
means long prefill/SSE streams need keep-alives/chunked responses (see audit
`docs.md`). Validate RDMA on hosts with `ibv_devices`, `ibv_devinfo`
(MTU 4096, 4X), `nvidia-smi topo -m`.
