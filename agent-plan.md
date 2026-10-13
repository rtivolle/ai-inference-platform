# Plan d'Exécution Multi-Agents : Plateforme d'Inférence Kubernetes + NVIDIA Dynamo + LiteLLM + Cloudflare

Ce document définit l'architecture organisationnelle, les contrats d'interface, l'arborescence du monorepo, le graphe de dépendances (DAG) et les instructions système pour une **équipe de 6 agents autonomes** chargée de construire, tester et déployer l'intégralité de la plateforme d'inférence distribuée.

## 1. Topologie de l'Équipe d'Agents et Responsabilités

Chaque agent possède un périmètre d'écriture strict dans le monorepo afin d'éviter tout conflit de fusion lors des exécutions parallèles.

| 

| **ID Agent** | **Rôle** | **Périmètre Monorepo** | **Responsabilités Clés** | 
| **Agent 0** | **Lead Architect & Orchestrator** | `/`, `contracts/`, `.github/` | Définit les schémas Pydantic/OpenAPI partagés, orchestre le DAG d'exécution, valide les Quality Gates et fusionne les branches. | 
| **Agent 1** | **Infra, K8s & Cloudflare Network** | `infra/`, `scripts/` | Automatise l'installation OS/GPU, K3s, [NVIDIA GPU Operator](https://www.google.com/search?q=https%3A%2F%2Fdocs.nvidia.com%2Fdynamo%2Fkubernetes%2Fgetting-started%2Fquickstart), Ingress-NGINX, [Cloudflare Tunnel et ExternalDNS](https://www.google.com/search?q=https%3A%2F%2Fitnext.io%2Fexposing-kubernetes-apps-to-the-internet-with-cloudflare-tunnel-ingress-controller-and-e30307c0fcb0). | 
| **Agent 2** | **NVIDIA Dynamo & Disaggregated Serving** | `dynamo/` | Déploie [NVIDIA Dynamo Platform](https://www.google.com/search?q=https%3A%2F%2Fdocs.nvidia.com%2Fdynamo%2Fv-0-9-0%2Fkubernetes-deployment%2Fdeployment-guide%2Fdetailed-installation-guide) (CRDs, Grove, KAI Scheduler), conçoit les templates `DynamoGraphDeployment` (Prefill/Decode) et configure le transfert KV-Cache RDMA/NVLink. | 
| **Agent 3** | **Auto-Tuner & Model Loader Engine** | `control-plane/autotuner/`, `loader/` | Inspecte les métadonnées Hugging Face / S3 (`config.json`), calcule la VRAM (poids + KV-Cache), déduit TP/PP/Quantization/Ratio P-D et gère le pré-chargement sur cache NVMe. | 
| **Agent 4** | **Control Plane API & LiteLLM Sync** | `control-plane/api/`, `gateway/` | Développe le WebService FastAPI, pilote les CRDs Kubernetes, surveille les `EndpointSlices` et synchronise à chaud les modèles dans [LiteLLM Proxy](https://www.google.com/search?q=https%3A%2F%2Fdocs.litellm.ai%2Fdocs%2Fproxy%2Fmodel_management) (`/model/new`, `/model/delete`). | 
| **Agent 5** | **Autoscaling (KEDA), Observabilité & QA** | `monitoring/`, `tests/` | Configure Prometheus + DCGM Exporter, crée les `ScaledObject` KEDA basés sur les métriques vLLM/Dynamo et exécute les tests de bout en bout (E2E) et de charge. | 

## 2. Arborescence Cible du Monorepo

Tous les agents travaillent sur une structure de dépôt unifiée :

```
ai-inference-platform/
├── contracts/                        # [Agent 0] Contrats figés entre agents
│   ├── model_spec.schema.json        # Entrée/Sortie du calculateur Auto-Tuner
│   ├── dynamo_crd.schema.json        # Schéma de génération DynamoGraphDeployment
│   └── control_plane_openapi.yaml    # Spécification REST du WebService
├── scripts/                          # [Agent 1] Scripts d'initialisation bare-metal
│   └── bootstrap_inference_cluster.sh
├── infra/                            # [Agent 1] Manifests K8s d'infrastructure
│   ├── gpu-operator/
│   ├── network-rdma/
│   ├── storage-nvme/
│   └── cloudflare/                   # cloudflared + external-dns + ingress-nginx
├── dynamo/                           # [Agent 2] Templates & CRDs NVIDIA Dynamo
│   ├── helm-values/
│   └── templates/
│       ├── dgd-disaggregated.yaml.j2 # Template Prefill/Decode séparés (Grove)
│       └── dgd-aggregated.yaml.j2    # Template standard monobloc
├── loader/                           # [Agent 3] Job/Daemon de cache NVMe
│   ├── Dockerfile
│   └── model_warmer.py               # Téléchargement parallèle HF/S3 -> NVMe
├── control-plane/                    # [Agent 3 & Agent 4] Backend Python FastAPI
│   ├── autotuner/                    # [Agent 3] Calculateur VRAM, TP, PP, P/D Ratio
│   │   ├── hf_inspector.py
│   │   ├── vram_calculator.py
│   │   └── sla_planner.py
│   └── api/                          # [Agent 4] Serveur FastAPI + K8s Watcher + LiteLLM
│       ├── main.py
│       ├── k8s_client.py
│       ├── dynamo_reconciler.py
│       └── litellm_sync.py
├── gateway/                          # [Agent 4] Configuration LiteLLM + Postgres + Redis
│   ├── litellm-deployment.yaml
│   └── config.yaml
├── monitoring/                       # [Agent 5] KEDA, Prometheus, Grafana
│   ├── keda-scaledobjects.yaml.j2
│   └── prometheus-rules.yaml
└── tests/                            # [Agent 5] Suite de validation unitaire & E2E
    ├── test_vram_calculator.py
    ├── test_crd_rendering.py
    └── e2e_deployment_flow.py

```

## 3. Contrats d'Interface Techniques (Handshakes Inter-Agents)

Pour permettre aux agents 2, 3 et 4 de coder simultanément sans blocage, l'**Agent 0** verrouille trois contrats de données dès l'initialisation.

### Contrat 1 : Sortie de l'Auto-Tuner (Agent 3 -> Agent 2 & Agent 4)

L'Agent 3 expose une fonction pure `compute_deployment_plan(request: DeployRequest, cluster_gpu_inventory: ClusterInventory) -> DeploymentPlan` qui retourne la structure suivante :

```
{
  "model_id": "meta-llama/Llama-3.3-70B-Instruct",
  "serving_mode": "disaggregated",
  "quantization": "fp8",
  "max_model_len": 32768,
  "gpu_memory_utilization": 0.90,
  "estimated_vram_gb_per_replica": 78.4,
  "prefill_clique": {
    "replicas": 2,
    "tensor_parallel_size": 4,
    "pipeline_parallel_size": 1,
    "gpu_sku": "NVIDIA-H100-80GB",
    "max_num_batched_tokens": 16384,
    "enable_chunked_prefill": true
  },
  "decode_clique": {
    "replicas": 4,
    "tensor_parallel_size": 2,
    "pipeline_parallel_size": 1,
    "gpu_sku": "NVIDIA-H100-80GB",
    "max_num_seqs": 256
  },
  "kv_cache_transfer": {
    "protocol": "rdma",
    "Engine": "nixl"
  }
}

```

### Contrat 2 : Rendu du CRD `DynamoGraphDeployment` (Agent 2 -> Agent 4)

L'Agent 2 fournit un moteur de rendu Jinja2 qui transforme le `DeploymentPlan` JSON en manifeste Kubernetes `DynamoGraphDeployment` valide pour le [Dynamo Operator](https://www.google.com/search?q=https%3A%2F%2Fdocs.nvidia.com%2Fdynamo%2Fv1.1.0%2Fkubernetes-deployment%2Fdeployment-guide%2Fdynamo-operator) :

```
apiVersion: nvidia.com/v1alpha1
kind: DynamoGraphDeployment
metadata:
  name: "{{ deployment_name }}"
  namespace: "{{ namespace }}"
  labels:
    app.kubernetes.io/managed-by: dynamo-control-plane
spec:
  backend: vllm
  model: "{{ model_id }}"
  sharedStorage:
    hostPath: "/mnt/nvme/model-cache"
  services:
    Frontend:
      replicas: 2
      ports:
        - name: http
          port: 8000
    PrefillWorker:
      replicas: {{ prefill_clique.replicas }}
      resources:
        limits:
          nvidia.com/gpu: {{ prefill_clique.tensor_parallel_size }}
      extraArgs:
        - "--tensor-parallel-size={{ prefill_clique.tensor_parallel_size }}"
        - "--quantization={{ quantization }}"
        - "--max-model-len={{ max_model_len }}"
        - "--gpu-memory-utilization={{ gpu_memory_utilization }}"
    DecodeWorker:
      replicas: {{ decode_clique.replicas }}
      resources:
        limits:
          nvidia.com/gpu: {{ decode_clique.tensor_parallel_size }}
      extraArgs:
        - "--tensor-parallel-size={{ decode_clique.tensor_parallel_size }}"
        - "--quantization={{ quantization }}"
        - "--max-model-len={{ max_model_len }}"

```

### Contrat 3 : Synchronisation Dynamique LiteLLM (Agent 4)

Dès que l'`EndpointSlice` du service `{{ deployment_name }}-frontend` passe à `Ready=true`, l'Agent 4 exécute l'appel REST suivant vers [LiteLLM Proxy](https://www.google.com/search?q=https%3A%2F%2Fdocs.litellm.ai%2Fdocs%2Fproxy%2Fmodel_management) :

```
POST http://litellm-service.ai-gateway.svc.cluster.local:4000/model/new
Authorization: Bearer <LITELLM_MASTER_KEY>
Content-Type: application/json

{
  "model_name": "llama-3.3-70b",
  "litellm_params": {
    "model": "openai/meta-llama/Llama-3.3-70B-Instruct",
    "api_base": "http://llama-3-3-70b-frontend.dynamo-workloads.svc.cluster.local:8000/v1",
    "api_key": "internal-dynamo-key",
    "rpm": 1000,
    "timeout": 600
  },
  "model_info": {
    "id": "dgd-llama-3-3-70b",
    "mode": "chat",
    "supports_function_calling": true
  }
}

```

## 4. Plan d'Exécution Séquentiel et Parallèle (DAG en 5 Vagues)

Le projet est découpé en **5 vagues (Waves)**. Au sein d'une même vague, les agents travaillent simultanément.

| **Vague** | **Agents Actifs** | **Tâches Exécutées en Parallèle** | **Critère de Validation (Quality Gate)** | 
| **Vague 1 : Fondations & Contrats** | **Agent 0**, **Agent 1** | **Agent 0** : Initialise le monorepo et publie les 3 schémas dans `contracts/`.  **Agent 1** : Finalise `bootstrap_inference_cluster.sh`, les manifests Cloudflare Tunnel (`cloudflared`), `external-dns` et le StorageClass NVMe. | Validation syntaxique `shellcheck` / `kubeconform` sur `infra/` et validation JSON Schema sur `contracts/`. | 
| **Vague 2 : Développement Cœur (Parallèle)** | **Agent 2**, **Agent 3**, **Agent 4** | **Agent 2** : Écrit les templates Jinja2 `DynamoGraphDeployment` (agrégé et désagrégé P/D) + scripts d'init RDMA.  **Agent 3** : Code l'inspecteur Hugging Face (`hf_inspector.py`), le calculateur de VRAM (`vram_calculator.py`) et le job de pré-chargement NVMe (`model_warmer.py`).  **Agent 4** : Développe l'API FastAPI (`POST /models/deploy`, `DELETE /models/{id}`, `GET /cluster/gpus`), le client K8s (`kubernetes_asyncio`) et le connecteur LiteLLM. | Tests unitaires isolés : l'Agent 3 génère un JSON conforme au Contrat 1 ; l'Agent 2 produit un YAML valide selon le Contrat 2 ; l'Agent 4 passe les tests avec mocks K8s/LiteLLM. | 
| **Vague 3 : Autoscaling & Intégration** | **Agent 0**, **Agent 4**, **Agent 5** | **Agent 5** : Crée les templates `ScaledObject` KEDA basés sur les métriques Prometheus de Dynamo (`vllm:num_requests_waiting`, `vllm:gpu_cache_usage_perc`).  **Agent 4 & Agent 0** : Intègrent les modules `autotuner/` (Agent 3) et `dynamo/templates/` (Agent 2) dans le pipeline de déploiement de `control-plane/api/`. | Construction de l'image Docker du Control Plane réussie et lancement du conteneur en environnement de test (Kind / K3s). | 
| **Vague 4 : Déploiement Cluster & DNS** | **Agent 1**, **Agent 2**, **Agent 4** | Exécution du bootstrap sur le cluster cible : installation GPU Operator, Dynamo Platform (avec Grove & KAI), LiteLLM + Postgres, Ingress-NGINX, Cloudflare Tunnel et Control Plane. | `kubectl get pods -A` affiche 100 % des pods en état `Running` ; `https://llm.<domaine>/health/readiness` et `https://control.<domaine>/health` répondent `200 OK` via Cloudflare. | 
| **Vague 5 : Recette E2E & Chaos Testing** | **Agent 5**, **Agent 0** | **Agent 5** : Déploie un modèle test via `POST https://control.<domaine>/api/v1/models/deploy`, vérifie le téléchargement NVMe, l'instanciation du CRD Dynamo, l'enregistrement automatique dans LiteLLM et lance un tir de charge streaming SSE. Simule la suppression (`DELETE`) et vérifie le drain sans erreur HTTP 502. | Zéro requête perdue lors de l'enregistrement et du retrait du modèle ; latence TTFT/TPOT conforme aux objectifs. | 

## 5. Algorithme Détaillé pour l'Agent 3 (Auto-Tuner & Calculateur VRAM)

L'Agent 3 doit implémenter les formules exactes suivantes dans `control-plane/autotuner/vram_calculator.py` pour éviter toute erreur `CUDA Out Of Memory` (OOM) au démarrage des pods Dynamo :

1. **Mémoire des Poids du Modèle (**$M_{\text{weights}}$**)** :
   

   $$
   M_{\text{weights}} = N_{\text{params}} \times B_{\text{dtype}} \times 1.05 \quad (\text{+5 \% pour les buffers d'embedding/LM head})
   $$

   
   où $B_{\text{dtype}} = 2$ octets (FP16/BF16), $1$ octet (FP8), ou $0.55$ octet (INT4/AWQ avec overhead de groupes).

2. **Mémoire du KV-Cache par Token (**$M_{\text{kv\_token}}$**)** :
   

   $$
   M_{\text{kv\_token}} = 2 \times L \times H_{\text{kv}} \times D_{\text{head}} \times B_{\text{kv\_dtype}}
   $$

   
   où $L$ = `num_hidden_layers`, $H_{\text{kv}}$ = `num_key_value_heads` (support natif de Grouped-Query Attention - GQA), et $D_{\text{head}} = \text{hidden\_size} / \text{num\_attention\_heads}$.

3. **Allocation Totale et Calcul de `tensor_parallel_size` (TP)** :
   

   $$
   M_{\text{total}} = \frac{M_{\text{weights}} + (M_{\text{kv\_token}} \times C_{\text{max}} \times B_{\text{seq}}) + M_{\text{cuda\_graphs}}}{\text{gpu\_memory\_utilization}}
   $$

   
   Le degré de parallélisme de tenseur $TP$ est choisi dans $\{1, 2, 4, 8\}$ tel que :
   

   $$
   TP = \min \left\{ p \in \{1, 2, 4, 8\} \;\middle\vert{}\; p \times \text{VRAM}_{\text{GPU\_unitaire}} \ge M_{\text{total}} \text{ et } H_{\text{kv}} \pmod p = 0 \right\}
   $$

   
   *(Contrainte critique : le nombre de têtes KV* $H_{\text{kv}}$ *doit toujours être divisible par* $TP$*).*

4. **Décision d'Architecture Agrégée vs Désagrégée (Prefill / Decode)** :

   * Si le modèle requiert $TP \ge 2$ et que le profil demandé est `low-latency` ou `high-throughput` (et qu'au moins $2 \times TP$ GPU sont libres sur le cluster) : activer le mode **Disaggregated Serving** de Dynamo (`PrefillWorker` + `DecodeWorker`).

   * Sinon : basculer en mode **Aggregated** (un seul groupe de workers combinant Prefill et Decode) pour économiser les GPU.

## 6. Prompts Système Prêts à l'Emploi pour chaque Agent

Ces instructions peuvent être injectées directement dans votre orchestrateur multi-agents (Claude Code Subagents, LangGraph, CrewAI ou AutoGen) :

### Prompt — Agent 1 (Infra, K8s & Cloudflare)

> Tu es l'Agent Infrastructure & Réseau Kubernetes. Ton périmètre d'écriture exclusif est `infra/` et `scripts/`. Tu dois maintenir le script d'installation Ubuntu bare-metal (`bootstrap_inference_cluster.sh`) et les chartes Helm pour K3s, NVIDIA GPU Operator, Network Operator (RDMA), Ingress-NGINX, Cloudflare Tunnel (`cloudflared` en 2 réplicas) et `external-dns`. Aucun port entrant ne doit être ouvert sur l'hôte : tout le trafic externe passe par le tunnel Cloudflare vers `ingress-nginx-controller` en ClusterIP. Assure-toi que tous les Ingress supportent le streaming SSE longue durée (`proxy-read-timeout: 3600`, `proxy-buffering: "off"`).

### Prompt — Agent 2 (NVIDIA Dynamo Specialist)

> Tu es l'Agent spécialiste NVIDIA Dynamo et Kubernetes CRDs. Ton périmètre d'écriture exclusif est `dynamo/`. Tu dois produire les templates Jinja2 permettant de générer des ressources `DynamoGraphDeployment` et `DynamoGraphDeploymentRequest` compatibles avec le Dynamo Operator, Grove (`PodCliqueSet`) et KAI Scheduler. Respecte strictement le schéma d'entrée `contracts/model_spec.schema.json`. Monte systématiquement le volume `/mnt/nvme/model-cache` dans les pods workers (`HF_HOME=/mnt/nvme/model-cache`) pour éviter tout téléchargement redondant.

### Prompt — Agent 3 (Auto-Tuner & Model Loader)

> Tu es l'Agent responsable du dimensionnement mathématique des modèles et du pré-chargement des poids. Ton périmètre d'écriture exclusif est `control-plane/autotuner/` et `loader/`. Implémente la récupération sans téléchargement des fichiers `config.json` et `model.safetensors.index.json` depuis Hugging Face ou S3, calcule au mégaoctet près l'empreinte VRAM des poids, du KV-Cache (en tenant compte de GQA/MLA) et des graphes CUDA, puis détermine automatiquement `tensor_parallel_size` (en vérifiant que `num_key_value_heads % tp == 0`), la quantification (`fp16`, `fp8`, `awq`) et le ratio de réplicas Prefill/Decode. Écris également le Job Kubernetes `model_warmer.py` utilisant `huggingface_hub` avec `hf_transfer` activé pour saturer la bande passante vers le disque NVMe.

### Prompt — Agent 4 (Control Plane API & LiteLLM Sync)

> Tu es l'Agent Backend Control Plane & Passerelle LiteLLM. Ton périmètre d'écriture exclusif est `control-plane/api/` et `gateway/`. Développe une API FastAPI asynchrone exposant :
>
> * `POST /api/v1/models/deploy` : appelle le module `autotuner` (Agent 3), lance le Job de pré-chargement NVMe, génère le manifeste `DynamoGraphDeployment` (Agent 2) et l'applique via `kubernetes_asyncio`.
>
> * Une tâche de fond (Kubernetes Watcher sur les `EndpointSlices`) qui détecte quand le Service Frontend Dynamo possède au moins un endpoint `ready=true`, puis enregistre immédiatement le modèle dans LiteLLM via `POST /model/new`.
>
> * `DELETE /api/v1/models/{model_name}` : retire d'abord le modèle de LiteLLM via `POST /model/delete`, attend 15 secondes pour drainer les générations en cours, puis supprime le `DynamoGraphDeployment` sur Kubernetes.

### Prompt — Agent 5 (Autoscaling KEDA & QA E2E)

> Tu es l'Agent Observabilité, Autoscaling et Assurance Qualité. Ton périmètre d'écriture exclusif est `monitoring/` et `tests/`. Crée les définitions `ScaledObject` KEDA qui scalent indépendamment les cliques `PrefillWorker` (selon la longueur de la file d'attente `vllm:num_requests_waiting`) et `DecodeWorker` (selon la saturation du KV-Cache `vllm:gpu_cache_usage_perc`). Écris la suite de tests `pytest` couvrant les calculs VRAM de l'Agent 3, la validité des manifestes YAML de l'Agent 2 et le test d'intégration E2E complet.