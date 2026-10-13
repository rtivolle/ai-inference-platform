#!/usr/bin/env bash
# ==============================================================================
# Script: bootstrap_inference_cluster.sh
# Agent: Agent 1 (Infra, K8s & Cloudflare Network)
# Purpose: Bare-metal initialization for high-performance AI inference cluster
#          Sets up NVMe storage, K3s, NVIDIA GPU Operator, RDMA Network Operator,
#          Ingress-NGINX (ClusterIP only), and Cloudflare Tunnel.
# ==============================================================================

set -euo pipefail

# Environment defaults
CACHE_DIR="${MODEL_CACHE_DIR:-/mnt/nvme/model-cache}"
K3S_VERSION="${K3S_VERSION:-v1.31.1+k3s1}"
CLOUDFLARE_NAMESPACE="cloudflare"
INGRESS_NAMESPACE="ingress-nginx"
GPU_NAMESPACE="gpu-operator"
NETWORK_NAMESPACE="network-operator"
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

log() {
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] [BOOTSTRAP] $*"
}

check_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "Error: This script must be executed as root (or with sudo)." >&2
        exit 1
    fi
}

setup_system_prerequisites() {
    log "Configuring system prerequisites, kernel modules, and HugePages..."
    
    # Kernel modules for NVLink, RDMA and container networking
    modprobe -a nvme mlx5_core mlx5_ib ib_uverbs overlay br_netfilter || true

    # Sysctl performance tuning
    cat << 'EOF' > /etc/sysctl.d/99-ai-inference.conf
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
fs.file-max = 2097152
vm.max_map_count = 1048576
vm.overcommit_memory = 1
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
EOF
    sysctl --system > /dev/null 2>&1 || true

    # Prepare model cache storage
    mkdir -p "${CACHE_DIR}"
    chmod 0777 "${CACHE_DIR}"
    log "NVMe model cache directory initialized at ${CACHE_DIR}"
}

install_k3s() {
    log "Installing K3s (version ${K3S_VERSION}) with Traefik disabled..."
    if command -v k3s >/dev/null 2>&1; then
        log "K3s is already installed. Skipping binary download."
    else
        curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="${K3S_VERSION}" \
            INSTALL_K3S_EXEC="--disable traefik --disable servicelb --flannel-backend=host-gw" sh -
    fi

    # Wait for node readiness
    mkdir -p /root/.kube
    if [[ -f /etc/rancher/k3s/k3s.yaml ]]; then
        cp /etc/rancher/k3s/k3s.yaml /root/.kube/config
        chmod 600 /root/.kube/config
    fi
    log "K3s node is active and ready."
}

install_helm_if_missing() {
    if ! command -v helm >/dev/null 2>&1; then
        log "Helm not found. Installing Helm..."
        curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
    fi
}

deploy_storage_and_crds() {
    log "Applying NVMe local StorageClass and directory structure..."
    if [[ -f "infra/storage-nvme/nvme-storageclass.yaml" ]]; then
        kubectl apply -f infra/storage-nvme/nvme-storageclass.yaml || true
    fi
}

deploy_gpu_and_network_operators() {
    log "Deploying NVIDIA GPU Operator and RDMA Network Operator..."
    install_helm_if_missing

    # Helm repo setup
    helm repo add nvidia https://helm.ngc.nvidia.com/nvidia || true
    helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx || true
    helm repo update > /dev/null 2>&1 || true

    # GPU Operator
    kubectl create namespace "${GPU_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
    if [[ -f "infra/gpu-operator/gpu-operator-values.yaml" ]]; then
        helm upgrade --install gpu-operator nvidia/gpu-operator \
            -n "${GPU_NAMESPACE}" \
            -f infra/gpu-operator/gpu-operator-values.yaml \
            --wait=false || true
    fi

    # RDMA Network Operator
    kubectl create namespace "${NETWORK_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
    if [[ -f "infra/network-rdma/rdma-network-operator.yaml" ]]; then
        kubectl apply -f infra/network-rdma/rdma-network-operator.yaml || true
    fi
}

deploy_ingress_and_cloudflare() {
    log "Deploying Ingress-NGINX (ClusterIP only, no public host ports)..."
    kubectl create namespace "${INGRESS_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

    if [[ -f "infra/cloudflare/ingress-nginx-values.yaml" ]]; then
        helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
            -n "${INGRESS_NAMESPACE}" \
            -f infra/cloudflare/ingress-nginx-values.yaml \
            --wait=false || true
    fi

    log "Deploying Cloudflare Tunnel (cloudflared in 2 replicas) and ExternalDNS..."
    kubectl create namespace "${CLOUDFLARE_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

    if [[ -f "infra/cloudflare/cloudflare-secret.yaml" ]]; then
        kubectl apply -f infra/cloudflare/cloudflare-secret.yaml || true
    fi
    if [[ -f "infra/cloudflare/cloudflared-deployment.yaml" ]]; then
        kubectl apply -f infra/cloudflare/cloudflared-deployment.yaml || true
    fi
    if [[ -f "infra/cloudflare/external-dns.yaml" ]]; then
        kubectl apply -f infra/cloudflare/external-dns.yaml || true
    fi
}

main() {
    log "=== Starting AI Inference Cluster Bootstrap ==="
    check_root
    setup_system_prerequisites
    install_k3s
    deploy_storage_and_crds
    deploy_gpu_and_network_operators
    deploy_ingress_and_cloudflare
    if [[ -n "${LITELLM_IMAGE:-}" ]]; then
        log "Installing LiteLLM with persistent model configuration..."
        (cd "${REPO_ROOT}" && "${PYTHON_BIN:-python3}" -m scripts.install_litellm \
            --context "${LITELLM_KUBE_CONTEXT:-$(kubectl config current-context)}" \
            --image "${LITELLM_IMAGE}")
    else
        log "Set LITELLM_IMAGE to a tested image digest to install LiteLLM during bootstrap."
    fi
    log "=== AI Inference Cluster Bootstrap Completed Successfully ==="
}

main "$@"
