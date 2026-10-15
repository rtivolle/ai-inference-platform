Audit Architectural et Plan d'Exécution Corrigé : Plateforme d'Inférence Distribuée Kubernetes, NVIDIA Dynamo, LiteLLM et Cloudflare
L'examen approfondi du plan d'exécution multi-agents proposé révèle une structuration méthodologique solide — notamment par le découpage strict des périmètres d'écriture au sein du monorepo et le séquencement par vagues (DAG) — mais met en évidence des divergences techniques bloquantes vis-à-vis des spécifications de production de NVIDIA Dynamo (versions v1.5 et supérieures), du moteur de transfert RDMA NIXL, de l'opérateur d'autoscaling KEDA et de la passerelle LiteLLM1. En l'état actuel, l'exécution littérale des contrats d'interface 1, 2 et 3 ainsi que des algorithmes de l'Agent 3 et de l'Agent 5 entraînerait des rejets de validation par le serveur d'API Kubernetes en raison d'un schéma CRD obsolète, l'inactivation silencieuse de la désagrégation Prefill/Decode faute de connecteurs NIXL déclarés, des conflits d'état lors de l'autoscaling et des erreurs HTTP 502 lors du retrait de modèles sous charge2.

1. Synthèse de l'Audit Technique et Écarts Critiques Identifiés
L'architecture cible repose sur l'orchestration conjointe de quatre sous-systèmes : l'infrastructure réseau et matérielle (K3s, NVIDIA GPU Operator, RDMA, Cloudflare Tunnel), le plan d'orchestration d'inférence distribuée (NVIDIA Dynamo, Grove, KAI Scheduler), le plan de contrôle et de routage applicatif (FastAPI, LiteLLM, PostgreSQL, Redis) et la boucle d'observabilité et d'élasticité (Prometheus, DCGM, KEDA)5. L'audit croisé de la conception initiale avec la documentation de référence fait ressortir sept points de rupture majeurs qui doivent être corrigés par l'Agent 0 avant le lancement de la Vague 12.

Domaine Technique
Conception du Plan Initial
Réalité Documentaire et Comportement Système
Impact Opérationnel et Correction Requise
API & Schéma CRD Dynamo (Agent 2)
Utilise nvidia.com/v1alpha1, spec.backend, spec.sharedStorage et un dictionnaire spec.services avec extraArgs.
Depuis Dynamo v1.5+, nvidia.com/v1beta1 est la version de stockage canonique1. Le schéma exige spec.backendFramework et une liste spec.components contenant podTemplate.spec.containers[name=main]2.
Rejet du manifeste ou échec de réconciliation. Refonte complète du Contrat 2 vers nvidia.com/v1beta1 avec type: frontend, prefill et decode2.
Configuration NIXL & KV-Cache (Agent 2)
Mentionne NIXL dans le Contrat 1 mais omet les drapeaux d'exécution vLLM dans le Contrat 2.
La désagrégation exige python3 -m dynamo.vllm avec --disaggregation-mode (prefill/decode) et --kv-transfer-config déclarant NixlConnector ou PdConnector (KVBM)4.
Fausse désagrégation. Les workers démarrent en mode isolé sans transfert RDMA7. Injection obligatoire des JSON --kv-transfer-config et de DYN_ROUTER_MODE=kv4.
Autoscaling KEDA (Agent 5)
Cible directement les cliques ou le DynamoGraphDeployment (DGD) via KEDA.
Le champ autoscaling du DGD est déprécié et ignoré5. L'opérateur génère un DynamoGraphDeploymentScalingAdapter (DGDSA) par composant qui possède l'autorité sur replicas5.
Conflit d'état et inopérance de KEDA. Le ScaledObject doit cibler la ressource DynamoGraphDeploymentScalingAdapter (nvidia.com/v1beta1) et arbitrer avec le Dynamo Planner5.
Synchronisation LiteLLM (Agent 4)
Appelle POST /model/new et POST /model/delete avec un drain fixe de 15 secondes.
Ces routes échouent si STORE_MODEL_IN_DB=True et LITELLM_SALT_KEY sont absents3. En mode multi-réplicas, seul le pod recevant l'appel se met à jour immédiatement ; les autres attendent PROXY_CONFIG_RELOAD_INTERVAL_SECONDS6.
Erreurs HTTP 502 au retrait du modèle. Activation du mode DB PostgreSQL, réduction de l'intervalle de rafraîchissement des réplicas et alignement du délai de drain3.
Calculateur VRAM & Profilage (Agent 3)
Formule symétrique unique (), uniquement GQA, graphes CUDA sur Prefill, désagrégation si .
Le Prefill désagrégé utilise --enforce-eager (sans CUDA graphs) et optionnellement KVBM CPU, tandis que Decode utilise les CUDA graphs4. Les modèles MLA (DeepSeek) et MoE faussent la formule GQA14.
OOM au démarrage ou sous-utilisation GPU. Séparation des équations VRAM Prefill () et Decode (), support MLA/MoE, et intégration avec le profileur natif DynamoGraphDeploymentRequest (DGDR)4.
Cache NVMe hostPath (Agent 1 & 3)
Un Job Kubernetes unique (model_warmer.py) télécharge les poids dans /mnt/nvme/model-cache.
Un Job standard s'exécute sur un seul nœud. Sur un cluster multi-nœuds, les pods planifiés par KAI sur les autres nœuds trouveront un répertoire /mnt/nvme/model-cache vide.
Téléchargements redondants ou crashs au démarrage. Transformation du Job en DaemonSet ou en Job indexé par nœud (Indexed completion mode), ou utilisation d'un PVC ReadWriteMany (RWX)20.
Dépendances Dynamo & Réseau (Agent 1)
Installe GPU Operator, Grove, KAI et Cloudflare Tunnel (proxy-read-timeout: 3600).
Dynamo requiert NATS (JetStream), etcd, le CRD ClusterPolicy et le label de nœud nvidia.com/gpu.present=true8. Cloudflare coupe les connexions inactives après 100s (HTTP 524).
Échec d'installation de l'opérateur et coupures SSE. Déploiement de NATS/etcd via dynamo-platform, labellisation des nœuds GPU et maintien de vie SSE8.

1. Architecture NVIDIA Dynamo (v1.5+) : Du Schéma v1alpha1 Obsolète au Graphe Désagrégé v1beta1
La première erreur structurelle du plan initial réside dans le Contrat 2, qui s'appuie sur une syntaxe nvidia.com/v1alpha1 obsolète utilisant un bloc spec.services et des clés de haut niveau spec.backend, spec.model et spec.sharedStorage. À partir de la version v1.5.0 de NVIDIA Dynamo, nvidia.com/v1beta1 est devenue la version de stockage canonique persistée dans etcd pour l'ensemble des ressources personnalisées critiques : DynamoGraphDeployment (DGD), DynamoComponentDeployment (DCD), DynamoGraphDeploymentRequest (DGDR) et DynamoGraphDeploymentScalingAdapter (DGDSA)1. Bien qu'un webhook de conversion maintienne la compatibilité de lecture pour v1alpha1 et qu'un contrôleur de migration asynchrone réécrive les objets existants via Server-Side Apply, tous les nouveaux gabarits et surcharges (overrides) doivent impérativement être rédigés en nvidia.com/v1beta11.
Dans le schéma nvidia.com/v1beta1, le champ spec.services est remplacé par une liste typée spec.components, où chaque élément est une instance de DynamoComponentDeploymentSharedSpec caractérisée par un nom (name), un rôle explicite (type choisi parmi frontend, worker, prefill, decode, planner, EPP), un nombre de réplicas (replicas) et une spécification complète de pod (podTemplate)2. De plus, chaque composant non-LPX doit obligatoirement définir un conteneur nommé main (spec.components[*].podTemplate.spec.containers[name=main]) doté d'une image valide, exécutant la commande python3 -m dynamo.vllm dans le répertoire /workspace/examples/backends/vllm2. Si l'image conteneur utilisée par l'Agent 2 possède un tag qui ne suit pas le versionnement sémantique de Dynamo (par exemple un digest SHA256 ou une version propre au moteur vLLM), le champ spec.components[*].runtimeVersionOverride doit obligatoirement être renseigné pour éviter un rejet par le webhook d'admission2. Enfin, au cours de cette transition vers v1beta1, l'ancien sélecteur de point de terminaison (Endpoint Picker - EPP) en Go a été supprimé au profit d'une implémentation native en Rust (Rust EPP) intégrée au Frontend1.

