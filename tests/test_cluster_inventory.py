from types import SimpleNamespace as NS

from api.k8s_client import inventory_from_nodes
from autotuner.sla_planner import SLAPlanner


def node(name, sku, count, memory, cordoned=False):
    return NS(metadata=NS(name=name, labels={"nvidia.com/gpu.product": sku, "nvidia.com/gpu.memory": memory}),
              spec=NS(unschedulable=cordoned), status=NS(allocatable={"nvidia.com/gpu": str(count)}, conditions=[NS(type="Ready", status="True")]))


def container(count, requests=True):
    return NS(resources=NS(requests={"nvidia.com/gpu": str(count)} if requests else None, limits={"nvidia.com/gpu": str(count)}))


def test_inventory_keeps_pools_distinct_and_excludes_cordoned_capacity():
    nodes = [node("a", "A100", 4, "40960"), node("b", "H100", 2, "81920"), node("c", "H100", 2, "81920", cordoned=True)]
    pods = [NS(spec=NS(node_name="a", containers=[container(1)], init_containers=[container(2)], overhead=None), status=NS(phase="Running")),
            NS(spec=NS(node_name="b", containers=[container(1, requests=False)], init_containers=[], overhead=None), status=NS(phase="Running"))]
    inventory = inventory_from_nodes(nodes, pods)
    pools = {pool["gpu_sku"]: pool for pool in inventory["gpu_pools"]}
    assert pools["A100"]["available_count"] == 2
    assert pools["A100"]["vram_gb"] == 40
    assert pools["H100"]["total_count"] == 4
    assert pools["H100"]["available_count"] == 1
    assert not inventory["rdma_network_active"]


def test_no_gpu_nodes_does_not_invent_a_default_pool():
    assert inventory_from_nodes([node("cpu", "", 0, "")], [])["gpu_pools"] == []


def test_missing_vram_metadata_is_unknown():
    assert inventory_from_nodes([node("gpu", "H100", 1, "")], [])["gpu_pools"][0]["vram_gb"] is None


def test_planning_respects_the_selected_gpu_pool_capacity():
    inventory = {"nodes_count": 2, "gpu_pools": [
        {"gpu_sku": "A100", "vram_gb": 40, "total_count": 1, "available_count": 1, "rdma_supported": False},
        {"gpu_sku": "H100", "vram_gb": 80, "total_count": 3, "available_count": 3, "rdma_supported": False},
    ], "rdma_network_active": False}
    plan = SLAPlanner().plan({"model_id": "meta-llama/Meta-Llama-3-8B-Instruct", "max_model_len": 8192, "target_concurrency": 4}, inventory)
    assert plan["worker_clique"]["gpu_sku"] == "H100"
    assert plan["worker_clique"]["replicas"] == 3
