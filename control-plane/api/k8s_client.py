"""
Asynchronous Kubernetes Client using kubernetes_asyncio for Custom Resource & Resource lifecycle.
"""

import os
import logging
from typing import Dict, Any, List, Optional
import yaml

logger = logging.getLogger("k8s_client")

try:
    from kubernetes_asyncio import client, config
    from kubernetes_asyncio.client.rest import ApiException
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False
    ApiException = Exception

class K8sClient:
    DGD_GROUP = "nvidia.com"
    DGD_VERSION = "v1alpha1"
    DGD_PLURAL = "dynamographdeployments"

    def __init__(self, in_cluster: bool = False, mock_mode: bool = False):
        self.mock_mode = mock_mode or not K8S_AVAILABLE
        self._initialized = False
        self._mock_resources: Dict[str, Dict[str, Any]] = {}
        self._mock_endpoint_slices: Dict[str, bool] = {}
        self.in_cluster = in_cluster

    async def initialize(self):
        if self._initialized or self.mock_mode:
            return
        try:
            if self.in_cluster or os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"):
                config.load_incluster_config()
            else:
                await config.load_kube_config()
            self._initialized = True
            logger.info("Kubernetes async client initialized successfully.")
        except Exception as e:
            logger.warning(f"Could not load kubeconfig ({e}). Falling back to mock mode.")
            self.mock_mode = True

    async def apply_dynamo_deployment(self, manifest_dict: Dict[str, Any], namespace: str = "dynamo-workloads") -> Dict[str, Any]:
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
                    name=name
                )
                # If exists, replace/patch
                res = await custom_api.patch_namespaced_custom_object(
                    group=self.DGD_GROUP,
                    version=self.DGD_VERSION,
                    namespace=namespace,
                    plural=self.DGD_PLURAL,
                    name=name,
                    body=manifest_dict
                )
            except ApiException as e:
                if e.status == 404:
                    res = await custom_api.create_namespaced_custom_object(
                        group=self.DGD_GROUP,
                        version=self.DGD_VERSION,
                        namespace=namespace,
                        plural=self.DGD_PLURAL,
                        body=manifest_dict
                    )
                else:
                    raise
            return res

    async def delete_dynamo_deployment(self, name: str, namespace: str = "dynamo-workloads") -> bool:
        await self.initialize()
        if self.mock_mode:
            self._mock_resources.pop(name, None)
            self._mock_endpoint_slices.pop(name, None)
            logger.info(f"[MOCK] Deleted DynamoGraphDeployment: {name}")
            return True

        async with client.ApiClient() as api:
            custom_api = client.CustomObjectsApi(api)
            try:
                await custom_api.delete_namespaced_custom_object(
                    group=self.DGD_GROUP,
                    version=self.DGD_VERSION,
                    namespace=namespace,
                    plural=self.DGD_PLURAL,
                    name=name
                )
                return True
            except ApiException as e:
                if e.status == 404:
                    return False
                raise

    async def is_endpoint_slice_ready(self, service_name: str, namespace: str = "dynamo-workloads") -> bool:
        await self.initialize()
        if self.mock_mode:
            return self._mock_endpoint_slices.get(service_name, True)

        async with client.ApiClient() as api:
            discovery_api = client.DiscoveryV1Api(api)
            try:
                slices = await discovery_api.list_namespaced_endpoint_slice(
                    namespace=namespace,
                    label_selector=f"kubernetes.io/service-name={service_name}"
                )
                for item in slices.items:
                    for endpoint in item.endpoints or []:
                        if endpoint.conditions and endpoint.conditions.ready:
                            return True
                return False
            except Exception as e:
                logger.error(f"Error querying EndpointSlices for {service_name}: {e}")
                return False

    async def get_cluster_gpu_inventory(self) -> Dict[str, Any]:
        await self.initialize()
        if self.mock_mode:
            return {
                "nodes_count": 2,
                "gpu_pools": [
                    {
                        "gpu_sku": "NVIDIA-H100-80GB",
                        "vram_gb": 80.0,
                        "total_count": 8,
                        "available_count": 8,
                        "rdma_supported": True
                    }
                ],
                "rdma_network_active": True
            }

        async with client.ApiClient() as api:
            core_api = client.CoreV1Api(api)
            nodes = await core_api.list_node()
            total_gpus = 0
            for node in nodes.items:
                alloc = node.status.allocatable or {}
                gpu_str = alloc.get("nvidia.com/gpu", "0")
                try:
                    total_gpus += int(gpu_str)
                except ValueError:
                    pass

            return {
                "nodes_count": len(nodes.items),
                "gpu_pools": [
                    {
                        "gpu_sku": "NVIDIA-H100-80GB",
                        "vram_gb": 80.0,
                        "total_count": total_gpus,
                        "available_count": total_gpus,
                        "rdma_supported": True
                    }
                ],
                "rdma_network_active": True
            }