Arbitrage Architectural : Auto-Tuner Personnalisé (Agent 3) vs DynamoGraphDeploymentRequest (DGDR) Natif
Une omission stratégique majeure du plan initial concerne l'existence de la ressource DynamoGraphDeploymentRequest (DGDR) au sein de NVIDIA Dynamo2. Le plan confie à l'Agent 3 la tâche de coder de toutes pièces un moteur de dimensionnement (control-plane/autotuner/) pour calculer la taille de parallélisme de tenseur (), le ratio Prefill/Decode et générer directement un DynamoGraphDeployment2. Or, Dynamo intègre nativement ce mécanisme via le contrôleur DGDR, qui pilote le moteur de dimensionnement AI Configurator (AIC) et le Dynamo Profiler9. Lorsqu'un DGDR est soumis, le contrôleur découvre automatiquement la topologie matérielle via DCGM et les labels de nœuds, exécute un balayage de simulation rapide (qui s'achève en moins d'une minute) ou un profilage réel sur GPU (thorough, réservé au mode désagrégé), détermine la configuration Pareto-optimale (coût, latence TTFT, débit ITL/TPOT, choix entre mode agrégé et désagrégé), puis génère et applique automatiquement le DynamoGraphDeployment cible si autoApply: true est activé9.
Pour éviter que l'Agent 3 ne réinvente de manière sous-optimale ce que l'opérateur Dynamo accomplit déjà, l'architecture du Control Plane doit supporter deux modes d'orchestration complémentaires2. Dans un premier mode direct (DGD v1beta1 via l'Agent 3), l'Auto-Tuner de l'Agent 3 agit comme un calculateur déterministe instantané (sans latence de job de profilage) qui vérifie l'admissibilité mathématique en VRAM avant même de solliciter Kubernetes, et produit un DeploymentPlan injecté dans le template Jinja2 DynamoGraphDeployment de l'Agent 22. Dans un second mode piloté par l'intention (DGDR v1beta1 avec surcharges), destiné aux charges de travail complexes ou aux nouveaux matériels, l'API de l'Agent 4 soumet un DynamoGraphDeploymentRequest en injectant les paramètres calculés par l'Agent 3 (prefill_pick, decode_pick, weight_dtype, moe_dtype, kv_cache_dtype) et les personnalisations d'infrastructure (montages NVMe, variables d'environnement) dans le bloc spec.overrides.dgd au format nvidia.com/v1beta114. En v1beta1, l'opérateur DGDR fusionne intelligemment les listes de cartes (components, containers, env) par leur clé name, tout en remplaçant les listes atomiques telles que args24.
Dépendances Système Oubliées : NATS, etcd, Labels GPU Operator et Stratégies Grove
Le script bootstrap_inference_cluster.sh de l'Agent 1 et la configuration Helm de l'Agent 2 omettent trois prérequis bloquants relatifs au plan de coordination, à la découverte matérielle et à la gestion des mises à jour de graphes2. Tout d'abord, le plan de données de NVIDIA Dynamo s'appuie obligatoirement sur le bus de messagerie NATS (avec JetStream activé) et le magasin d'état etcd pour la découverte de services temps réel, la publication des événements de KV-Cache entre les workers et le routeur Frontend, ainsi que la coordination distribuée (bien qu'un mode d'expiration locale par TTL appelé vllm-agg-router-kv-approx permette de fonctionner sans NATS pour des topologies simples)4. L'Agent 2 doit impérativement déployer la charte dynamo-crds suivie de dynamo-platform, qui installe l'opérateur (nvcr.io/nvidia/ai-dynamo/kubernetes-operator), les pods dynamo-platform-nats-0 et etcd, ainsi que Grove et KAI Scheduler (activables directement via install=true et enabled=true dans les sous-chartes intégrées)8. Par ailleurs, la documentation officielle de Dynamo exige un cluster Kubernetes en version 1.30 ou ultérieure, et en version 1.34+ si l'allocation dynamique de ressources DRA v1 avec le pilote NVIDIA GPU DRA est activée4.
Ensuite, la validation du GPU Operator requiert une attention particulière sur l'étiquetage des nœuds8. Le script officiel de pré-vérification de Dynamo (deploy/pre-deployment/pre-deployment-check.sh) échoue si la CRD ClusterPolicy (clusterpolicies.nvidia.com) du NVIDIA GPU Operator n'est pas dans l'état ready, ou si les nœuds GPU ne portent pas explicitement le label nvidia.com/gpu.present=true, car certains clusters ne posent par défaut que le label nvidia.com/gpu.product via le Device Plugin autonome8. L'Agent 1 doit donc intégrer cette vérification et ce marquage explicite dans le script de bootstrap tout en vérifiant l'absence de conflit de double comptage des GPU allouables entre un Device Plugin préexistant et celui du GPU Operator8.
Enfin, l'orchestration multi-nœuds par Grove (PodCliqueSet) comporte un piège critique lié à la stratégie de mise à jour Coherent2. Grove traduit les composants Dynamo en ressources PodClique ou PodCliqueScalingGroup (PCSG) et s'interface avec KAI Scheduler (qui gère les ressources PodGang) pour assurer l'ordonnancement groupé (gang scheduling)2. Par défaut, Grove applique la stratégie de mise à jour RollingRecreate avec maxUnavailable: 12. Si l'Agent 2 souhaite activer les mises à jour coordonnées Prefill/Decode via l'annotation metadata.annotations["nvidia.com/grove-update-strategy"]: "Coherent", une précaution majeure s'impose : comme documenté dans le bogue Grove #873, une mise à jour Coherent peut se bloquer définitivement (stall) si des réplicas déjà indisponibles épuisent le budget d'interruption, et retirer l'annotation à chaud ne débloque pas une mise à jour déjà active2. De plus, si minAvailable == replicas, l'unité minimale de mise à jour peut mettre l'intégralité du composant hors ligne2. L'Agent 2 doit donc conserver RollingRecreate par défaut ou garantir minAvailable < replicas, et utiliser l'option experimental.grove.forceScalingGroup: true en v1beta1 lorsqu'un composant mono-nœud à  doit être ordonnancé comme un PodCliqueScalingGroup indépendant par réplica1. De même, le champ KvTransferEnforcement (qui vaut required par défaut) contraint le routeur à ne sélectionner que des workers Decode situés dans le même domaine topologique que le worker Prefill choisi ; si le cluster de l'Agent 1 répartit Prefill et Decode sur des domaines topologiques distincts interconnectés en RoCE/InfiniBand, ce paramètre doit être ajusté sur preferred ou aligné via topologyConstraint dans le bloc providerOverride (apiVersion: grove.io/v1alpha1)2.
3. Plan de Données Désagrégé : Protocole NIXL, KVBM, Routage KV-Aware et Contraintes RDMA
Séparer les workers Prefill et Decode dans deux déploiements Kubernetes distincts ne suffit pas à activer l'inférence désagrégée : sans configuration explicite du plan de transfert mémoire et du routeur dans les arguments de conteneur, le système fonctionne comme deux silos agrégés ou échoue à transférer l'état d'attention4.
Configuration des Connecteurs NIXL et du KV Block Manager (KVBM)
Dans l'écosystème NVIDIA Dynamo, le transfert de KV-Cache repose sur la bibliothèque NIXL (NVIDIA Inference Xfer Library), qui abstrait le réseau sous-jacent en utilisant UCX par défaut pour InfiniBand, RoCE et NVLink, LIBFABRIC pour AWS EFA, ou UCCL pour le contrôle de congestion logiciel piloté par CPU13. Par défaut, NIXL opère selon un modèle Pull (lecture RDMA unilatérale directe où le pod Decode lit la mémoire GPU du pod Prefill dès la fin du calcul d'invite), bien que les versions récentes de vLLM introduisent également un mode Push où Prefill écrit directement dans les blocs pré-enregistrés du worker Decode pour superposer l'allocation mémoire au calcul et préparer le transfert couche par couche33.
Pour que l'Agent 2 génère des manifestes DynamoGraphDeployment fonctionnels, les conteneurs main des composants du graphe doivent recevoir une configuration précise par rôle4. Sur la clique decode (type: decode), il est impératif de passer les drapeaux --disaggregation-mode decode, --disable-hybrid-kv-cache-manager, et --kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_consumer","kv_buffer_device":"cuda"}'4. Il est également recommandé d'y adjoindre la politique "kv_load_failure_policy":"fail" afin qu'une défaillance du canal RDMA remonte immédiatement une erreur visible plutôt que de déclencher silencieusement un recalcul complet du prefill sur le nœud de décodage36.
Sur la clique prefill (type: prefill), deux architectures sont possibles selon la pression mémoire exercée par les invites longues4. Pour un transfert GPU-à-GPU pur en mémoire HBM, le conteneur reçoit --disaggregation-mode prefill et --kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_producer","kv_buffer_device":"cuda"}'4. En revanche, pour éviter que les longs contextes ne saturent la mémoire HBM des GPU de Prefill et permettre la réutilisation multi-tours ou l'archivage vers du stockage partagé, l'architecture s'appuie sur Dynamo KVBM (KV Block Manager)4. Dans ce mode illustré par les templates officiels disagg_kvbm de Dynamo, la configuration chaîne deux connecteurs via PdConnector (DynamoConnector pour le déchargement vers la RAM CPU/NVMe et NixlConnector pour l'expédition RDMA vers Decode) tout en définissant la variable d'environnement DYN_KVBM_CPU_CACHE_GB: "100" et une allocation de RAM système de 200Gi à 250Gi sur le pod Prefill4.
Enfin, sur le composant Frontend (type: frontend), l'exploitation de la localité du cache d'attention dans les conversations multi-tours et les boucles d'agents requiert l'activation du routage conscient du KV-Cache via la variable d'environnement DYN_ROUTER_MODE=kv (ou via le bloc spec.features.kvRouter dans un DGDR), tandis que les workers publient leurs événements d'allocation de blocs via --kv-events-config '{"publisher":"zmq","topic":"kv-events","endpoint":"tcp://*:20080","enable_kv_cache_events":true}'12. Par ailleurs, Dynamo v1.5+ intègre un contournement conditionnel de désagrégation (conditional disaggregation bypass) qui achemine directement les requêtes très courtes vers les workers Decode afin d'économiser la latence de transfert réseau lorsque le coût de calcul du prefill est négligeable1.
Gestion des Baux de KV-Cache (NIXL Lease Renewal) et Contraintes Matérielles RDMA
Un problème opérationnel critique de la désagrégation Prefill/Decode survient lors des pics de trafic ou des pannes de pods Decode41. Dans la conception initiale de vLLM, le worker Prefill conservait les blocs de KV-Cache générés pendant un délai fixe unique (VLLM_NIXL_ABORT_REQUEST_TIMEOUT, fixé à 480 secondes par défaut) en attendant que le worker Decode vienne les lire35. Si un pod Decode tombait en panne, le pod Prefill retenait plusieurs gigaoctets de blocs « morts » pendant 8 minutes, provoquant un effondrement de capacité et des erreurs OOM ; inversement, réduire ce délai fixe provoquait l'éviction prématurée des blocs lorsque la requête patientait dans la file d'attente d'un worker Decode surchargé41.
La solution implémentée dans l'architecture NIXL récente repose sur un mécanisme de renouvellement de bail par battements de cœur (Lease Renewal via Heartbeats) superposé au canal de notification NIXL (send_notif / get_new_notifs)41. Lorsqu'un worker Prefill termine son calcul, il épingle les blocs de KV-Cache avec un bail initial court (kv_lease_duration, fixé par défaut à 30 secondes)41. Dès qu'une requête marquée do_remote_prefill=True entre dans l'ordonnanceur du worker Decode (via le hook on_new_request() de NixlConnectorScheduler, avant même que la requête ne soit planifiée pour exécution sur le GPU), le worker Decode démarre le suivi et émet un message groupé de battement de cœur toutes les lease_duration // 6 (), ce qui prolonge l'ensemble des baux actifs auprès de ce worker Prefill de lease_duration * 2/3 ()41. Ainsi, si le worker Decode crashe, les battements de cœur cessent et Prefill récupère la mémoire HBM en quelques secondes après le dernier battement ; si Decode est simplement engorgé, le bail est maintenu indéfiniment jusqu'au transfert effectif et à la notification de fin de lecture41.
Sur le plan matériel (périmètre de l'Agent 1 et de l'Agent 5), NIXL utilise l'appel ibv_reg_mr (InfiniBand verbs memory registration) pour épingler la mémoire GPU et effectuer des transferts zero-copy entre des GPU physiques distincts reliés par NVLink SXM (avec NVSwitch) ou par une fabrique RDMA (InfiniBand ou RoCE)7. Cette architecture impose d'interdire formellement le partage temporel (time-slicing) d'un même GPU physique entre un pod Prefill et un pod Decode : si les deux pods partagent le même espace mémoire physique ou s'exécutent sur un nœud dépourvu de chemin RDMA inter-pods (comme l'interconnexion CPU-GPU NVLink-C2C d'un unique GH200), NIXL ne peut pas initialiser sa couche de transport, le port du canal latéral (VLLM_NIXL_SIDE_CHANNEL_PORT=5600) s'ouvre à vide et aucun bloc KV n'est transféré7. De même, il est impératif de prévenir tout repli silencieux sur TCP (TCP Fallback)40. Si les pilotes OFED/RDMA sont mal exposés dans les conteneurs (absence de securityContext.capabilities.add: ["IPC_LOCK"], absence des périphériques /dev/infiniband/uverbs* ou MTU mal alignée), UCX bascule automatiquement sur TCP40. Or, les mesures en production démontrent qu'un repli TCP dégrade le débit de 19 fois (19x), faisant passer la latence de transfert d'une invite de 47 tokens de 2,3 ms (attendu sous 5 ms en InfiniBand HDR) à plus de 50 ms40. L'Agent 1 doit donc valider systématiquement ibv_devices et ibv_devinfo (en vérifiant Active MTU: 4096 et Active width: 4X) ainsi que nvidia-smi topo -m (présence de liens NV ou PIX, et absence de traversée NUMA SYS ou PHB), tout en injectant UCX_NET_DEVICES=all et NCCL_CUMEM_ENABLE=1 dans les pods workers13.
4. Refonte Mathématique de l'Auto-Tuner (Agent 3) et Stratégie de Cache NVMe Multi-Nœuds
Le modèle mathématique de la Section 5 du plan initial présente quatre failles structurelles qui compromettent la fiabilité de l'Agent 34. Premièrement, le Contrat 1 affiche une valeur unique "estimated_vram_gb_per_replica": 78.4 tout en spécifiant tensor_parallel_size: 4 pour prefill_clique (soit 4 GPU H100 de 80 Go = 320 Go de VRAM totale par réplica) et tensor_parallel_size: 2 pour decode_clique (soit 2 GPU H100 de 80 Go = 160 Go de VRAM totale par réplica). Pour Llama-3.3-70B-Instruct en FP8 (), la mémoire totale des poids est . Cette masse est divisée par  sur chaque GPU de Prefill () et par  sur chaque GPU de Decode (). Le schéma JSON du Contrat 1 doit donc impérativement distinguer la VRAM par GPU et par réplica au sein de chaque clique respective.
Deuxièmement, la formule de la Section 5 applique uniformément  et  à tous les workers, ignorant l'asymétrie fondamentale entre Prefill et Decode4. Comme l'illustrent les templates officiels de Dynamo (disagg.yaml, disagg_kvbm_tp2.yaml), les workers Prefill sont lancés avec le drapeau --enforce-eager, ce qui désactive totalement les graphes CUDA () car la phase de prefill est limitée par le calcul (compute-bound) avec des longueurs de séquences dynamiques4. En revanche, Prefill nécessite un important buffer d'activations transitoires proportionnel à max_num_batched_tokens () ainsi que l'espace nécessaire pour retenir les blocs KV durant le bail NIXL4. À l'inverse, les workers Decode sont limités par la bande passante mémoire (memory-bandwidth-bound), traitent un token par séquence à chaque itération, dépendent crucialement des graphes CUDA ( selon max_num_seqs) et doivent maintenir l'intégralité du fonds de roulement de KV-Cache pour absorber les transferts NIXL concurrents4.
Troisièmement, il existe une contradiction directe entre le Prompt de l'Agent 3, qui exige de calculer le KV-Cache « en tenant compte de GQA/MLA », et la formule de la Section 5 qui ne modélise que GQA. Pour les architectures à Multi-head Latent Attention (MLA) (telles que DeepSeek-V3/R1) ou à Mixture-of-Experts (MoE) (prises en charge nativement par l'AI Configurator de Dynamo via moe_dtype), les clés et valeurs ne sont pas stockées par tête , mais compressées conjointement dans un vecteur latent de rang faible de dimension  (ex. ) accompagné d'une sous-dimension RoPE découplée  (ex. )14. Appliquer la formule GQA à un modèle MLA surestime le KV-Cache d'un facteur 5 à 10, poussant l'Auto-Tuner à refuser le déploiement ou à exiger un  excessif. De plus, contrairement à GQA où le KV-Cache se divise par  (), le vecteur latent compressé  de MLA est répliqué sur chaque rang TP lorsque le parallélisme de tenseur standard est utilisé.
Quatrièmement, le critère de bascule entre architecture agrégée et désagrégée (activé dès que ) est trop naïf pour les charges à contexte court1. Les analyses de performance sur vLLM démontrent qu'à empreinte GPU égale, le mode agrégé obtient un TTFT inférieur ou égal au mode désagrégé lorsque les invites sont courtes, car la désagrégation ajoute une étape de sérialisation et de transfert réseau RDMA sur le chemin critique1. La désagrégation ne doit être déclenchée que si l'interconnexion RDMA/NVLink est validée, que le budget GPU permet d'allouer au moins  GPU, et que la longueur moyenne d'entrée (ISL) dépasse un seuil critique (typiquement ) où l'interférence du prefill dégrade le TPOT/ITL des générations en cours7.
Formules Mathématiques Corrigées pour vram_calculator.py (Agent 3)
L'Agent 3 doit implémenter le système d'équations asymétriques suivant, exprimé par GPU unitaire au sein d'une clique de degré de parallélisme de tenseur  et de parallélisme de pipeline 4. L'empreinte des poids par GPU (), prenant en charge les modèles denses ainsi que les architectures Mixture-of-Experts (MoE) avec types de données distincts (weight_dtype et moe_dtype), s'exprime par14 :

