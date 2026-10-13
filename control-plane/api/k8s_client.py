"""
Asynchronous Kubernetes Client using kubernetes_asyncio for Custom Resource & Resource lifecycle.
Adheres to R1: No silent mock fallback. Mock mode is strictly opt-in via CONTROL_PLANE_MOCK=1.
"""

import asyncio
import logging
import os
from typing import Any

from .k8s_controls import component_specs, gpu_per_replica
from .metrics import UsageMetrics

logger = logging.getLogger("k8s_client")

try:
    from kubernetes_asyncio import client, config
    from kubernetes_asyncio.client.rest import ApiException

    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False
    ApiException = Exception  # type: ignore[misc,assignment]


class K8sClient:
    DGD_GROUP = "nvidia.com"
    DGD_VERSION = "v1beta1"
    DGD_PLURAL = "dynamographdeployments"

    def __init__(self, in_cluster: bool = False, mock_mode: bool | None = None):
        env_mock = os.environ.get("CONTROL_PLANE_MOCK", "0") in ("1", "true", "True")
        is_in_cluster = in_cluster or os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")

        if is_in_cluster and (mock_mode or env_mock):
            raise RuntimeError("Mock mode (CONTROL_PLANE_MOCK=1) is strictly forbidden in-cluster!")

        if mock_mode is not None:
            self.mock_mode = mock_mode
        else:
            self.mock_mode = env_mock

        self._initialized = False
        self._mock_resources: dict[str, dict[str, Any]] = {}
        self._mock_endpoint_slices: dict[str, bool] = {}
        self._mock_scaled_objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.in_cluster = is_in_cluster
        self.metrics = UsageMetrics()
        self._control_lock = asyncio.Lock()
        self._mock_restarts: dict[str, int] = {}

    async def initialize(self):
        if self._initialized:
            return
        if self.mock_mode:
            self._initialized = True
            logger.info("Kubernetes client initialized in explicit MOCK mode.")
            return

        if not K8S_AVAILABLE:
            raise RuntimeError("kubernetes_asyncio is not installed and CONTROL_PLANE_MOCK=1 is not set.")

        try:
            if self.in_cluster:
                config.load_incluster_config()
            else:
                await config.load_kube_config()
            self._initialized = True
            logger.info("Kubernetes async client initialized successfully with live cluster.")
        except Exception as e:
            logger.error(f"Failed to load Kubernetes configuration: {e}")
            raise RuntimeError(
                f"Kubernetes cluster connection failed: {e}. "
                "Set CONTROL_PLANE_MOCK=1 to opt-in to mock mode for offline testing."
            )

    async def apply_dynamo_deployment(
        self, manifest_dict: dict[str, Any], namespace: str = "dynamo-workloads"
    ) -> dict[str, Any]:
        await self.initialize()
        name = manifest_dict["metadata"]["name"]
        if self.mock_mode:
            self._mock_resources[name] = manifest_dict
            self._mock_endpoint_slices[name] = True
            logger.info(f"[MOCK] Applied DynamoGraphDeployment: {name} in {namespace}")
            return manifest_dict

        async with client.ApiClient() as api:
            custom_api = client.CustomObjectsApi(api)
            try:
                # Try to get existing resource
                await custom_api.get_namespaced_custom_object(
                    group=self.DGD_GROUP,
                    version=self.DGD_VERSION,
                    namespace=namespace,
                    plural=self.DGD_PLURAL,
                    name=name,
                )
                res = await custom_api.patch_namespaced_custom_object(
                    group=self.DGD_GROUP,
                    version=self.DGD_VERSION,
                    namespace=namespace,
                    plural=self.DGD_PLURAL,
                    name=name,
                    body=manifest_dict,
                )
            except ApiException as e:
                if e.status == 404:
                    res = await custom_api.create_namespaced_custom_object(
                        group=self.DGD_GROUP,
                        version=self.DGD_VERSION,
                        namespace=namespace,
                        plural=self.DGD_PLURAL,
                        body=manifest_dict,
                    )
                else:
                    raise
            return res

    async def get_dynamo_deployment(self, name: str, namespace: str = "dynamo-workloads") -> dict[str, Any] | None:
        await self.initialize()
        if self.mock_mode:
            return self._mock_resources.get(name)

        async with client.ApiClient() as api:
            custom_api = client.CustomObjectsApi(api)
            try:
                res = await custom_api.get_namespaced_custom_object(
                    group=self.DGD_GROUP,
                    version=self.DGD_VERSION,
                    namespace=namespace,
                    plural=self.DGD_PLURAL,
                    name=name,
                )
                return res
            except ApiException as e:
                if e.status == 404:
                    return None
                raise

    async def list_dynamo_deployments(self, namespace: str = "dynamo-workloads") -> list[dict[str, Any]]:
        await self.initialize()
        if self.mock_mode:
            return list(self._mock_resources.values())

        async with client.ApiClient() as api:
            custom_api = client.CustomObjectsApi(api)
            res = await custom_api.list_namespaced_custom_object(
                group=self.DGD_GROUP, version=self.DGD_VERSION, namespace=namespace, plural=self.DGD_PLURAL
            )
            return res.get("items", [])

    async def delete_dynamo_deployment(self, name: str, namespace: str = "dynamo-workloads") -> bool:
        await self.initialize()
        if self.mock_mode:
            existed = name in self._mock_resources
            self._mock_resources.pop(name, None)
            self._mock_endpoint_slices.pop(name, None)
            logger.info(f"[MOCK] Deleted DynamoGraphDeployment: {name}")
            return existed

        async with client.ApiClient() as api:
            custom_api = client.CustomObjectsApi(api)
            try:
                await custom_api.delete_namespaced_custom_object(
                    group=self.DGD_GROUP,
                    version=self.DGD_VERSION,
                    namespace=namespace,
                    plural=self.DGD_PLURAL,
                    name=name,
                )
                return True
            except ApiException as e:
                if e.status == 404:
                    return False
                raise

    async def apply_scaled_object(
        self, manifest_dict: dict[str, Any], namespace: str = "dynamo-workloads"
    ) -> dict[str, Any]:
        await self.initialize()
        name = manifest_dict["metadata"]["name"]
        if self.mock_mode:
            self._mock_scaled_objects[(namespace, name)] = manifest_dict
            return manifest_dict
        async with client.ApiClient() as api:
            custom_api = client.CustomObjectsApi(api)
            args: dict[str, Any] = {"group": "keda.sh", "version": "v1alpha1", "namespace": namespace, "plural": "scaledobjects"}
            try:
                await custom_api.get_namespaced_custom_object(**args, name=name)
            except ApiException as exc:
                if exc.status != 404:
                    raise
                return await custom_api.create_namespaced_custom_object(**args, body=manifest_dict)
            return await custom_api.patch_namespaced_custom_object(**args, name=name, body=manifest_dict)

    async def delete_scaled_object(self, name: str, namespace: str = "dynamo-workloads") -> bool:
        await self.initialize()
        if self.mock_mode:
            return self._mock_scaled_objects.pop((namespace, name), None) is not None
        async with client.ApiClient() as api:
            try:
                await client.CustomObjectsApi(api).delete_namespaced_custom_object(
                    group="keda.sh", version="v1alpha1", namespace=namespace, plural="scaledobjects", name=name
                )
                return True
            except ApiException as exc:
                if exc.status == 404:
                    return False
                raise

    async def get_target_gpu_nodes(self, plan: dict[str, Any]) -> list[str]:
        """Select Ready, schedulable GPU hostnames for both warming and serving."""
        await self.initialize()
        if self.mock_mode:
            return [plan["target_node"]] if plan.get("target_node") else ["mock-gpu-0", "mock-gpu-1"]
        cliques = [plan[key] for key in ("worker_clique", "prefill_clique", "decode_clique") if key in plan]
        async with client.ApiClient() as api:
            nodes = await client.CoreV1Api(api).list_node()
        targets = []
        for node in nodes.items:
            labels = node.metadata.labels or {}
            hostname = labels.get("kubernetes.io/hostname")
            if not hostname or node.spec.unschedulable:
                continue
            if plan.get("target_node") and hostname != plan["target_node"]:
                continue
            if not any(condition.type == "Ready" and condition.status == "True" for condition in node.status.conditions or []):
                continue
            gpu_count = int((node.status.allocatable or {}).get("nvidia.com/gpu", "0"))
            if any(
                labels.get("nvidia.com/gpu.product") == clique.get("gpu_sku")
                and gpu_count >= clique["tensor_parallel_size"] * clique.get("pipeline_parallel_size", 1)
                for clique in cliques
            ):
                targets.append(hostname)
        return sorted(set(targets))

    async def apply_job(self, manifest_dict: dict[str, Any], namespace: str = "dynamo-workloads") -> Any:
        await self.initialize()
        name = manifest_dict["metadata"]["name"]
        if self.mock_mode:
            logger.info(f"[MOCK] Applied Warmer Job: {name} in {namespace}")
            return manifest_dict

        async with client.ApiClient() as api:
            batch_api = client.BatchV1Api(api)
            try:
                await batch_api.delete_namespaced_job(name=name, namespace=namespace, propagation_policy="Background")
                await asyncio.sleep(1)
            except ApiException as exc:
                if exc.status != 404:
                    raise
            res = await batch_api.create_namespaced_job(namespace=namespace, body=manifest_dict)  # type: ignore[arg-type]
            return res

    async def wait_for_job_completion(
        self, job_name: str, namespace: str = "dynamo-workloads", timeout_seconds: int = 1800
    ) -> bool:
        await self.initialize()
        if self.mock_mode:
            logger.info(f"[MOCK] Warmer Job {job_name} finished immediately.")
            return True

        async with client.ApiClient() as api:
            batch_api = client.BatchV1Api(api)
            poll_interval = 5
            elapsed = 0
            while elapsed < timeout_seconds:
                job = await batch_api.read_namespaced_job_status(name=job_name, namespace=namespace)
                conditions = job.status.conditions or []
                if any(condition.type == "Failed" and condition.status == "True" for condition in conditions):
                    raise RuntimeError(f"Warmer Job {job_name} failed.")
                if any(condition.type == "Complete" and condition.status == "True" for condition in conditions):
                    return True
                if (job.status.succeeded or 0) >= (job.spec.completions or 1):
                    logger.info(f"Warmer Job {job_name} succeeded.")
                    return True
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            raise TimeoutError(f"Warmer Job {job_name} timed out after {timeout_seconds} seconds.")

    async def get_running_requests_count(self, model_name: str) -> int:
        """Verify active requests from Prometheus; unavailable monitoring is an error."""
        await self.initialize()
        if self.mock_mode:
            return 0
        deployment = await self.get_dynamo_deployment(model_name)
        model_id = (deployment.get("metadata", {}).get("annotations", {}).get("inference.platform/model-id")
                    or deployment.get("spec", {}).get("model")) if deployment else None
        if not model_id:
            raise RuntimeError("The model ID cannot be resolved for a drain check.")
        return await self.metrics.running_requests(model_id)

    async def is_endpoint_slice_ready(self, service_name: str, namespace: str = "dynamo-workloads") -> bool:
        await self.initialize()
        if self.mock_mode:
            return self._mock_endpoint_slices.get(service_name, True)

        async with client.ApiClient() as api:
            discovery_api = client.DiscoveryV1Api(api)
            try:
                slices = await discovery_api.list_namespaced_endpoint_slice(
                    namespace=namespace, label_selector=f"kubernetes.io/service-name={service_name}"
                )
                for item in slices.items:
                    for endpoint in item.endpoints or []:
                        if endpoint.conditions and endpoint.conditions.ready:
                            return True
                return False
            except Exception as e:
                logger.error(f"Error querying EndpointSlices for {service_name}: {e}")
                return False

    async def get_cluster_gpu_inventory(self) -> dict[str, Any]:
        """
        Retrieves real GPU inventory from GPU Feature Discovery (GFD) labels
        and computes actual available GPUs by subtracting pod limits (D5 fix).
        """
        await self.initialize()
        if self.mock_mode:
            allocated = sum(
                int(service.get("replicas", 0)) * gpu_per_replica(service)
                for resource in self._mock_resources.values()
                for service in component_specs(resource).values()
            )
            return {
                "nodes_count": 2,
                "gpu_pools": [
                    {
                        "gpu_sku": "NVIDIA-H100-80GB",
                        "vram_gb": 80.0,
                        "total_count": 8,
                        "available_count": max(0, 8 - allocated),
                        "rdma_supported": True,
                    }
                ],
                "rdma_network_active": True,
            }

        async with client.ApiClient() as api:
            core_api = client.CoreV1Api(api)
            nodes = await core_api.list_node()
            pods = await core_api.list_pod_for_all_namespaces(
                field_selector="status.phase!=Failed,status.phase!=Succeeded"
            )

            # Calculate total GPUs allocated to pods
            allocated_gpus = 0
            for pod in pods.items:
                for c in pod.spec.containers:
                    if c.resources and c.resources.requests:
                        gpu_req = c.resources.requests.get("nvidia.com/gpu", "0")
                        try:
                            allocated_gpus += int(gpu_req)
                        except ValueError:
                            pass

            total_gpus = 0
            gpu_product = "NVIDIA-H100-80GB"
            gpu_memory = 80.0
            rdma_active = True

            for node in nodes.items:
                labels = node.metadata.labels or {}
                allocatable = node.status.allocatable or {}

                # Read GPU Feature Discovery labels
                product = labels.get("nvidia.com/gpu.product")
                memory_mb = labels.get("nvidia.com/gpu.memory")
                if product:
                    gpu_product = product
                if memory_mb:
                    try:
                        gpu_memory = round(float(memory_mb) / 1024, 1)
                    except ValueError:
                        pass

                gpu_str = allocatable.get("nvidia.com/gpu", "0")
                try:
                    total_gpus += int(gpu_str)
                except ValueError:
                    pass

            available_gpus = max(0, total_gpus - allocated_gpus)

            return {
                "nodes_count": len(nodes.items),
                "gpu_pools": [
                    {
                        "gpu_sku": gpu_product,
                        "vram_gb": gpu_memory,
                        "total_count": total_gpus,
                        "available_count": available_gpus,
                        "rdma_supported": rdma_active,
                    }
                ],
                "rdma_network_active": rdma_active,
            }
