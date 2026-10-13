"""
Dynamo Reconciler: Handles Jinja2 rendering of DynamoGraphDeployment CRDs,
Kubernetes CR lifecycle, EndpointSlice readiness watching, and LiteLLM synchronization.
"""

import os
import asyncio
import logging
from typing import Dict, Any, Optional
import yaml
import jinja2
from jinja2 import FileSystemLoader, Environment

from .k8s_client import K8sClient
from .litellm_sync import LiteLLMSync

logger = logging.getLogger("dynamo_reconciler")

TEMPLATES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../dynamo/templates")
)

class DynamoReconciler:
    def __init__(
        self,
        k8s_client: Optional[K8sClient] = None,
        litellm_sync: Optional[LiteLLMSync] = None,
        templates_dir: str = TEMPLATES_DIR,
        drain_seconds: int = 15,
    ):
        self.k8s = k8s_client or K8sClient()
        self.litellm = litellm_sync or LiteLLMSync()
        self.drain_seconds = drain_seconds
        self.active_deployments: Dict[str, Dict[str, Any]] = {}

        self.jinja_env = Environment(
            loader=FileSystemLoader(templates_dir),
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False
        )

    def render_manifest(self, plan: Dict[str, Any], namespace: str = "dynamo-workloads") -> Dict[str, Any]:
        """
        Renders the DynamoGraphDeployment Jinja2 template according to Contract 2.
        """
        serving_mode = plan.get("serving_mode", "aggregated")
        template_name = "dgd-disaggregated.yaml.j2" if serving_mode == "disaggregated" else "dgd-aggregated.yaml.j2"
        
        template = self.jinja_env.get_template(template_name)
        context = {**plan, "namespace": namespace}
        rendered_yaml = template.render(context)
        parsed_dict = yaml.safe_load(rendered_yaml)
        return parsed_dict

    async def deploy_model(self, plan: Dict[str, Any], namespace: str = "dynamo-workloads") -> Dict[str, Any]:
        deployment_name = plan["deployment_name"]
        logger.info(f"Reconciling deployment {deployment_name} in {namespace}...")

        # 1. Render YAML manifest
        manifest = self.render_manifest(plan, namespace)

        # 2. Apply Custom Resource to Kubernetes
        applied_cr = await self.k8s.apply_dynamo_deployment(manifest, namespace)

        self.active_deployments[deployment_name] = {
            "name": deployment_name,
            "model_id": plan["model_id"],
            "plan": plan,
            "namespace": namespace,
            "status": "provisioning",
            "ready": False,
        }

        # 3. Spawn background watcher for EndpointSlice readiness and LiteLLM sync
        asyncio.create_task(
            self._watch_and_sync_litellm(deployment_name, plan["model_id"], namespace)
        )

        return {
            "status": "provisioning",
            "deployment_name": deployment_name,
            "manifest": manifest,
            "plan": plan,
        }

    async def _watch_and_sync_litellm(self, deployment_name: str, model_id: str, namespace: str):
        """
        Watches until Frontend EndpointSlice indicates readiness, then notifies LiteLLM Proxy.
        """
        service_name = f"{deployment_name}-frontend"
        logger.info(f"Watching EndpointSlice readiness for {service_name}...")

        for attempt in range(60): # wait up to ~2 minutes
            is_ready = await self.k8s.is_endpoint_slice_ready(service_name, namespace)
            if is_ready:
                logger.info(f"Frontend service {service_name} is ready! Registering in LiteLLM...")
                reg_result = await self.litellm.register_model(
                    model_name=deployment_name,
                    original_model_id=model_id,
                    service_name=service_name,
                    namespace=namespace,
                )
                if deployment_name in self.active_deployments:
                    self.active_deployments[deployment_name]["status"] = "running"
                    self.active_deployments[deployment_name]["ready"] = True
                    self.active_deployments[deployment_name]["litellm_registered"] = True
                return reg_result
            await asyncio.sleep(2)

        logger.warning(f"Timeout waiting for EndpointSlice readiness on {service_name}.")

    async def delete_model(self, deployment_name: str, namespace: str = "dynamo-workloads") -> bool:
        """
        Executes graceful teardown:
        1. Deregisters from LiteLLM
        2. Waits drain_seconds (default 15s) for in-flight completions
        3. Deletes Kubernetes Custom Resource
        """
        logger.info(f"Graceful deletion initiated for {deployment_name}...")

        # 1. LiteLLM deregistration
        await self.litellm.deregister_model(deployment_name)

        # 2. Drain in-flight generations
        logger.info(f"Draining active generations for {self.drain_seconds} seconds...")
        await asyncio.sleep(self.drain_seconds)

        # 3. K8s CRD deletion
        deleted = await self.k8s.delete_dynamo_deployment(deployment_name, namespace)
        self.active_deployments.pop(deployment_name, None)
        logger.info(f"Deletion complete for {deployment_name}.")
        return deleted

    def get_deployment_status(self, deployment_name: str) -> Optional[Dict[str, Any]]:
        return self.active_deployments.get(deployment_name)

    def list_deployments(self) -> list[Dict[str, Any]]:
        return list(self.active_deployments.values())
