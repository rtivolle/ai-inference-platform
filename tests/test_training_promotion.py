"""Pinned promotion through the real renderer and mocked Kubernetes APIs."""

import json
import runpy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from api.data_models import CaptureEvent, TrainingRequest
from api.data_routes import data_store
from api.data_store import ArtifactStore, DataStore
from api.training import TrainingService
from api.training_routes import training_service
from autotuner.sla_planner import ClusterInventory, DeployRequest, SLAPlanner
from autotuner.vram_calculator import ModelArchitectureSpecs
from starlette.testclient import TestClient
from test_data_workflow import dataset, event, result_for, training_request

ROOT = Path(__file__).resolve().parents[1]


def test_model_revision_is_pinned_in_inspection_warming_and_serving(reconciler):
    inspector = Mock()
    inspector.inspect.return_value = ModelArchitectureSpecs(num_params=494032768, num_hidden_layers=24, hidden_size=896,
                                                            num_attention_heads=14, num_key_value_heads=2)
    request = DeployRequest(model_id="example/trained", model_revision="a" * 40, max_model_len=1024, target_concurrency=1)
    plan = SLAPlanner(inspector=inspector).plan(request, ClusterInventory())
    inspector.inspect.assert_called_once_with("example/trained", revision="a" * 40)
    warmer = reconciler.render_warmer_job(plan)
    arguments = warmer["spec"]["template"]["spec"]["containers"][0]["args"]
    assert arguments[arguments.index("--revision") + 1] == "a" * 40
    manifest = reconciler.render_manifest(plan)
    components = manifest["spec"]["components"]
    worker_comp = next(c for c in components if c.get("name", "").lower() == "worker")
    worker = worker_comp["podTemplate"]["spec"]["containers"][0]
    assert worker["args"][worker["args"].index("--model") + 1] == f"/mnt/nvme/model-cache/example--trained/{'a' * 40}"
    assert worker["args"][worker["args"].index("--served-model-name") + 1] == "example/trained"


def test_data_contracts_match_runtime():
    import jsonschema
    from api.main import app

    expected = app.openapi()
    committed = yaml.safe_load((ROOT / "contracts/control_plane_openapi.yaml").read_text())
    for path, definition in expected["paths"].items():
        if path.startswith(("/api/v1/data", "/api/v1/training")):
            assert committed["paths"][path] == definition
    schema = json.loads((ROOT / "contracts/data_workflow.schema.json").read_text())
    assert schema == runpy.run_path(str(ROOT / "scripts/export_data_contracts.py"))["data_schema"]()
    jsonschema.Draft202012Validator.check_schema(schema)
    for model, value in [(CaptureEvent, event()), (TrainingRequest, training_request("dataset"))]:
        jsonschema.validate(value.model_dump(mode="json"), {**schema, "$ref": f"#/$defs/{model.__name__}"})
    assert expected["paths"]["/api/v1/data/events"]["post"]["security"] == [{"CaptureToken": []}]


@pytest.mark.asyncio
async def test_only_job_owned_pod_logs_are_exposed(tmp_path, mock_k8s, monkeypatch):
    from api import training

    store = DataStore(":memory:", ArtifactStore(str(tmp_path)))
    service = TrainingService(store, mock_k8s)
    store.save("run", "run", {"id": "run", "job_name": "ft-run"})
    mock_k8s.mock_mode = False
    batch, core = AsyncMock(), AsyncMock()
    batch.read_namespaced_job.return_value = Mock(metadata=Mock(uid="real-job"))
    owned = Mock(metadata=Mock(owner_references=[Mock(kind="Job", uid="real-job")]))
    owned.metadata.name = "owned"
    spoofed = Mock(metadata=Mock(owner_references=[Mock(kind="Job", uid="other-job")]))
    spoofed.metadata.name = "unrelated"
    core.list_namespaced_pod.return_value = Mock(items=[spoofed, owned])
    core.read_namespaced_pod_log.return_value = "training metrics"
    monkeypatch.setattr(training.client, "BatchV1Api", lambda api: batch)
    monkeypatch.setattr(training.client, "CoreV1Api", lambda api: core)
    logs = await service.logs("run", 42)
    assert logs["logs"] == [{"pod": "owned", "text": "training metrics"}]
    core.read_namespaced_pod_log.assert_awaited_once_with("owned", "inference-training", container="trainer", tail_lines=42, limit_bytes=131072, timestamps=True, _request_timeout=5)
    store.db.close()


def test_explicit_promotion_uses_result_identity_and_keeps_existing_deployment(tmp_path, monkeypatch):
    from api import main

    store = DataStore(":memory:", ArtifactStore(str(tmp_path)))
    service = TrainingService(store, main.k8s_client)
    version = dataset(store)
    request = training_request(version["id"])
    run = {"id": "run", "job_name": "ft-run", "request": request.model_dump(), "dataset": version, "status": "completed"}
    run["result"] = result_for(run)
    store.save("run", "run", run)
    inspector = Mock()
    inspector.inspect.return_value = ModelArchitectureSpecs(num_params=494032768, num_hidden_layers=24, hidden_size=896,
                                                            num_attention_heads=14, num_key_value_heads=2)
    reconciler = Mock()
    reconciler.get_deployment_status = AsyncMock(return_value={"model_id": "existing/model"})
    reconciler.deploy_model = AsyncMock()
    monkeypatch.setattr(main, "reconciler", reconciler)
    monkeypatch.setattr(main, "sla_planner", SLAPlanner(inspector=inspector))
    main.app.dependency_overrides[data_store] = lambda: store
    main.app.dependency_overrides[training_service] = lambda: service
    try:
        # No lifespan: avoid mutating unrelated deployment reconciliation state.
        client = TestClient(main.app)
        headers = {"Authorization": "Bearer test-token"}
        payload = {"deployment_name": "candidate", "max_model_len": 1024}
        assert client.post("/api/v1/training/runs/run/promote", json=payload, headers=headers).status_code == 409
        reconciler.deploy_model.assert_not_called()
        reconciler.get_deployment_status.return_value = None
        response = client.post("/api/v1/training/runs/run/promote", json=payload, headers=headers)
        assert response.status_code == 202
        plan = reconciler.deploy_model.call_args.args[0]
        assert plan["model_id"] == run["result"]["model_id"]
        assert plan["model_revision"] == run["result"]["model_revision"]
        assert client.post("/api/v1/training/runs/run/promote", json=payload, headers=headers).status_code == 202
        assert reconciler.deploy_model.call_count == 1
        assert client.post("/api/v1/training/runs/run/promote", json={"deployment_name": "other"}, headers=headers).status_code == 409
    finally:
        main.app.dependency_overrides.pop(data_store, None)
        main.app.dependency_overrides.pop(training_service, None)
        store.db.close()
