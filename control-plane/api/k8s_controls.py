"""Namespaced control of operator-managed inference workloads."""

from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException

if TYPE_CHECKING:
    from .k8s_client import K8sClient

NAMESPACE = "dynamo-workloads"
POD_LABEL = "nvidia.com/dynamo-graph-deployment-name"
ADAPTER_PLURAL = "dynamographdeploymentscalingadapters"


class ControlError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def component_specs(resource: dict) -> dict[str, dict]:
    spec = resource.get("spec", {})
    if isinstance(spec.get("services"), dict):
        return spec["services"]
    return {component.get("componentName", component.get("name")): component for component in spec.get("components", [])}


def gpu_per_replica(component: dict) -> int:
    containers = component.get("podTemplate", {}).get("spec", {}).get("containers", [])
    if containers:
        resources = [container.get("resources", {}) for container in containers]
    else:
        resources = [component.get("resources", component.get("extraPodSpec", {}).get("mainContainer", {}).get("resources", {}))]
    return sum(int(resource.get("limits", resource.get("requests", {})).get("nvidia.com/gpu", 0)) for resource in resources) * int(component.get("multinode", {}).get("nodeCount", 1))


def pod_gpu_count(pod: Any) -> int:
    return sum((int((container.resources.limits or {}).get("nvidia.com/gpu", 0)) for container in pod.spec.containers if container.resources), 0)


