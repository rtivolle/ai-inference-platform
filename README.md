# Plateforme d'Inférence Kubernetes + NVIDIA Dynamo + LiteLLM + Cloudflare

Plateforme d'inférence distribuée multi-modèles à haute performance, conçue pour orchestrer des LLMs en architecture désagrégée (Prefill & Decode) avec NVIDIA Dynamo, synchronisation à chaud dans LiteLLM Proxy, et exposition sécurisée sans port entrant via Cloudflare Tunnel.

Le pipeline de déploiement vers plusieurs clusters Kubernetes indépendants est
documenté dans [deploy/README.md](deploy/README.md) : images immuables publiées une
fois, secrets propres à chaque cluster, validation et vérification des rollouts.

---

## Console Web

Le Control Plane sert une console à la racine `/` : inventaire GPU, recherche et filtrage des
déploiements, détails du plan, création de modèles et suppression avec confirmation. Les données
sont actualisées toutes les 10 secondes lorsque l'onglet est visible. Le token Bearer reste
uniquement en mémoire dans l'onglet ; il est effacé lors de la déconnexion ou du rechargement.

Pour essayer la console sans cluster (Python 3.11) :

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
CONTROL_PLANE_MOCK=1 uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Ouvrir `http://127.0.0.1:8000` et utiliser `test-token` pour se connecter. Le bandeau
« Mock environment » indique que l'inventaire et les déploiements sont simulés. Les profils
connus proposés dans le formulaire fonctionnent hors ligne ; les autres identifiants interrogent
Hugging Face pour obtenir leurs métadonnées. Les paramètres avancés permettent de régler la
longueur de contexte, la concurrence et l'allocation mémoire.

Avec un cluster réel, ne pas définir `CONTROL_PLANE_MOCK`. Configurer `CONTROL_PLANE_API_TOKEN`,
`LITELLM_MASTER_KEY` et `LITELLM_PROXY_URL` via l'environnement ou des Secrets Kubernetes,
ainsi que les credentials Kubernetes. La console utilise les mêmes endpoints protégés que l'API
et affiche les erreurs de capacité ou de déploiement retournées par le service.

Alternative Docker, toujours en simulation locale :

```bash
docker build -t ai-inference-control-plane:local -f control-plane/Dockerfile .
docker run --rm -p 127.0.0.1:8000:8000 -e CONTROL_PLANE_MOCK=1 ai-inference-control-plane:local
```

La référence API interactive reste disponible sur `/docs`. Les fichiers de la console se trouvent
dans `control-plane/api/web/` et ne nécessitent ni Node.js ni compilation pour l'exécution.

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

## Installation et configuration automatique de LiteLLM

Voir le [guide LiteLLM](gateway/README.md) pour installer la passerelle, créer les
secrets partagés avec le control plane et configurer automatiquement PostgreSQL
persistant et l’enregistrement dynamique des modèles.


## Remédiation R5 et validation avant cluster

Les templates ciblent NVIDIA Dynamo **v1.5.1**, `nvidia.com/v1beta1` et
`spec.components`. Le frontend lance `dynamo.frontend` avec routage KV ; les
workers lancent `python3 -m dynamo.vllm`. La désagrégation utilise
`NixlConnector` avec `--disaggregation-mode` et des événements KV côté prefill.
Les workers RDMA demandent `rdma/rdma_shared_device` et la capacité `IPC_LOCK` ;
le device plugin du Network Operator doit exposer cette ressource.

`DYNAMO_RUNTIME_IMAGE` permet de fournir une image compatible, idéalement par
digest, et `DYNAMO_RUNTIME_IMAGE_PULL_SECRET` configure le secret de registre.
`MODEL_WARMER_IMAGE` et `MODEL_WARMER_IMAGE_PULL_SECRET` configurent le warmer.

Le réconciliateur découvre les nœuds GPU Ready, non cordonnés, avec le SKU et
la capacité par pod requis. Il crée un Job épinglé à chaque hostname et attend
**toutes** les complétions avant de créer le DGD. Les workers sont limités à ces
mêmes hostnames. Les échecs et timeouts du warmer bloquent le déploiement.
Un nouveau nœud ne rejoint pas ce périmètre automatiquement : redéployer le
modèle pour préchauffer le cache et actualiser les hostnames autorisés.

Les composants prefill/decode activent `scalingAdapter: {}`. Le réconciliateur
applique leurs deux ScaledObjects KEDA après le DGD, puis les supprime après
retrait LiteLLM et drain, avant suppression du DGD. Le control plane doit
recevoir le RBAC actualisé dans `infra/control-plane-rbac.yaml`. Son image
inclut désormais les templates `monitoring/`.

Utiliser Python 3.11 comme en CI :

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]' shellcheck-py
CONTROL_PLANE_MOCK=1 pytest -v tests/
python -m scripts.validate_manifests --kubeconform /path/to/kubeconform
```

Le validateur utilise kubeconform v0.7.0 et Kubernetes 1.31.0. Il contrôle les
manifestes `infra/` et `gateway/` et les rendus DGD, warmer et KEDA contre les
CRDs officiels versionnés Dynamo v1.5.1, KEDA v2.16.1 et Network Operator
v24.7.0, sans ignorer les schémas manquants. Les fichiers Helm values et les
configurations sans GVK sont exclus. La validation Kind, le bootstrap réel et
les tests GPU/SSE de charge et chaos restent à exécuter dans les vagues 4–5.