L'empreinte du KV-Cache par token et par GPU () dépend de l'architecture d'attention détectée dans config.json, où  kv_lora_rank et  qk_rope_head_dim pour MLA :

À partir de ces composantes, le dimensionnement asymétrique par GPU sépare strictement la clique Prefill () et la clique Decode ()4. Pour un GPU de la clique Prefill opérant en mode --enforce-eager (), mais devant retenir les blocs pendant le bail NIXL  pour  requêtes en vol tout en allouant le buffer d'activation , la mémoire requise par GPU est4 :

Inversement, pour un GPU de la clique Decode opérant avec les graphes CUDA activés () et une concurrence élevée  max_num_seqs pouvant atteindre 256 séquences simultanées sur une longueur de contexte active moyenne  bornée par , la mémoire requise par GPU s'établit à4 :

Dans ces deux équations,  désigne gpu_memory_utilization (typiquement , ou abaissé à  dans certains profils légers de test KVBM) et  représente les tampons RDMA enregistrés via ibv_reg_mr4.

Résolution du Piège hostPath NVMe sur Cluster Multi-Nœuds (Agent 1 & Agent 3)
Dans le plan initial, l'Agent 4 lance un Job Kubernetes (loader/model_warmer.py) qui télécharge les poids du modèle vers /mnt/nvme/model-cache, puis déploie le DynamoGraphDeployment qui monte ce chemin local (hostPath) dans les pods workers. Cette conception comporte un défaut critique dès que le cluster comporte plus d'un nœud physique GPU : un Job Kubernetes standard n'exécute son pod que sur un seul nœud du cluster. Lorsque KAI Scheduler et Grove déploient les pods Prefill et Decode sur les autres nœuds GPU du cluster, leur répertoire local /mnt/nvme/model-cache est vide, ce qui force chaque worker vLLM à retélécharger des dizaines de gigaoctets depuis Hugging Face au démarrage, provoquant des délais d'initialisation de plusieurs minutes, une saturation de la bande passante sortante ou un blocage HTTP 429 de Hugging Face.
Deux architectures permettent de corriger ce problème selon la topologie de stockage retenue par l'Agent 1 et l'Agent 38. Si l'infrastructure s'appuie sur des disques NVMe locaux montés en hostPath, l'Agent 4 doit interroger l'API Kubernetes au moment du pré-chargement pour lister tous les nœuds portant le label nvidia.com/gpu.present=true () et lancer un Job en mode completionMode: Indexed avec completions: N_nodes, parallelism: N_nodes et une règle de podAntiAffinity stricte (kubernetes.io/hostname) garantissant qu'exactement un pod model_warmer.py s'exécute sur chaque nœud GPU pour peupler son disque NVMe local /mnt/nvme/model-cache avant l'instanciation du DGD8. Alternativement, comme le montrent les utilitaires officiels de Dynamo (deploy/utils/manifests/pvc.yaml), la plateforme peut exploiter un stockage partagé NVMe-oF ou NFS via un PersistentVolumeClaim (PVC) en mode ReadWriteMany (RWX) monté dans les composants via spec.components[*].volumeMounts15. En complément, Dynamo v1.5+ introduit l'opérateur autonome Dynamo Snapshot (DynamoCheckpoint CRD en v1alpha1) avec les politiques de démarrage Immediate ou WaitForCheckpoint, permettant de restaurer directement l'état mémoire d'un worker initialisé plutôt que de relire et désérialiser les fichiers .safetensors à chaque création de pod1.
5. Autoscaling (Agent 5) et Synchronisation Passerelle LiteLLM (Agent 4)
Autoscaling via DynamoGraphDeploymentScalingAdapter (DGDSA) et Arbitrage avec Dynamo Planner
Le prompt de l'Agent 5 dans le plan initial lui demande de créer des ScaledObject KEDA scalant directement les cliques PrefillWorker et DecodeWorker. Or, dans l'architecture de l'opérateur NVIDIA Dynamo, le champ autoscaling interne au DGD est officiellement déprécié et ignoré5. Le modèle de propriété des réplicas (Replica Ownership Model) de Dynamo fonctionne exactement comme la relation entre un Deployment et un ReplicaSet dans Kubernetes : lorsque le DynamoGraphDeployment est appliqué, l'opérateur Dynamo crée automatiquement une ressource DynamoGraphDeploymentScalingAdapter (abrégée dgdsa, API nvidia.com/v1beta1) pour chaque composant du graphe5. Le nom de chaque adaptateur suit la convention stricte <nom-du-dgd>-<nom-du-composant-en-minuscules>, telle que llama-3-3-70b-prefill et llama-3-3-70b-decode5.
Tant que scalingAdapter est activé (comportement par défaut), le DGDSA détient la source de vérité exclusive sur le champ replicas et expose la sous-ressource standard Kubernetes /scale5. Toute modification directe de replicas sur le DGD ou le PodCliqueSet est écrasée par le contrôleur d'adaptateur5. Par conséquent, l'Agent 5 doit configurer le bloc scaleTargetRef de ses ScaledObject KEDA pour cibler explicitement apiVersion: nvidia.com/v1beta1 et kind: DynamoGraphDeploymentScalingAdapter5.
En outre, l'Agent 5 doit prévenir tout conflit de contrôle entre KEDA et le Dynamo Planner5. Dynamo inclut en effet un composant natif d'autoscaling (type: planner), qui ajuste dynamiquement les réplicas Prefill et Decode via les DGDSA selon quatre cibles d'optimisation (débit, latence, charge, ou budget GPU borné par max_gpu_budget et min_endpoint) en interrogeant prometheusEndpoint5. Si l'Agent 5 active KEDA sur un déploiement, le composant planner ne doit pas être activé simultanément sur les mêmes DGDSA, sous peine de provoquer des oscillations (thrashing) entre les deux contrôleurs5. KEDA est privilégié pour le scaling basé sur des règles PromQL personnalisées et le scale-to-zero, tandis que le Planner est utilisé lorsque l'ajustement conjoint du ratio P/D par modélisation SLA est souhaité5. Au niveau des métriques PromQL, au-delà des compteurs internes de vLLM (vllm:num_requests_waiting pour la file d'attente et vllm:gpu_cache_usage_perc pour la saturation du KV-Cache), le Frontend Dynamo exporte sur son endpoint /metrics l'histogramme dynamo_frontend_time_to_first_token_seconds_bucket labellisé par dynamo_namespace="<k8s_namespace>-<dgd_name>"5. Combiner vllm:num_requests_waiting avec le quantile p95 ou p99 du TTFT (histogram_quantile(0.95, sum(rate(dynamo_frontend_time_to_first_token_seconds_bucket[2m])) by (le, dynamo_namespace))) permet à KEDA de réagir dès que la latence d'amorçage dépasse l'objectif SLA de 300 ms ou 500 ms5.
Synchronisation Dynamique LiteLLM : Mode Base de Données, Concurrence Multi-Réplicas et Drain Sans Erreur 502
L'architecture de l'Agent 4 comporte trois angles morts critiques concernant le fonctionnement interne de LiteLLM Proxy en production3. Tout d'abord, les routes HTTP POST /model/new, POST /model/update et POST /model/delete — en notant que la suppression d'un modèle en base de données s'effectue obligatoirement via une requête POST /model/delete avec un corps JSON {"id": "<model_info.id>"} et non via un verbe HTTP DELETE — échouent systématiquement si la variable d'environnement STORE_MODEL_IN_DB="True" (ou general_settings.store_model_in_db: true dans config.yaml) n'est pas activée3. De plus, LiteLLM exige la présence d'une base PostgreSQL (DATABASE_URL) et d'une clé de chiffrement immuable LITELLM_SALT_KEY pour chiffrer les identifiants des fournisseurs stockés en base, les migrations de schéma étant désormais appliquées automatiquement au démarrage sans nécessiter l'ancien drapeau USE_PRISMA_MIGRATE3. Il est également impératif de ne pas déclarer les modèles dynamiques Dynamo dans le fichier statique config.yaml : LiteLLM considère les modèles issus de config.yaml comme appartenant au fichier et interdit leur modification ou suppression via l'API /model/delete3.
Ensuite, une condition de course (race condition) apparaît entre les réplicas LiteLLM lors d'une opération DELETE en haute disponibilité6. Lorsque LiteLLM tourne sur plusieurs réplicas (par exemple 3 pods derrière litellm-service) et qu'un appel POST /model/new ou POST /model/delete touche le Service Kubernetes, seul le processus du pod LiteLLM qui reçoit la requête HTTP rafraîchit immédiatement son routeur en mémoire6. Les autres réplicas LiteLLM ne découvrent l'ajout ou la suppression du modèle dans PostgreSQL qu'à l'expiration de leur intervalle périodique de rechargement fixé par PROXY_CONFIG_RELOAD_INTERVAL_SECONDS6. Si l'Agent 4 attend seulement 15 secondes après POST /model/delete avant de détruire le DynamoGraphDeployment alors que PROXY_CONFIG_RELOAD_INTERVAL_SECONDS possède une valeur supérieure, les autres pods LiteLLM continueront d'envoyer des requêtes vers des pods Dynamo en cours d'extinction, générant des erreurs HTTP 502 lors du test E2E de la Vague 56. Pour éliminer ce défaut, l'Agent 4 doit configurer PROXY_CONFIG_RELOAD_INTERVAL_SECONDS=10 sur tous les pods LiteLLM et attendre dans le gestionnaire de suppression un délai minimal supérieur à cet intervalle tout en vérifiant via Prometheus que vllm:num_requests_running == 0 avant de supprimer le CRD DynamoGraphDeployment6.
Enfin, la gestion des connexions PostgreSQL et Redis doit être dimensionnée pour la charge11. Chaque instance LiteLLM ouvre  vers PostgreSQL, soit 120 connexions pour 3 pods à 4 workers11. L'Agent 4 doit activer allow_requests_on_db_unavailable: True dans config.yaml (afin que /health/readiness retourne toujours 200 OK même en cas de micro-coupure PostgreSQL), configurer scheduled_job_stagger (window_seconds: 600 avec POD_NAME exposé en variable d'environnement pour désynchroniser les tâches cron de calcul des coûts entre réplicas), et activer le tampon de transactions Redis pour absorber l'écriture des journaux de dépenses au-delà de 1 000 requêtes par seconde11.
Durcissement du Tunnel Cloudflare face au Timeout Idle de 100 Secondes (Agent 1)
Le prompt de l'Agent 1 configure proxy-read-timeout: 3600 et proxy-buffering: "off" sur le contrôleur ingress-nginx et s'appuie sur external-dns pour créer les enregistrements CNAME pointant vers <tunnel-id>.cfargotunnel.com via l'annotation external-dns.alpha.kubernetes.io/target50. Cependant, le réseau Edge de Cloudflare applique un délai d'inactivité strict de 100 secondes entre deux paquets HTTP successifs provenant de l'origine, au-delà duquel Cloudflare coupe la connexion avec une erreur HTTP 524 A Timeout Occurred quelle que soit la configuration d'Ingress-NGINX. Si un modèle de raisonnement ou une requête à contexte de 128k tokens met plus de 100 secondes en phase de Prefill avant d'émettre le premier token (TTFT > 100s) ou lors d'un appel d'outil non-streamé, Cloudflare interrompt la requête. L'Agent 1 et l'Agent 4 doivent donc activer les battements de cœur TCP/QUIC sur cloudflared (--grace-period 30s, protocole QUIC) et activer le streaming de commentaires SSE périodiques (: keep-alive\n\n) ou le transfert par blocs (chunked transfer encoding) dès l'acceptation de la requête dans la passerelle.
6. Contrats d'Interface Corrigés (Handshakes v2.0)
Afin que les Agents 2, 3, 4 et 5 puissent développer leurs modules en parallèle lors des Vagues 2 et 3 sans rupture d'intégration, l'Agent 0 doit figer les quatre contrats corrigés suivants dans contracts/2.
Contrat 1 Corrigé : Sortie de l'Auto-Tuner (Agent 3 -> Agent 2 & Agent 4)
Le schéma contracts/model_spec.schema.json est enrichi pour refléter l'asymétrie VRAM par GPU, le support GQA/MLA/MoE, la configuration du bail NIXL et les paramètres de déchargement KVBM4 :