class KubernetesControls:
    def __init__(self, k8s: "K8sClient"):
        self.k8s = k8s

    async def managed_deployment(self, name: str) -> dict:
        resource = await self.k8s.get_dynamo_deployment(name, NAMESPACE)
        if not resource:
            raise ControlError(404, "Deployment was not found.")
        if resource.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/managed-by") != "dynamo-control-plane":
            raise ControlError(403, "This workload is outside the control plane's managed resources.")
        return resource

    async def nodes(self) -> list[dict]:
        await self.k8s.initialize()
        if self.k8s.mock_mode:
            return [{"name": f"mock-gpu-node-{index}", "ready": True, "schedulable": True, "gpu_count": 4, "gpu_sku": "NVIDIA-H100-80GB", "cpu": "32", "memory": "256Gi", "pressure": []} for index in (1, 2)]
        async with client.ApiClient() as api:
            nodes = await client.CoreV1Api(api).list_node()
        return [{
            "name": node.metadata.name,
            "ready": any(condition.type == "Ready" and condition.status == "True" for condition in node.status.conditions or []),
            "schedulable": not node.spec.unschedulable,
            "gpu_count": int((node.status.allocatable or {}).get("nvidia.com/gpu", 0)),
            "gpu_sku": (node.metadata.labels or {}).get("nvidia.com/gpu.product"),
            "cpu": (node.status.allocatable or {}).get("cpu"),
            "memory": (node.status.allocatable or {}).get("memory"),
            "pressure": [condition.type for condition in node.status.conditions or [] if condition.status == "True" and condition.type.endswith("Pressure")],
        } for node in nodes.items]

    async def pods(self, name: str) -> list[dict]:
        resource = await self.managed_deployment(name)
        if self.k8s.mock_mode:
            return [{
                "name": f"{name}-{component.lower()}-{index}", "uid": f"mock-{name}-{component.lower()}-{index}",
                "namespace": NAMESPACE, "component": component, "phase": "Running", "ready": True,
                "node": f"mock-gpu-node-{index % 2 + 1}", "gpu_count": gpu_per_replica(config),
                "restarts": self.k8s._mock_restarts.get(f"{name}-{component.lower()}-{index}", 0),
                "containers": ["main"], "reason": None, "created_at": None,
            } for component, config in component_specs(resource).items() for index in range(int(config.get("replicas", 0)))]
        async with client.ApiClient() as api:
            pods = await client.CoreV1Api(api).list_namespaced_pod(NAMESPACE, label_selector=f"{POD_LABEL}={name}")
        return [{
            "name": pod.metadata.name, "uid": pod.metadata.uid, "namespace": NAMESPACE,
            "component": (pod.metadata.labels or {}).get("nvidia.com/dynamo-component", "unknown"),
            "phase": pod.status.phase, "ready": any(condition.type == "Ready" and condition.status == "True" for condition in pod.status.conditions or []),
            "node": pod.spec.node_name,
            "gpu_count": pod_gpu_count(pod),
            "restarts": sum(container.restart_count for container in pod.status.container_statuses or []),
            "containers": [container.name for container in pod.spec.containers],
            "reason": pod.status.reason, "created_at": pod.metadata.creation_timestamp,
        } for pod in pods.items]

    async def pod_logs(self, name: str, pod_name: str, container: str | None, tail_lines: int) -> dict:
        pods = await self.pods(name)
        pod = next((pod for pod in pods if pod["name"] == pod_name), None)
        if not pod:
            raise ControlError(404, "Pod does not belong to this deployment.")
        selected = container or pod["containers"][0]
        if selected not in pod["containers"]:
            raise ControlError(404, "Container was not found in this pod.")
        if self.k8s.mock_mode:
            return {"pod": pod_name, "container": selected, "text": "Mock environment: no live container logs are available."}
        async with client.ApiClient() as api:
            text = await client.CoreV1Api(api).read_namespaced_pod_log(pod_name, NAMESPACE, container=selected, tail_lines=tail_lines, limit_bytes=131072, timestamps=True, _request_timeout=10)
        return {"pod": pod_name, "container": selected, "text": text}

    async def evict_pod(self, name: str, pod_name: str, uid: str) -> dict:
        async with self.k8s._control_lock:
            pods = await self.pods(name)
            pod = next((pod for pod in pods if pod["name"] == pod_name), None)
            if not pod:
                raise ControlError(404, "Pod does not belong to this deployment.")
            if pod["uid"] != uid:
                raise ControlError(409, "Pod was replaced. Refresh before requesting another replacement.")
            if self.k8s.mock_mode:
                self.k8s._mock_restarts[pod_name] = self.k8s._mock_restarts.get(pod_name, 0) + 1
            else:
                eviction = client.V1Eviction(
                    api_version="policy/v1", kind="Eviction", metadata=client.V1ObjectMeta(name=pod_name, namespace=NAMESPACE),
                    delete_options=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid)),
                )
                async with client.ApiClient() as api:
                    await client.CoreV1Api(api).create_namespaced_pod_eviction(pod_name, NAMESPACE, body=eviction)
        return {"status": "replacement_requested", "pod": pod_name, "mode": "mock" if self.k8s.mock_mode else "live"}

    async def _adapters(self, api: client.ApiClient) -> list[dict]:
        custom = client.CustomObjectsApi(api)
        for version in ("v1beta1", "v1alpha1"):
            try:
                data = await custom.list_namespaced_custom_object("nvidia.com", version, NAMESPACE, ADAPTER_PLURAL)
                return data.get("items", [])
            except ApiException as exc:
                if exc.status != 404:
                    raise
        return []

    async def components(self, name: str) -> list[dict]:
        resource = await self.managed_deployment(name)
        specs = component_specs(resource)
        if self.k8s.mock_mode:
            adapters = [{"metadata": {"name": f"{name}-{component.lower()}"}, "spec": {"dgdRef": {"name": name, "serviceName": component}, "replicas": config.get("replicas", 1)}, "status": {"replicas": config.get("replicas", 1)}} for component, config in specs.items()]
            autoscaled: set[str] = set()
        else:
            async with client.ApiClient() as api:
                adapters = await self._adapters(api)
                hpas = await client.AutoscalingV2Api(api).list_namespaced_horizontal_pod_autoscaler(NAMESPACE)
                autoscaled = {hpa.spec.scale_target_ref.name for hpa in hpas.items if hpa.spec.scale_target_ref.kind == "DynamoGraphDeploymentScalingAdapter"}
                # KEDA can own an adapter before its HPA has appeared.
                try:
                    scaled_objects = await client.CustomObjectsApi(api).list_namespaced_custom_object("keda.sh", "v1alpha1", NAMESPACE, "scaledobjects")
                    autoscaled.update(item.get("spec", {}).get("scaleTargetRef", {}).get("name", "") for item in scaled_objects.get("items", []))
                except ApiException as exc:
                    if exc.status != 404:
                        raise
        planner_active = any(config.get("componentType", config.get("type", "")).lower() == "planner" or component.lower() == "planner" for component, config in specs.items())
        results = []
        for adapter in adapters:
            ref = adapter.get("spec", {}).get("dgdRef", {})
            component = ref.get("componentName", ref.get("serviceName"))
            if ref.get("name") != name or component not in specs:
                continue
            controlled = planner_active or adapter["metadata"]["name"] in autoscaled
            results.append({
                "name": component, "adapter": adapter["metadata"]["name"],
                "api_version": adapter.get("apiVersion", "nvidia.com/v1alpha1"),
                "desired_replicas": adapter.get("spec", {}).get("replicas", 0),
                "current_replicas": adapter.get("status", {}).get("replicas", 0),
                "resource_version": adapter["metadata"].get("resourceVersion"),
                "gpu_per_replica": gpu_per_replica(specs[component]),
                "minimum_replicas": max(1, int(specs[component].get("minAvailable", 1))),
                "can_scale": not controlled, "blocked_reason": "Managed by an autoscaler or Dynamo Planner." if controlled else None,
                "target_kind": "adapter",
            })
        adapted = {item["name"] for item in results}
        for component, config in specs.items():
            if component in adapted:
                continue
            declared_adapter = config.get("scalingAdapter") is not None
            results.append({
                "name": component, "adapter": None, "target_kind": "deployment",
                "api_version": resource.get("apiVersion", "nvidia.com/v1beta1"),
                "desired_replicas": config.get("replicas", 1), "current_replicas": None,
                "resource_version": resource.get("metadata", {}).get("resourceVersion"),
                "gpu_per_replica": gpu_per_replica(config), "minimum_replicas": max(1, int(config.get("minAvailable", 1))),
                "can_scale": not declared_adapter and not planner_active,
                "blocked_reason": "Waiting for a scaling adapter." if declared_adapter else "Managed by Dynamo Planner." if planner_active else None,
            })
        return results

    async def scale(self, name: str, component_name: str, replicas: int, resource_version: str | None) -> dict[str, Any]:
        async with self.k8s._control_lock:
            components = await self.components(name)
            target = next((component for component in components if component["name"] == component_name), None)
            if not target:
                raise ControlError(409, "No scaling adapter is available for this component.")
            if not target["can_scale"]:
                raise ControlError(409, target["blocked_reason"])
            if replicas < target["minimum_replicas"]:
                raise ControlError(409, "Replica count is below this component's minimum availability.")
            if not self.k8s.mock_mode and (not resource_version or resource_version != target["resource_version"]):
                raise ControlError(409, "Replica settings changed. Refresh before scaling.")
            extra_gpus = max(0, replicas - target["desired_replicas"]) * target["gpu_per_replica"]
            inventory = await self.k8s.get_cluster_gpu_inventory()
            if extra_gpus > sum(pool["available_count"] for pool in inventory["gpu_pools"]):
                raise ControlError(409, "There is not enough unallocated GPU capacity for these replicas.")
            if self.k8s.mock_mode:
                resource = await self.managed_deployment(name)
                component_specs(resource)[component_name]["replicas"] = replicas
            else:
                async with client.ApiClient() as api:
                    custom = client.CustomObjectsApi(api)
                    if target["target_kind"] == "adapter":
                        await custom.patch_namespaced_custom_object_scale(
                            "nvidia.com", target["api_version"].split("/")[-1], NAMESPACE, ADAPTER_PLURAL, target["adapter"],
                            body={"apiVersion": "autoscaling/v1", "kind": "Scale", "metadata": {"resourceVersion": resource_version}, "spec": {"replicas": replicas}},
                        )
                    else:
                        resource = await self.managed_deployment(name)
                        if resource.get("metadata", {}).get("resourceVersion") != resource_version:
                            raise ControlError(409, "Deployment changed. Refresh before scaling.")
                        specs = component_specs(resource)
                        specs[component_name]["replicas"] = replicas
                        patch = {"services": {component_name: {"replicas": replicas}}} if "services" in resource["spec"] else {"components": resource["spec"]["components"]}
                        await custom.patch_namespaced_custom_object(
                            "nvidia.com", target["api_version"].split("/")[-1], NAMESPACE, self.k8s.DGD_PLURAL, name,
                            body={"metadata": {"resourceVersion": resource_version}, "spec": patch},
                        )
        return {"status": "scaling", "component": component_name, "replicas": replicas}
