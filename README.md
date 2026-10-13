# Plateforme d'Inférence Kubernetes + NVIDIA Dynamo + LiteLLM + Cloudflare

Plateforme d'inférence distribuée multi-modèles à haute performance, conçue pour orchestrer des LLMs en architecture désagrégée (Prefill & Decode) avec NVIDIA Dynamo, synchronisation à chaud dans LiteLLM Proxy, et exposition sécurisée sans port entrant via Cloudflare Tunnel.

---

## 1. Architecture Globale et DAG d'Exécution

```mermaid
flowchart TD
    CF[Cloudflare Tunnel\ncloudflared (2 réplicas)] --> Ingress[Ingress-NGINX\nClusterIP + SSE streaming]
    Ingress --> CP[Control Plane API\nFastAPI :8000]
    Ingress --> GW[LiteLLM Proxy\n:4000]

    CP --> AT[Auto-Tuner & VRAM Engine\nContrat 1 : Sizing mathématique]
    CP --> Warmer[Job NVMe Model Warmer\nhf_transfer -> /mnt/nvme]
    CP --> Reconciler[Dynamo Reconciler\nContrat 2 : Jinja2 DGD]

    Reconciler --> CRD[DynamoGraphDeployment\nOperator + Grove + KAI]
    CRD --> PW[PrefillWorker Clique\nTP=4, Chunked Prefill]
    CRD --> DW[DecodeWorker Clique\nTP=2, P/D RDMA KV-Cache]

    DW -. EndpointSlice Ready .-> Sync[LiteLLM Synchronizer\nContrat 3 : POST /model/new]
    Sync --> GW
    
    KEDA[KEDA ScaledObjects] -->|vllm:num_requests_waiting| PW
    KEDA -->|vllm:gpu_cache_usage_perc| DW
```

---

## 2. Structure du Monorepo

- `contracts/` : Contrats d'interfaces figés entre les agents (JSON Schema, OpenAPI 3.1).
  - `model_spec.schema.json` : Schéma d'entrée/sortie du calculateur Auto-Tuner (**Contrat 1**).
  - `dynamo_crd.schema.json` : Schéma de validation Kubernetes DynamoGraphDeployment (**Contrat 2**).
  - `control_plane_openapi.yaml` : Spécification REST OpenAPI du Control Plane.
- `scripts/` :
  - `bootstrap_inference_cluster.sh` : Script d'initialisation bare-metal Ubuntu (K3s, GPU Operator, RDMA, Cloudflare Tunnel). Validé `shellcheck`.
- `infra/` : Manifests d'infrastructure Kubernetes :
  - `gpu-operator/` : Helm values GPU Operator (DCGM + GPUDirect RDMA).
  - `network-rdma/` : NicClusterPolicy pour RoCE / InfiniBand.
  - `storage-nvme/` : StorageClass local et PVC pour `/mnt/nvme/model-cache`.
  - `cloudflare/` : Ingress-NGINX, Cloudflare Tunnel (2 réplicas) et ExternalDNS.
- `dynamo/` :
  - `helm-values/` : Configuration NVIDIA Dynamo Platform, Grove et KAI Scheduler.
  - `templates/` : Templates Jinja2 `dgd-disaggregated.yaml.j2` et `dgd-aggregated.yaml.j2`.
- `loader/` :
  - `model_warmer.py` : Téléchargement parallèle rapide avec `hf_transfer` vers le cache NVMe.
  - `Dockerfile` : Image de pré-chargement.
- `control-plane/` :
  - `autotuner/` : Inspecteur Hugging Face (`hf_inspector.py`), calculateur VRAM exact (`vram_calculator.py`), planificateur SLA (`sla_planner.py`).
  - `api/` : API FastAPI (`main.py`), client K8s async (`k8s_client.py`), reconciliateur Dynamo (`dynamo_reconciler.py`), synchroniseur LiteLLM (`litellm_sync.py`).
- `gateway/` :
  - `litellm-deployment.yaml` : Déploiement LiteLLM Proxy + Postgres.
  - `config.yaml` : Configuration LiteLLM Proxy.
- `monitoring/` :
  - `keda-scaledobjects.yaml.j2` : ScaledObjects KEDA pour scalabilité indépendante Prefill et Decode.
  - `prometheus-rules.yaml` : Règles d'alerting et d'enregistrement DCGM / vLLM.
- `tests/` :
  - `test_vram_calculator.py` : Tests des formules mathématiques de VRAM et divisibilité $H_{kv} \pmod{TP} = 0$.
  - `test_crd_rendering.py` : Tests de rendu et de conformité JSON Schema des templates CRD.
  - `e2e_deployment_flow.py` : Test de bout en bout (déploiement, K8s reconciliation, LiteLLM registration, drain 15s, suppression).

---

## 3. Formules Mathématiques du Calculateur VRAM (Section 5)

1. **Mémoire des Poids ($M_{\text{weights}}$)** :
   $$M_{\text{weights}} = N_{\text{params}} \times B_{\text{dtype}} \times 1.05$$
   où $B_{\text{dtype}} = 2$ octets (BF16/FP16), $1$ octet (FP8), ou $0.55$ octet (AWQ/INT4).

2. **Mémoire du KV-Cache par Token ($M_{\text{kv\_token}}$)** :
   $$M_{\text{kv\_token}} = 2 \times L \times H_{\text{kv}} \times D_{\text{head}} \times B_{\text{kv\_dtype}}$$
   Support natif de Grouped-Query Attention (GQA) avec $D_{\text{head}} = \text{hidden\_size} / \text{num\_attention\_heads}$.

3. **Allocation Totale et Sélection de $TP \in \{1, 2, 4, 8\}$** :
   $$M_{\text{total}} = \frac{M_{\text{weights}} + (M_{\text{kv\_token}} \times C_{\text{max}} \times B_{\text{seq}}) + M_{\text{cuda\_graphs}}}{\text{gpu\_memory\_utilization}}$$
   Contrainte critique :
   $$TP = \min \left\{ p \in \{1, 2, 4, 8\} \;\middle\vert{}\; p \times \text{VRAM}_{\text{GPU}} \ge M_{\text{total}} \text{ et } H_{\text{kv}} \pmod p = 0 \right\}$$

---

## 4. Validation des Quality Gates

Pour exécuter la suite de tests et vérifier les Quality Gates :

```bash
# Vérification des scripts shell
shellcheck scripts/*.sh

# Exécution de la suite de tests complète (Unitaires + E2E)
pytest -v tests/
```