JSON
{
  "model_id": "meta-llama/Llama-3.3-70B-Instruct",
  "deployment_name": "llama-3-3-70b",
  "namespace": "dynamo-workloads",
  "serving_mode": "disaggregated",
  "attention_type": "gqa",
  "is_moe": false,
  "quantization": "fp8",
  "kv_cache_dtype": "fp8",
  "max_model_len": 32768,
  "gpu_memory_utilization": 0.90,
  "dynamo_runtime_image": "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.5.1",
  "prefill_clique": {
    "replicas": 2,
    "min_available": 1,
    "tensor_parallel_size": 4,
    "pipeline_parallel_size": 1,
    "gpu_sku": "NVIDIA-H100-80GB",
    "estimated_vram_gb_per_gpu": 72.0,
    "estimated_vram_gb_per_replica": 288.0,
    "max_num_batched_tokens": 16384,
    "enforce_eager": true,
    "kvbm_cpu_cache_gb": 100,
    "system_memory_request_gi": 200,
    "system_memory_limit_gi": 250
  },
  "decode_clique": {
    "replicas": 4,
    "min_available": 2,
    "tensor_parallel_size": 2,
    "pipeline_parallel_size": 1,
    "gpu_sku": "NVIDIA-H100-80GB",
    "estimated_vram_gb_per_gpu": 72.0,
    "estimated_vram_gb_per_replica": 144.0,
    "max_num_seqs": 256,
    "enforce_eager": false,
    "system_memory_request_gi": 64,
    "system_memory_limit_gi": 96
  },
  "kv_cache_transfer": {
    "engine": "nixl",
    "backend": "UCX",
    "kv_lease_duration_s": 30,
    "kv_load_failure_policy": "fail",
    "enable_kvbm": true,
    "router_mode": "kv",
    "topology_enforcement": "preferred"
  }
}

