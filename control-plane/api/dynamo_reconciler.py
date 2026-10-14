"""
Dynamo Reconciler adhering to R1, R2, and R3:
- Slices deployment state directly from Kubernetes Custom Resources (D8).
- Launches and awaits NVMe warmer job before creating DynamoGraphDeployment (D6).
- Reconciles existing cluster deployments on startup (D8).
- Dynamic drain driven by active generation metrics with configurable timeout (D8).
- Prevents K8s resource deletion if LiteLLM deregistration fails (D1).
"""

import asyncio
import hashlib
import logging
import os
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader

from .k8s_client import K8sClient
from .litellm_sync import LiteLLMSync

logger = logging.getLogger("dynamo_reconciler")

TEMPLATES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../dynamo/templates"))
MONITORING_TEMPLATES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../monitoring"))
LOADER_TEMPLATES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../loader"))


class DynamoReconciler:
    def __init__(
        self,
        k8s_client: K8sClient | None = None,
        litellm_sync: LiteLLMSync | None = None,
        templates_dir: str = TEMPLATES_DIR,
        drain_timeout_seconds: int = 120,
        readiness_timeout_seconds: int = 1800,
    ):
        self.k8s = k8s_client or K8sClient()
        self.litellm = litellm_sync or LiteLLMSync()
        self.drain_timeout_seconds = drain_timeout_seconds
        self.readiness_timeout_seconds = readiness_timeout_seconds
        self._in_memory_cache: dict[str, dict[str, Any]] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._reconcile_locks: dict[str, asyncio.Lock] = {}

        self.jinja_env = Environment(
            loader=FileSystemLoader([templates_dir, LOADER_TEMPLATES_DIR, MONITORING_TEMPLATES_DIR]),
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False,
        )

    async def reconcile_existing(self, namespace: str = "dynamo-workloads"):
        """Reconciles deployments already present on the Kubernetes cluster at startup (D8 fix)."""
        logger.info(f"Reconciling existing Dynamo deployments in {namespace}...")
        try:
            items = await self.k8s.list_dynamo_deployments(namespace=namespace)
            for item in items:
                name = item.get("metadata", {}).get("name")
                if name:
                    self._in_memory_cache[name] = {
                        "name": name,
                        "model_id": self._model_id(item),
                        "status": "running",
                        "ready": True,
                        "source": "cluster_cr",
                    }
                    logger.info(f"Recovered existing deployment: {name}")
        except Exception as e:
            logger.warning(f"Could not reconcile existing deployments from cluster: {e}")

    def render_manifest(self, plan: dict[str, Any], namespace: str = "dynamo-workloads") -> dict[str, Any]:
        """Renders the DynamoGraphDeployment Jinja2 template according to Contract 2."""
        serving_mode = plan.get("serving_mode", "aggregated")
        template_name = "dgd-disaggregated.yaml.j2" if serving_mode == "disaggregated" else "dgd-aggregated.yaml.j2"

        template = self.jinja_env.get_template(template_name)
        context = {**plan, "namespace": namespace}
        context["worker_components"] = (
            [("prefill", plan["prefill_clique"]), ("decode", plan["decode_clique"])]
            if serving_mode == "disaggregated" else [("worker", plan["worker_clique"])]
        )
        context["dynamo_image"] = os.environ.get(
            "DYNAMO_RUNTIME_IMAGE", plan.get("dynamo_image", "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.5.1")
        )
        context["dynamo_image_pull_secret"] = os.environ.get(
            "DYNAMO_RUNTIME_IMAGE_PULL_SECRET", plan.get("dynamo_image_pull_secret", "")
        )
        rendered_yaml = template.render(context)
        parsed_dict = yaml.safe_load(rendered_yaml)
        return parsed_dict

    def render_warmer_job(self, plan: dict[str, Any], namespace: str = "dynamo-workloads") -> dict[str, Any]:
        """Render a cache warmer pinned to one GPU hostname."""
        template = self.jinja_env.get_template("warmer-job.yaml.j2")
        context = {**plan, "namespace": namespace}
        node = plan.get("target_node", "")
        identity = f"{plan['deployment_name']}:{node}"
        suffix = f"-{hashlib.sha256(identity.encode()).hexdigest()[:10]}" if node else ""
        context["warmer_job_name"] = f"{plan['deployment_name'][:45]}-warmer{suffix}"
        if os.environ.get("MODEL_WARMER_IMAGE"):
            context["warmer_image"] = os.environ["MODEL_WARMER_IMAGE"]
        if os.environ.get("MODEL_WARMER_IMAGE_PULL_SECRET"):
            context["warmer_image_pull_secret"] = os.environ["MODEL_WARMER_IMAGE_PULL_SECRET"]
        rendered_yaml = template.render(context)
        return yaml.safe_load(rendered_yaml)

    @staticmethod
    def _model_id(cr: dict[str, Any]) -> str:
        return cr.get("metadata", {}).get("annotations", {}).get("inference.platform/model-id", "")

    def render_scaled_objects(self, plan: dict[str, Any], namespace: str = "dynamo-workloads") -> list[dict[str, Any]]:
        if plan.get("serving_mode") != "disaggregated":
            return []
        template = self.jinja_env.get_template("keda-scaledobjects.yaml.j2")
        rendered = template.render({**plan, "namespace": namespace})
        return [document for document in yaml.safe_load_all(rendered) if document]

    async def deploy_model(self, plan: dict[str, Any], namespace: str = "dynamo-workloads") -> dict[str, Any]:
        deployment_name = plan["deployment_name"]
        lock = self._reconcile_locks.setdefault(deployment_name, asyncio.Lock())
        async with lock:
            if deployment_name in self._in_memory_cache and self._in_memory_cache[deployment_name].get("status") in ("provisioning", "running"):
                raise RuntimeError(f"Deployment '{deployment_name}' is already being reconciled.")
            logger.info(f"Reconciling deployment {deployment_name} in {namespace}...")

            # Discover all eligible hosts, then pin one Job to each. Worker scheduling is
            # restricted to this same set so every node-local cache is ready before startup.
            target_nodes = await self.k8s.get_target_gpu_nodes(plan)
            if not target_nodes:
                raise RuntimeError("No eligible GPU nodes found for model cache warming.")
            plan = {**plan, "target_node_names": target_nodes}
            jobs = [self.render_warmer_job({**plan, "target_node": node}, namespace) for node in target_nodes]
            for job in jobs:
                await self.k8s.apply_job(job, namespace)
            completed = await asyncio.gather(*(
                self.k8s.wait_for_job_completion(job["metadata"]["name"], namespace, timeout_seconds=1800)
                for job in jobs
            ))
            if not all(completed):
                raise RuntimeError(f"Model cache warming failed for {deployment_name}.")

            manifest = self.render_manifest(plan, namespace)
            await self.k8s.apply_dynamo_deployment(manifest, namespace)
            for scaled_object in self.render_scaled_objects(plan, namespace):
                await self.k8s.apply_scaled_object(scaled_object, namespace)

            self._in_memory_cache[deployment_name] = {
                "name": deployment_name,
                "model_id": plan["model_id"],
                "plan": plan,
                "namespace": namespace,
                "status": "provisioning",
                "ready": False,
            }

            # 3. Spawn background watcher for EndpointSlice readiness and LiteLLM sync
            task = asyncio.create_task(self._watch_and_sync_litellm(deployment_name, plan["model_id"], namespace, plan.get("model_revision")))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

            return {
                "status": "provisioning",
                "deployment_name": deployment_name,
                "manifest": manifest,
                "plan": plan,
            }

    async def _watch_and_sync_litellm(self, deployment_name: str, model_id: str, namespace: str, model_revision: str | None = None):
        """Watches EndpointSlice readiness with configurable timeout, then notifies LiteLLM."""
        service_name = f"{deployment_name}-frontend"
        logger.info(
            f"Watching EndpointSlice readiness for {service_name} (timeout {self.readiness_timeout_seconds}s)..."
        )

        poll_interval = 2 if self.k8s.mock_mode else 5
        max_attempts = max(1, self.readiness_timeout_seconds // poll_interval)

        for attempt in range(max_attempts):
            is_ready = await self.k8s.is_endpoint_slice_ready(service_name, namespace)
            if is_ready:
                cached = self._in_memory_cache.get(deployment_name)
                if cached and cached.get("status") in ("deleting", "deleted"):
                    logger.info(f"Deployment {deployment_name} was deleted during readiness wait; skipping LiteLLM registration.")
                    return None
                logger.info(f"Frontend service {service_name} is ready! Registering in LiteLLM...")
                try:
                    reg_result = await self.litellm.register_model(
                        model_name=deployment_name,
                        original_model_id=model_id,
                        service_name=service_name,
                        namespace=namespace,
                        model_revision=model_revision,
                    )
                    if deployment_name in self._in_memory_cache:
                        self._in_memory_cache[deployment_name]["status"] = "running"
                        self._in_memory_cache[deployment_name]["ready"] = True
                        self._in_memory_cache[deployment_name]["litellm_registered"] = True
                    return reg_result
                except Exception as e:
                    logger.error(f"LiteLLM registration error for {deployment_name}: {e}")
                    if deployment_name in self._in_memory_cache:
                        self._in_memory_cache[deployment_name]["status"] = "failed"
                    raise

            await asyncio.sleep(poll_interval)

        logger.error(f"Timeout waiting for EndpointSlice readiness on {service_name}.")
        if deployment_name in self._in_memory_cache:
            self._in_memory_cache[deployment_name]["status"] = "readiness_timeout"

    async def delete_model(self, deployment_name: str, namespace: str = "dynamo-workloads") -> bool:
        cached = self._in_memory_cache.setdefault(deployment_name, {"name": deployment_name, "namespace": namespace})
        cached["status"] = "deleting"
        cached.pop("error", None)
        try:
            return await self._delete_model_resources(deployment_name, namespace)
        except Exception as exc:
            cached["status"] = "delete_failed"
            cached["error"] = str(exc)
            raise

    async def _delete_model_resources(self, deployment_name: str, namespace: str) -> bool:
        """
        Graceful teardown (D1 & D8):
        1. Deregisters from LiteLLM (blocks K8s deletion if this fails!)
        2. Drains until running requests == 0 (dynamic drain with timeout)
        3. Deletes Kubernetes Custom Resource
        """
        logger.info(f"Initiating graceful deletion for {deployment_name}...")

        # 1. Deregister from LiteLLM (raises error and prevents deletion if LiteLLM deregistration fails)
        await self.litellm.deregister_model(deployment_name)

        if not self.k8s.mock_mode:
            # Let every gateway replica observe deregistration before destroying backends.
            await asyncio.sleep(max(0, int(os.getenv("CONTROL_PLANE_GATEWAY_DRAIN_DELAY_SECONDS", "15"))))

        # 2. Dynamic drain loop: poll until active requests == 0 or drain timeout
        logger.info(f"Draining active generations for {deployment_name} (max {self.drain_timeout_seconds}s)...")
        drain_elapsed = 0
        poll_step = 2
        while True:
            running = await self.k8s.get_running_requests_count(deployment_name)
            if running == 0:
                logger.info(f"All active requests drained for {deployment_name}.")
                break
            if drain_elapsed >= self.drain_timeout_seconds:
                if not self.k8s.mock_mode:
                    raise TimeoutError("Active requests did not drain before the deadline; the Kubernetes workload was retained.")
                break
            await asyncio.sleep(poll_step)
            drain_elapsed += poll_step

        # Remove external autoscalers before deleting the operator-owned graph.
        # Deterministic names allow cleanup even after a control-plane restart.
        for role in ("prefill", "decode"):
            await self.k8s.delete_scaled_object(f"{deployment_name}-{role}-scaler", namespace)

        # 3. K8s CRD deletion
        deleted = await self.k8s.delete_dynamo_deployment(deployment_name, namespace)
        self._in_memory_cache.pop(deployment_name, None)
        logger.info(f"Resource cleanup finished for {deployment_name}.")
        return deleted

    async def get_deployment_status(
        self, deployment_name: str, namespace: str = "dynamo-workloads"
    ) -> dict[str, Any] | None:
        """Queries live CR from Kubernetes to ensure persistence across API restarts (D8 fix)."""
        cr = await self.k8s.get_dynamo_deployment(deployment_name, namespace)
        if cr:
            cached = self._in_memory_cache.get(deployment_name, {})
            return {
                "name": deployment_name,
                "model_id": self._model_id(cr) or cached.get("model_id"),
                "namespace": cr.get("metadata", {}).get("namespace", namespace),
                "serving_mode": cr.get("metadata", {}).get("labels", {}).get("dynamo.nvidia.com/serving-mode"),
                "plan": cached.get("plan"),
                "error": cached.get("error"),
                "status": cached.get("status", "running"),
                "ready": cached.get("ready", True),
                "k8s_cr": cr,
            }
        return self._in_memory_cache.get(deployment_name)

    async def list_deployments(self, namespace: str = "dynamo-workloads") -> list[dict[str, Any]]:
        """Queries all active deployments from Kubernetes CRs (D8 fix)."""
        crs = await self.k8s.list_dynamo_deployments(namespace=namespace)
        results = []
        for cr in crs:
            name = cr.get("metadata", {}).get("name")
            cached = self._in_memory_cache.get(name, {})
            results.append(
                {
                    "name": name,
                    "model_id": self._model_id(cr) or cached.get("model_id"),
                    "serving_mode": cr.get("metadata", {}).get("labels", {}).get("dynamo.nvidia.com/serving-mode"),
                    "status": cached.get("status", "running"),
                    "ready": cached.get("ready", True),
                }
            )
        if not results and self.k8s.mock_mode:
            return list(self._in_memory_cache.values())
        return results