Contrat 2 Corrigé : Template DynamoGraphDeployment en nvidia.com/v1beta1 (Agent 2 -> Agent 4)
Le fichier dynamo/templates/dgd-disaggregated.yaml.j2 adopte la spécification officielle nvidia.com/v1beta1 avec spec.backendFramework, spec.components, les connecteurs NixlConnector / PdConnector, le routage DYN_ROUTER_MODE=kv et l'intégration Grove2 :

YAML
apiVersion: nvidia.com/v1beta1
kind: DynamoGraphDeployment
metadata:
  name: "{{ deployment_name }}"
  namespace: "{{ namespace }}"
  labels:
    app.kubernetes.io/managed-by: dynamo-control-plane
  annotations:
    nvidia.com/grove-update-strategy: RollingRecreate
spec:
  backendFramework: vllm
  components:
    - name: Frontend
      type: frontend
      replicas: 2
      podTemplate:
        spec:
          containers:
            - name: main
              image: "{{ dynamo_runtime_image }}"
              env:
                - name: DYN_ROUTER_MODE
                  value: "{{ kv_cache_transfer.router_mode }}"
              envFrom:
                - secretRef:
                    name: hf-token-secret
              ports:
                - name: http
                  containerPort: 8000

    - name: prefill
      type: prefill
      replicas: {{ prefill_clique.replicas }}
      providerOverride:
        apiVersion: grove.io/v1alpha1
        value:
          spec:
            minAvailable: {{ prefill_clique.min_available }}
          rollingUpdate:
            maxUnavailable: 1
      podTemplate:
        spec:
          volumes:
            - name: model-cache
              hostPath:
                path: /mnt/nvme/model-cache
                type: DirectoryOrCreate
            - name: dshm
              emptyDir:
                medium: Memory
                sizeLimit: 32Gi
          containers:
            - name: main
              image: "{{ dynamo_runtime_image }}"
              workingDir: /workspace/examples/backends/vllm
              command:
                - python3
                - -m
                - dynamo.vllm
              args:
                - --model
                - "{{ model_id }}"
                - --disaggregation-mode
                - prefill
                - --disable-hybrid-kv-cache-manager
                - --tensor-parallel-size
                - "{{ prefill_clique.tensor_parallel_size }}"
                - --quantization
                - "{{ quantization }}"
                - --kv-cache-dtype
                - "{{ kv_cache_dtype }}"
                - --max-model-len
                - "{{ max_model_len }}"
                - --max-num-batched-tokens
                - "{{ prefill_clique.max_num_batched_tokens }}"
                - --gpu-memory-utilization
                - "{{ gpu_memory_utilization }}"
                {% if prefill_clique.enforce_eager %}
                - --enforce-eager
                {% endif %}
                - --kv-events-config
                - '{"publisher":"zmq","topic":"kv-events","endpoint":"tcp://*:20080","enable_kv_cache_events":true}'
                - --kv-transfer-config
                {% if kv_cache_transfer.enable_kvbm %}
                - '{"kv_connector":"PdConnector","kv_role":"kv_both","kv_connector_extra_config":{"connectors":[{"kv_connector":"DynamoConnector","kv_connector_module_path":"kvbm.vllm_integration.connector","kv_role":"kv_both"},{"kv_connector":"NixlConnector","kv_role":"kv_producer","kv_buffer_device":"cuda"}]},"kv_connector_module_path":"kvbm.vllm_integration.connector"}'
                {% else %}
                - '{"kv_connector":"NixlConnector","kv_role":"kv_producer","kv_buffer_device":"cuda","kv_connector_extra_config":{"backends":["{{ kv_cache_transfer.backend }}"]}}'
                {% endif %}
              env:
                - name: HF_HOME
                  value: /mnt/nvme/model-cache
                - name: DYN_KVBM_CPU_CACHE_GB
                  value: "{{ prefill_clique.kvbm_cpu_cache_gb }}"
                - name: UCX_NET_DEVICES
                  value: "all"
                - name: NCCL_CUMEM_ENABLE
                  value: "1"
                - name: VLLM_NIXL_SIDE_CHANNEL_PORT
                  value: "5600"
              envFrom:
                - secretRef:
                    name: hf-token-secret
              securityContext:
                capabilities:
                  add:
                    - IPC_LOCK
              volumeMounts:
                - name: model-cache
                  mountPath: /mnt/nvme/model-cache
                - name: dshm
                  mountPath: /dev/shm
              resources:
                requests:
                  memory: "{{ prefill_clique.system_memory_request_gi }}Gi"
                  nvidia.com/gpu: "{{ prefill_clique.tensor_parallel_size }}"
                limits:
                  memory: "{{ prefill_clique.system_memory_limit_gi }}Gi"
                  nvidia.com/gpu: "{{ prefill_clique.tensor_parallel_size }}"

    - name: decode
      type: decode
      replicas: {{ decode_clique.replicas }}
      providerOverride:
        apiVersion: grove.io/v1alpha1
        value:
          spec:
            minAvailable: {{ decode_clique.min_available }}
          rollingUpdate:
            maxUnavailable: 1
      podTemplate:
        spec:
          volumes:
            - name: model-cache
              hostPath:
                path: /mnt/nvme/model-cache
                type: DirectoryOrCreate
            - name: dshm
              emptyDir:
                medium: Memory
                sizeLimit: 32Gi
          containers:
            - name: main
              image: "{{ dynamo_runtime_image }}"
              workingDir: /workspace/examples/backends/vllm
              command:
                - python3
                - -m
                - dynamo.vllm
              args:
                - --model
                - "{{ model_id }}"
                - --disaggregation-mode
                - decode
                - --disable-hybrid-kv-cache-manager
                - --tensor-parallel-size
                - "{{ decode_clique.tensor_parallel_size }}"
                - --quantization
                - "{{ quantization }}"
                - --kv-cache-dtype
                - "{{ kv_cache_dtype }}"
                - --max-model-len
                - "{{ max_model_len }}"
                - --max-num-seqs
                - "{{ decode_clique.max_num_seqs }}"
                - --gpu-memory-utilization
                - "{{ gpu_memory_utilization }}"
                - --kv-transfer-config
                - '{"kv_connector":"NixlConnector","kv_role":"kv_consumer","kv_buffer_device":"cuda","kv_load_failure_policy":"{{ kv_cache_transfer.kv_load_failure_policy }}","kv_connector_extra_config":{"backends":["{{ kv_cache_transfer.backend }}"]}}'
              env:
                - name: HF_HOME
                  value: /mnt/nvme/model-cache
                - name: UCX_NET_DEVICES
                  value: "all"
                - name: NCCL_CUMEM_ENABLE
                  value: "1"
                - name: VLLM_NIXL_SIDE_CHANNEL_PORT
                  value: "5601"
              envFrom:
                - secretRef:
                    name: hf-token-secret
              securityContext:
                capabilities:
                  add:
                    - IPC_LOCK
              volumeMounts:
                - name: model-cache
                  mountPath: /mnt/nvme/model-cache
                - name: dshm
                  mountPath: /dev/shm
              resources:
                requests:
                  memory: "{{ decode_clique.system_memory_request_gi }}Gi"
                  nvidia.com/gpu: "{{ decode_clique.tensor_parallel_size }}"
                limits:
                  memory: "{{ decode_clique.system_memory_limit_gi }}Gi"
                  nvidia.com/gpu: "{{ decode_clique.tensor_parallel_size }}"

Contrat 3 Corrigé : Configuration de Production et Synchronisation LiteLLM (Agent 4)
Le fichier gateway/config.yaml de LiteLLM doit activer la persistance en base de données, la tolérance aux pannes DB, le décalage des tâches planifiées et l'intervalle rapide de synchronisation inter-pods11 :

YAML
general_settings:
  store_model_in_db: true
  allow_requests_on_db_unavailable: true
  proxy_batch_write_at: 10

router_settings:
  routing_strategy: simple-shuffle
  redis_host: redis-service.ai-gateway.svc.cluster.local
  redis_port: 6379

litellm_settings:
  cache: true
  cache_params:
    type: redis
    namespace: "litellm.caching.caching"

scheduled_job_stagger:
  window_seconds: 600

En complément, le Deployment Kubernetes de LiteLLM (gateway/litellm-deployment.yaml) doit injecter les variables d'environnement suivantes : STORE_MODEL_IN_DB="True", PROXY_CONFIG_RELOAD_INTERVAL_SECONDS="10", DATABASE_URL, LITELLM_MASTER_KEY, LITELLM_SALT_KEY et POD_NAME (via fieldRef.fieldPath: metadata.name)11. Lors du retrait d'un modèle, l'Agent 4 exécute la requête POST suivante et applique le protocole de drain associé3 :

JSON
{
  "endpoint": "POST <http://litellm-service.ai-gateway.svc.cluster.local:4000/model/delete>",
  "headers": {
    "Authorization": "Bearer <LITELLM_MASTER_KEY>",
    "Content-Type": "application/json"
  },
  "body": {
    "id": "dgd-llama-3-3-70b"
  },
  "post_delete_drain_protocol": {
    "min_wait_seconds_for_replica_sync": 15,
    "prometheus_Active_Requests_Check": "sum(vllm:num_requests_running{namespace='dynamo-workloads', model_name='meta-llama/Llama-3.3-70B-Instruct'}) == 0",
    "max_drain_timeout_seconds": 120
  }
}

Contrat 4 (Nouveau) : Autoscaling KEDA via DynamoGraphDeploymentScalingAdapter (Agent 5)
Le fichier monitoring/keda-scaledobjects.yaml.j2 cible les ressources DynamoGraphDeploymentScalingAdapter générées automatiquement par l'opérateur Dynamo pour chaque composant (<deployment_name>-prefill et <deployment_name>-decode)5 :

YAML
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: "{{ deployment_name }}-prefill-scaler"
  namespace: "{{ namespace }}"
spec:
  scaleTargetRef:
    apiVersion: nvidia.com/v1beta1
    kind: DynamoGraphDeploymentScalingAdapter
    name: "{{ deployment_name }}-prefill"
  pollingInterval: 15
  cooldownPeriod: 120
  minReplicaCount: {{ prefill_clique.replicas }}
  maxReplicaCount: {{ prefill_clique.replicas * 4 }}
  triggers:
    - type: prometheus
      metadata:
        serverAddress: <http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090>
        metricName: dynamo_ttft_p95_seconds
        threshold: "0.35"
        query: |
          histogram_quantile(0.95, sum(rate(dynamo_frontend_time_to_first_token_seconds_bucket{dynamo_namespace="{{ namespace }}-{{ deployment_name }}"}[2m])) by (le))
    - type: prometheus
      metadata:
        serverAddress: <http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090>
        metricName: vllm_requests_waiting_prefill
        threshold: "5"
        query: |
          sum(vllm:num_requests_waiting{namespace="{{ namespace }}", dynamo_component="prefill", model_name="{{ model_id }}"})
---

apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: "{{ deployment_name }}-decode-scaler"
  namespace: "{{ namespace }}"
spec:
  scaleTargetRef:
    apiVersion: nvidia.com/v1beta1
    kind: DynamoGraphDeploymentScalingAdapter
    name: "{{ deployment_name }}-decode"
  pollingInterval: 15
  cooldownPeriod: 180
  minReplicaCount: {{ decode_clique.replicas }}
  maxReplicaCount: {{ decode_clique.replicas * 4 }}
  triggers:
    - type: prometheus
      metadata:
        serverAddress: <http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090>
        metricName: vllm_gpu_cache_usage_perc
        threshold: "0.80"
        query: |
          avg(vllm:gpu_cache_usage_perc{namespace="{{ namespace }}", dynamo_component="decode", model_name="{{ model_id }}"})

1. Graphe d'Exécution (DAG) Mis à Jour et Prompts Système Révisés (Agents 0 à 5)
Le graphe de dépendances en 5 vagues est conservé dans sa philosophie d'exécution parallèle, mais ses portes de validation (Quality Gates) et les tâches inter-agents sont mises à jour pour intégrer le contrat KEDA/DGDSA, la validation RDMA/NIXL, le pré-chargement NVMe distribué et le délai de synchronisation multi-réplicas de LiteLLM5.

Tableau Récapitulatif du DAG Corrigé en 5 Vagues

Vague
Agents Actifs
Tâches Exécutées en Parallèle (Corrigées)
Critère de Validation Strict (Quality Gate)
Vague 1 : Fondations, Réseau & Contrats v2.0
Agent 0, Agent 1
Agent 0 : Publie les 4 schémas dans contracts/ (model_spec.schema.json asymétrique, dynamo_crd.schema.json en nvidia.com/v1beta1, keda_dgdsa.schema.json et control_plane_openapi.yaml)2.

Agent 1 : Code bootstrap_inference_cluster.sh (K3s 1.30+, GPU Operator, Network Operator RDMA, labellisation nvidia.com/gpu.present=true), Ingress-NGINX, Cloudflare Tunnel (QUIC) et external-dns (<tunnel-id>.cfargotunnel.com)8.
shellcheck + kubeconform valides ; validation JSON Schema des 4 contrats ; vérification des annotations external-dns et proxy-buffering: "off"50.
Vague 2 : Développement Cœur (Parallèle)
Agent 2, Agent 3, Agent 4
Agent 2 : Rédige les chartes dynamo-crds et dynamo-platform (avec NATS JetStream, etcd, Grove et KAI activés) et les templates Jinja2 v1beta1 (dgd-disaggregated.yaml.j2, dgd-aggregated.yaml.j2, dgdr-intent.yaml.j2) incluant NixlConnector / PdConnector4.

Agent 3 : Implémente hf_inspector.py, vram_calculator.py (équations asymétriques Prefill/Decode, support GQA/MLA/MoE) et le Job indexé multi-nœuds model_warmer.py (hf_transfer)4.

Agent 4 : Développe l'API FastAPI, le réconciliateur K8s (kubernetes_asyncio), le déploiement LiteLLM (STORE_MODEL_IN_DB=True, LITELLM_SALT_KEY, PROXY_CONFIG_RELOAD_INTERVAL_SECONDS=10, Postgres, Redis) et le connecteur /model/new et /model/delete3.
Tests unitaires isolés : l'Agent 3 valide les calculs VRAM sur Llama-3.3-70B (GQA) et DeepSeek-V3 (MLA/MoE) ; l'Agent 2 valide le rendu YAML nvidia.com/v1beta1 ; l'Agent 4 valide le cycle complet avec mocks K8s/LiteLLM2.
Vague 3 : Autoscaling DGDSA & Intégration
Agent 0, Agent 4, Agent 5
Agent 5 : Crée les templates ScaledObject KEDA ciblant kind: DynamoGraphDeploymentScalingAdapter (nvidia.com/v1beta1) sur dynamo_frontend_time_to_first_token_seconds_bucket, vllm:num_requests_waiting et vllm:gpu_cache_usage_perc5.

Agent 0 & Agent 4 : Intègrent autotuner/, dynamo/templates/ et monitoring/keda-scaledobjects.yaml.j2 dans le pipeline d'orchestration FastAPI5.
Construction réussie des images Docker (control-plane et model-warmer) et validation du pipeline complet sur cluster éphémère avec le backend simulé (mocker) de Dynamo1.
Vague 4 : Déploiement Cluster, RDMA & DNS
Agent 1, Agent 2, Agent 4
Exécution du bootstrap matériel : installation GPU Operator (ClusterPolicy ready), validation RDMA (ibv_devinfo, nvidia-smi topo -m), installation dynamo-platform (Operator + NATS + etcd + Grove + KAI), déploiement LiteLLM + Postgres + Redis, Cloudflare Tunnel et Control Plane8.
Script officiel pre-deployment-check.sh 100 % vert ; ibv_devinfo affiche Active MTU: 4096 ; 100 % des pods Running ; <https://llm>.<domaine>/health/readiness et <https://control>.<domaine>/health retournent 200 OK8.
Vague 5 : Recette E2E, NIXL & Chaos Testing
Agent 5, Agent 0
Agent 5 : Déploie un modèle via POST /api/v1/models/deploy, vérifie le Job indexé NVMe sur tous les nœuds, l'instanciation du DGD v1beta1 et des DGDSA, vérifie que la latence de transfert NIXL est < 5 ms (zéro repli TCP), lance un tir de charge aiperf en streaming SSE et teste DELETE sous charge7.
Zéro erreur HTTP 502/524 lors de l'ajout et de la suppression à chaud ; métriques NIXL non nulles (vllm:nixl_post_time_seconds < 5ms) ; scaling KEDA des DGDSA validé7.

Spécifications des Prompts Système Corrigés pour l'Orchestrateur Multi-Agents
Pour garantir une exécution déterministe sans conflit de fusion ni régression vers les schémas dépréciés, les instructions système injectées dans l'orchestrateur multi-agents doivent être mises à jour selon les directives suivantes2.
Spécification Système — Agent 0 (Lead Architect & Orchestrator)
Rôle assigné : Architecte Principal et Orchestrateur du Monorepo. Le périmètre d'écriture exclusif de l'agent couvre /, contracts/ et .github/. L'agent verrouille quatre contrats JSON/OpenAPI avant toute écriture des autres agents : (1) contracts/model_spec.schema.json intégrant le calcul VRAM asymétrique par GPU et par clique (prefill_clique vs decode_clique), le support GQA/MLA/MoE et la configuration NIXL/KVBM ; (2) contracts/dynamo_crd.schema.json strictement aligné sur l'API nvidia.com/v1beta1 (spec.backendFramework, spec.components, podTemplate.spec.containers[name=main], providerOverride pour grove.io/v1alpha1) ; (3) contracts/keda_dgdsa.schema.json ciblant kind: DynamoGraphDeploymentScalingAdapter ; et (4) contracts/control_plane_openapi.yaml2. L'agent rejette toute Pull Request contenant des ressources DynamoGraphDeployment avec l'ancienne structure spec.services ou un ciblage KEDA direct sur le DGD2.
Spécification Système — Agent 1 (Infra, K8s, RDMA & Cloudflare Network)
Rôle assigné : Ingénieur Infrastructure Bare-Metal, Kubernetes et Réseau. Le périmètre d'écriture exclusif de l'agent est limité à infra/ et scripts/. L'agent maintient bootstrap_inference_cluster.sh pour provisionner un cluster K3s (version ), le NVIDIA GPU Operator et le NVIDIA Network Operator (RDMA/RoCE/InfiniBand)8. Le script doit obligatoirement vérifier que la ressource ClusterPolicy atteint l'état ready, appliquer le label nvidia.com/gpu.present=true sur tous les nœuds GPU, valider la topologie matérielle via nvidia-smi topo -m (liens NVLink/PIX) et ibv_devinfo (Active MTU: 4096, Active width: 4X), et exécuter le script de vérification amont de Dynamo (pre-deployment-check.sh)8. Sur le plan réseau, aucun port entrant n'est ouvert sur l'hôte : tout le trafic passe par cloudflared (2 réplicas en mode QUIC) vers ingress-nginx-controller en ClusterIP. L'agent configure external-dns avec les annotations external-dns.alpha.kubernetes.io/target: <tunnel-id>.cfargotunnel.com et external-dns.alpha.kubernetes.io/cloudflare-proxied: "true", active proxy-read-timeout: 3600, proxy-send-timeout: 3600 et proxy-buffering: "off" sur les Ingress, et configure l'envoi de battements de cœur SSE pour prévenir la coupure HTTP 524 de 100 secondes de Cloudflare50.
Spécification Système — Agent 2 (NVIDIA Dynamo & Disaggregated Serving Specialist)
Rôle assigné : Spécialiste NVIDIA Dynamo, Grove et KAI Scheduler. Le périmètre d'écriture exclusif de l'agent est limité à dynamo/. L'agent configure le déploiement Helm de dynamo-crds et dynamo-platform en activant explicitement l'opérateur Dynamo, NATS avec JetStream, etcd, ainsi que les sous-chartes intégrées Grove et KAI Scheduler (install=true, enabled=true)8. L'agent produit les templates Jinja2 (dgd-disaggregated.yaml.j2, dgd-aggregated.yaml.j2 et dgdr-intent.yaml.j2) strictement en version apiVersion: nvidia.com/v1beta12. Chaque composant dans spec.components doit définir un conteneur nommé main exécutant python3 -m dynamo.vllm, monter /mnt/nvme/model-cache (HF_HOME=/mnt/nvme/model-cache) et un volume /dev/shm en RAM (sizeLimit: 32Gi), et accorder la capacité IPC_LOCK pour l'enregistrement mémoire RDMA (ibv_reg_mr)2. En mode désagrégé, le composant Frontend reçoit DYN_ROUTER_MODE=kv ; le composant prefill (type: prefill) reçoit --disaggregation-mode prefill, --disable-hybrid-kv-cache-manager, --enforce-eager, --kv-events-config (ZMQ port 20080) et --kv-transfer-config configurant PdConnector (DynamoConnector + NixlConnector en kv_producer) avec DYN_KVBM_CPU_CACHE_GB ; le composant decode (type: decode) reçoit --disaggregation-mode decode, --disable-hybrid-kv-cache-manager et --kv-transfer-config configurant NixlConnector en kv_consumer avec "kv_load_failure_policy":"fail"4. L'agent configure providerOverride (grove.io/v1alpha1) en garantissant toujours minAvailable < replicas et utilise la stratégie RollingRecreate par défaut pour éviter les blocages de mise à jour documentés dans le bogue Grove #8732.
Spécification Système — Agent 3 (Auto-Tuner Mathématique & Model Loader Multi-Nœuds)
Rôle assigné : Ingénieur Dimensionnement Mathématique et Pré-chargement NVMe. Le périmètre d'écriture exclusif de l'agent est limité à control-plane/autotuner/ et loader/. L'agent implémente l'inspection sans téléchargement de config.json et model.safetensors.index.json depuis Hugging Face ou S3, détecte automatiquement l'architecture d'attention (GQA/MHA vs MLA avec kv_lora_rank et qk_rope_head_dim) et la présence de couches MoE14. Dans vram_calculator.py, l'agent calcule séparément l'empreinte VRAM par GPU pour la clique Prefill ( en mode --enforce-eager, sans graphes CUDA, incluant les buffers d'activation pour max_num_batched_tokens et la rétention de KV-Cache durant le bail NIXL de 30s) et pour la clique Decode ( incluant les graphes CUDA pour max_num_seqs et le fonds de roulement KV-Cache)4. Pour GQA, l'agent vérifie la contrainte stricte num_key_value_heads % tp == 0. L'agent active le mode disaggregated uniquement si l'interconnexion RDMA/NVLink est présente, qu'au moins  GPU sont disponibles et que le profil de charge implique une longueur d'invite significative () ou un SLA strict7. Dans loader/model_warmer.py, l'agent conçoit un Job Kubernetes Indexé (completionMode: Indexed) doté d'une règle de podAntiAffinity par kubernetes.io/hostname afin que le téléchargement parallèle (hf_transfer=1) peuple simultanément le disque NVMe local /mnt/nvme/model-cache de chaque nœud GPU du cluster avant le démarrage des workers8.
Spécification Système — Agent 4 (Control Plane API & Synchronisation Haute Disponibilité LiteLLM)
Rôle assigné : Ingénieur Backend Control Plane et Passerelle LiteLLM. Le périmètre d'écriture exclusif de l'agent est limité à control-plane/api/ et gateway/. Dans gateway/, l'agent déploie LiteLLM Proxy en haute disponibilité avec PostgreSQL et Redis en activant obligatoirement STORE_MODEL_IN_DB="True", LITELLM_SALT_KEY, PROXY_CONFIG_RELOAD_INTERVAL_SECONDS="10", allow_requests_on_db_unavailable: true et scheduled_job_stagger (window_seconds: 600 indexé sur POD_NAME), sans déclarer aucun modèle dynamique dans config.yaml3. Dans control-plane/api/, l'agent développe le serveur FastAPI asynchrone gérant l'intégralité du cycle de vie des modèles3. Lors d'un appel à POST /api/v1/models/deploy, le service invoque le module autotuner (Agent 3), lance le Job indexé de pré-chargement NVMe sur tous les nœuds GPU et attend sa complétion, puis génère et applique le manifeste DynamoGraphDeployment (nvidia.com/v1beta1, Agent 2) ainsi que les ScaledObject KEDA (Agent 5) via kubernetes_asyncio2. En parallèle, une tâche de fond (EndpointSlice Watcher) surveille le Service Frontend Dynamo et, dès qu'au moins un endpoint passe à ready=true, enregistre le modèle dans LiteLLM via POST /model/new en stockant un identifiant déterministe dgd-<deployment_name> dans model_info.id3. Enfin, lors d'un appel à DELETE /api/v1/models/{model_name}, le service envoie d'abord POST /model/delete avec le corps JSON {"id": "dgd-<deployment_name>"} à LiteLLM, attend au minimum 15 secondes (supérieur à PROXY_CONFIG_RELOAD_INTERVAL_SECONDS=10 pour que tous les réplicas LiteLLM purgent la route de leur mémoire), vérifie via Prometheus que vllm:num_requests_running == 0 (avec un timeout maximal de 120s), puis supprime les ScaledObject KEDA et le DynamoGraphDeployment3.
Spécification Système — Agent 5 (Autoscaling KEDA DGDSA, Observabilité RDMA & QA E2E)
Rôle assigné : Ingénieur Observabilité, Autoscaling KEDA et Assurance Qualité. Le périmètre d'écriture exclusif de l'agent est limité à monitoring/ et tests/. L'agent configure Prometheus et DCGM Exporter pour collecter les métriques du Frontend Dynamo (/metrics), des workers vLLM et des compteurs NIXL (vllm:nixl_post_time_seconds)5. L'agent rédige les templates ScaledObject KEDA (monitoring/keda-scaledobjects.yaml.j2) en ciblant exclusivement la ressource DynamoGraphDeploymentScalingAdapter (apiVersion: nvidia.com/v1beta1, kind: DynamoGraphDeploymentScalingAdapter, nommée <deployment_name>-prefill et <deployment_name>-decode) et s'assure que le Dynamo Planner est désactivé lorsque KEDA pilote l'adaptateur5. Le scaling de prefill est déclenché par le quantile p95 de dynamo_frontend_time_to_first_token_seconds_bucket et vllm:num_requests_waiting ; le scaling de decode est déclenché par vllm:gpu_cache_usage_perc5. Dans tests/, l'agent écrit les tests unitaires pytest (calculateur VRAM GQA/MLA/MoE, validation de schéma CRD v1beta1) et le scénario E2E complet utilisant l'outil de benchmark aiperf (exécuté sous Python 3.12), vérifiant que la latence de transfert NIXL reste inférieure à 5 ms (absence de repli TCP) et qu'aucune erreur HTTP 502 ou 524 ne survient lors d'un déploiement et d'une suppression sous flux SSE continu7.
8. Conclusions et Recommandations d'Ingénierie
La viabilité en production d'une plateforme d'inférence désagrégée combinant Kubernetes, NVIDIA Dynamo, LiteLLM et Cloudflare dépend de l'alignement strict entre la couche d'abstraction logicielle et les mécanismes physiques sous-jacents7. La mise à niveau du plan d'exécution vers l'API nvidia.com/v1beta1, l'intégration explicite des connecteurs NixlConnector et PdConnector (KVBM), l'adoption du mécanisme de bail NIXL à battements de cœur (30s/5s) et le ciblage des ressources DynamoGraphDeploymentScalingAdapter par KEDA éliminent l'ensemble des blocages structurels identifiés dans la conception initiale2.
Pour l'exploitation à jour 2 (Day-2 Operations), trois axes d'optimisation complémentaires s'imposent afin de consolider les performances et la résilience de la plateforme7. Premièrement, l'intégration de Dynamo Snapshot (DynamoCheckpoint) devient essentielle dès que le cluster dépasse 4 GPU par réplica (), car le temps d'initialisation de CUDA, de compilation des graphes et de chargement des poids pénalise la réactivité de KEDA lors des pics de charge ; l'activation de la capture automatique de points de contrôle avec les politiques WaitForCheckpoint ou Immediate réduit drastiquement le temps de démarrage à froid des nouveaux pods Decode1. Deuxièmement, une surveillance active du transport NIXL doit être instituée dans Prometheus : des panneaux vides ou à valeur nulle sur les métriques de transfert NIXL dans Grafana ne constituent jamais un simple défaut de télémétrie, mais le symptôme primaire d'une défaillance d'initialisation RDMA (ibv_reg_mr) ou d'un repli TCP7. L'ajout d'une règle d'alerte dans prometheus-rules.yaml déclenchée dès que la latence p95 de transfert NIXL (vllm:nixl_post_time_seconds) dépasse 10 ms garantit la détection immédiate de toute dégradation de la fabrique InfiniBand/RoCE42. Troisièmement, l'exploitation hybride du Dynamo Profiler (DGDR) permet de combiner le calculateur analytique de l'Agent 3 pour le contrôle d'admission synchrone rapide avec l'exécution asynchrone du job de simulation AI Configurator via un DynamoGraphDeploymentRequest (autoApply: false), offrant à l'équipe d'exploitation un mécanisme continu de recalibrage des ratios Prefill/Decode face à l'évolution réelle des distributions de longueur d'invite9.
Sources des citations
Dynamo v1.5.0 - NVIDIA Documentation, <https://docs.nvidia.com/dynamo/reference/releases/v1-5-0>
DynamoGraphDeployment (DGD) Reference - NVIDIA Documentation, <https://docs.nvidia.com/dynamo/dev/reference/api/kubernetes/dynamo-graph-deployment>
Model Management | liteLLM, <https://docs.litellm.ai/docs/proxy/model_management>
vLLM Deployment Templates | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/v1.5.1/recipes/kubernetes-templates/dgd/v-llm>
Autoscaling | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/v-0-9-0/kubernetes-deployment/deployment-guide/autoscaling>
kubernetes native litellm operator - GitHub, <https://github.com/home-operations/litellm-operator>
P/D Disaggregation on a Single GPU - Karthika Raghavan, <https://kraghavan.ca/llm-infrastructure/inference/2026/04/21/llm-d-pd-disaggregation.html>
Deploying NVIDIA Dynamo on a Kubernetes Instant Cluster, <https://docs.verda.com/clusters/instant-clusters/tutorials/dynamo-inference/>
NVIDIA Dynamo on Kubernetes: A Deep Dive with vCluster Private, <https://www.vcluster.com/blog/nvidia-dynamo-with-vcluster>
Installation Guide | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/dev/kubernetes/installation/install-dynamo>
Production Best Practices | liteLLM, <https://docs.litellm.ai/docs/proxy/prod>
vLLM Deployment Templates | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/dev/recipes/kubernetes-templates/dgd/v-llm>
Remote Direct Memory Access (RDMA) and Networking Configuration, <https://llm-d.ai/docs/infrastructure/rdma>
DynamoGraphDeploymentRequ, <https://docs.nvidia.com/dynamo/v1.5.1/reference/api/kubernetes/dynamo-graph-deployment-request>
API Reference | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/dev/reference/api/kubernetes/full-api-reference>
TTFT-Driven Autoscaling for Disaggregated LLM Inference with, <https://azureglobalblackbelts.com/2026/05/11/dynamo-keda-autoscaling-on-aks>
DEP: Rewrite Dynamo custom resources to v1beta1 storage #11642, <https://github.com/ai-dynamo/dynamo/issues/11642>
Deploying LiteLLM: An Open-Source AI Gateway - DEV Community, <https://dev.to/vultr/deploying-litellm-an-open-source-ai-gateway-2idp>
Deploy - Open WebUI + LiteLLM | Team ChatGPT with an AI Gateway, <https://railway.com/deploy/open-webui-litellm?referralCode=sLjDXb>
dynamo/deploy/utils/README.md at main - GitHub, <https://github.com/ai-dynamo/dynamo/blob/main/deploy/utils/README.md>
Local Installation | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/dev/cli/installation/install-dynamo>
Dynamo Runtime - NVIDIA Documentation, <https://docs.nvidia.com/dynamo/v-0-8-1/user-guides/writing-python-workers-in-dynamo>
Building Pinterest's VLM Serving Stack on NVIDIA Dynamo - Medium, <https://medium.com/pinterest-engineering/building-pinterests-vlm-serving-stack-on-nvidia-dynamo-0dce6e93d0f3>
Auto Deploy with DGDR | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/kubernetes/auto-deployment/dgdr-walkthrough>
Model Deployment | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/dev/kubernetes/model-deployment/introduction>
DGDR Reference | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/v1.3.0/kubernetes-deployment/deploy-models/dgdr-reference>
Dynamo CRDs - NGC Catalog - NVIDIA, <https://catalog.ngc.nvidia.com/orgs/nvidia/ai-dynamo/helm-charts/dynamo-crds/->
dynamo/deploy/helm/charts/platform/README.md at main - GitHub, <https://github.com/ai-dynamo/dynamo/blob/main/deploy/helm/charts/platform/README.md>
Grove Open-Source Kubernetes API - NVIDIA Developer, <https://developer.nvidia.com/grove>
Streamline Complex AI Inference on Kubernetes with NVIDIA Grove, <https://developer.nvidia.com/blog/streamline-complex-ai-inference-on-kubernetes-with-nvidia-grove/>
NVIDIA Dynamo: Disaggregated Inference at Scale - Rafay Systems, <https://rafay.co/ai-and-cloud-native-blog/nvidia-dynamo-turning-disaggregated-inference-into-a-production-system>
EFA (RDMA over AWS Fabric) on EKS - NVIDIA Documentation, <https://docs.nvidia.com/dynamo/kubernetes/installation/rdma-setup/efa-on-aws>
Remote Direct Memory Access (RDMA) and Networking ... - GitHub, <https://github.com/llm-d/llm-d/blob/main/docs/infrastructure/rdma/README.md>
NixlConnector Usage Guide - vLLM, <https://docs.vllm.ai/en/v0.23.0/features/nixl_connector_usage/>
Support KV push from Prefill to Decode node using Nixl Connector, <https://github.com/vllm-project/vllm/issues/36923>
Disaggregated Prefill - LMCache, <https://docs.lmcache.ai/mp/disaggregated_prefill.html>
Native KV Cache Offloading to Any Filesystem with llm-d, <https://llm-d.ai/blog/native-kv-cache-offloading-to-any-file-system-with-llm-d>
KV Cache Offloading | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/v1.1.0/backends/v-llm/kv-cache-offloading>
vLLM | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/dev/knowledge-base/modular-components/backends/v-llm/overview>
Achieving Up to 67% Cost Savings with Prefill-Decode ... - Anyscale, <https://www.anyscale.com/blog/ray-vllm-prefill-decode-disaggregation-amd-mi325x-67-percent-savings>
NIXL KV Cache Lease Renewal - vLLM Documentation, <https://docs.vllm.ai/en/latest/design/nixl_kv_cache_lease/>
NVIDIA NIXL and Disaggregated Inference: Move KV Caches, <https://www.spheron.network/blog/nvidia-nixl-disaggregated-inference-guide/>
Taking vLLM Apart: A Practical Guide to Disaggregated Serving, <https://vllm.ai/blog/2026-09-29-disaggregated-serving-guide>
Configure Kubernetes Explorer - Containers - Datadog Docs, <https://docs.datadoghq.com/containers/monitoring/kubernetes_explorer_configuration/>
Generative AI on Kubernetes | WisdomInterface, <https://www.wisdominterface.com/wp-content/uploads/2026/02/cl-oreilly-generative-ai-kubernetes-analyst-material-3188555kr-202512-en.pdf>
Autoscaling | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/knowledge-base/kubernetes/kubernetes-operator/autoscaling>
[Feature]: Dynamic reloading config file #964 - BerriAI/litellm - GitHub, <https://github.com/BerriAI/litellm/issues/964>
Config.yaml - LiteLLM documentation, <https://docs.litellm.ai/docs/proxy/configs>
Router - Load Balancing | liteLLM, <https://docs.litellm.ai/docs/routing>
Exposing Kubernetes Apps to the Internet with Cloudflare Tunnel, <https://itnext.io/exposing-kubernetes-apps-to-the-internet-with-cloudflare-tunnel-ingress-controller-and-e30307c0fcb0>
config_settings - LiteLLM documentation, <https://docs.litellm.ai/docs/proxy/config_settings>
NixlConnector Usage Guide - vLLM, <https://docs.vllm.ai/en/stable/features/nixl_connector_usage/>
DGDR Templates | NVIDIA Dynamo Documentation, <https://docs.nvidia.com/dynamo/v1.5.1/recipes/kubernetes-templates/dgdr>
