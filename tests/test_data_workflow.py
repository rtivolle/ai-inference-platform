"""Data boundary, immutable dataset, and mocked training lifecycle checks."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from api.data_models import CaptureEvent, DatasetRequest, ReviewRequest, TrainingRequest
from api.data_store import ArtifactStore, DataError, DataStore, canonical, digest
from api.training import TrainingService, render_training_job, training_memory_gb
from starlette.testclient import TestClient


def event(identifier="request-1", **updates):
    return CaptureEvent.model_validate({"request_id": identifier, "conversation_id": identifier, "model": "test/model", "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
                                       "outcome": "success", "latency_ms": 100, "consent": True,
                                       "messages": [{"role": "user", "content": f"Question {identifier}"}, {"role": "assistant", "content": "A reviewed answer"}], **updates})


@pytest.fixture
def store(tmp_path):
    value = DataStore(str(tmp_path / "metadata.sqlite3"), ArtifactStore(str(tmp_path / "artifacts")))
    yield value
    value.db.close()


def dataset(store, count=100):
    identifiers = []
    for index in range(count):
        value = event(str(index))
        store.capture(value)
        store.review(value.request_id, ReviewRequest(status="approved"), "reviewer")
        identifiers.append(value.request_id)
    return store.build(DatasetRequest(name="reviewed", event_ids=identifiers))


def training_request(dataset_id):
    return TrainingRequest(dataset_id=dataset_id, base_revision="a" * 40, output_model_id="example/trained-run")


def result_for(run, **updates):
    return {"run_id": run["id"], "dataset_manifest_sha256": run["dataset"]["manifest_sha256"], "request_sha256": digest(canonical(run["request"])),
            "model_id": run["request"]["output_model_id"], "model_revision": "b" * 40,
            "base_validation_loss": 2.0, "candidate_validation_loss": 1.5, "base_test_loss": 2.1, "candidate_test_loss": 1.6, **updates}


def test_capture_requires_consent_and_redacts_before_storage(store):
    value = event(messages=[{"role": "user", "content": "email me at user@example.com Bearer secret-token"}, {"role": "assistant", "content": "sk-1234567890"}])
    store.capture(value)
    saved = canonical(store.get("event", value.request_id)).decode()
    assert "user@example.com" not in saved
    assert "secret-token" not in saved
    assert "sk-1234567890" not in saved
    store.capture(event("metadata-only", consent=False))
    assert store.get("event", "metadata-only")["messages"] == []
    with pytest.raises(DataError, match="consented"):
        store.review("metadata-only", ReviewRequest(status="approved"), "reviewer")


def test_capture_idempotent_across_threads_and_conflicting_replays(store):
    value = event()
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(store.capture, [value] * 8))
    assert sum(not response["duplicate"] for response in responses) == 1
    store.review(value.request_id, ReviewRequest(status="approved"), "reviewer")
    assert store.capture(value)["duplicate"]
    assert store.get("event", value.request_id)["review"]["status"] == "approved"
    with pytest.raises(DataError, match="different event"):
        store.capture(value.model_copy(update={"latency_ms": 123}))


def test_expired_events_are_removed_without_mutating_snapshots(store):
    version = dataset(store, 1)
    saved = store.get("event", "0")
    saved["received_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    store.save("event", "0", saved)
    assert store.list("event") == []
    split = version["provenance"][0]["split"]
    assert store.export(version["id"], split)


def test_s3_artifacts_use_conditional_writes_and_checked_keys(monkeypatch):
    import boto3
    from botocore.response import StreamingBody
    from botocore.stub import Stubber

    s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
    artifacts = ArtifactStore("/unused", bucket="test-artifacts")
    monkeypatch.setattr(artifacts, "client", lambda: s3)
    with Stubber(s3) as stub:
        stub.add_response("put_object", {}, {"Bucket": "test-artifacts", "Key": "datasets/version/train.jsonl", "Body": b"example\n", "ContentType": "application/octet-stream", "IfNoneMatch": "*"})
        artifacts.put("datasets/version/train.jsonl", b"example\n")
        stub.add_response("get_object", {"Body": StreamingBody(BytesIO(b"example\n"), 8)}, {"Bucket": "test-artifacts", "Key": "datasets/version/train.jsonl"})
        assert artifacts.get("datasets/version/train.jsonl") == b"example\n"
    for key in ("../secret", "/absolute", "datasets/../secret"):
        with pytest.raises(ValueError):
            artifacts.get(key)


@pytest.mark.parametrize("updates", [{"outcome": "failure"}, {"outcome": "incomplete"}, {"messages": [{"role": "user", "content": "No answer"}]}])
def test_only_complete_examples_can_be_approved(store, updates):
    value = event(**updates)
    store.capture(value)
    with pytest.raises(DataError):
        store.review(value.request_id, ReviewRequest(status="approved"), "reviewer")


def test_snapshot_is_immutable_and_survives_restart(store):
    store.capture(event())
    with pytest.raises(DataError, match="approved"):
        store.build(DatasetRequest(name="pending", event_ids=["request-1"]))
    store.review("request-1", ReviewRequest(status="approved", corrected_response="Corrected answer"), "reviewer")
    version = store.build(DatasetRequest(name="version-1", event_ids=["request-1"]))
    split = version["provenance"][0]["split"]
    body = store.export(version["id"], split)
    assert json.loads(body)["messages"][-1]["content"] == "Corrected answer"
    store.review("request-1", ReviewRequest(status="rejected"), "reviewer-2")
    reopened = DataStore(store.db.execute("PRAGMA database_list").fetchone()[2], store.artifacts)
    try:
        assert reopened.export(version["id"], split) == body
    finally:
        reopened.db.close()
    path = store.artifacts.root / version["files"][split]["key"]
    path.write_bytes(b"tampered")
    with pytest.raises(DataError, match="checksum"):
        store.export(version["id"], split)


def test_duplicate_content_connects_conversations_before_split_assignment(store):
    messages = [{"role": "user", "content": "Shared question"}, {"role": "assistant", "content": "Shared answer"}]
    for identifier, conversation, content in [("a", "conversation-a", messages), ("b", "conversation-b", messages), ("c", "conversation-b", [{"role": "user", "content": "Unique followup"}, {"role": "assistant", "content": "Unique answer"}])]:
        store.capture(event(identifier, conversation_id=conversation, messages=content))
        store.review(identifier, ReviewRequest(status="approved"), "reviewer")
    version = store.build(DatasetRequest(name="dedup", event_ids=["a", "b", "c"]))
    assert len({item["split"] for item in version["provenance"]}) == 1
    assert sum(item["count"] for item in version["files"].values()) == 2
    repeated = store.build(DatasetRequest(name="same", event_ids=["c", "b", "a"]))
    assert [item["split"] for item in version["provenance"]] == [item["split"] for item in repeated["provenance"]]
    assert {key: value["sha256"] for key, value in version["files"].items()} == {key: value["sha256"] for key, value in repeated["files"].items()}


@pytest.mark.asyncio
async def test_training_launch_render_cancel_and_evaluation_gate(store, mock_k8s):
    version = dataset(store)
    service = TrainingService(store, mock_k8s)
    request = training_request(version["id"])
    inventory = await mock_k8s.get_cluster_gpu_inventory()
    run = await service.launch(request, inventory, "admin")
    assert run["status"] == "queued"
    manifest = render_training_job(run, "artifacts", None, run["gpu_sku"])
    pod = manifest["spec"]["template"]["spec"]
    assert pod["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert not pod["automountServiceAccountToken"]
    assert manifest["spec"]["backoffLimit"] == 0
    assert "provenance" not in json.loads(pod["containers"][0]["env"][0]["value"])["dataset"]
    with pytest.raises(DataError, match="completed"):
        service.validate_promotion(run)
    store.artifacts.put(f"runs/{run['id']}/result.json", canonical(result_for(run)))
    service.load_result(run)
    service.validate_promotion(run)
    run["result"]["candidate_test_loss"] = 3.0
    with pytest.raises(DataError, match="Candidate"):
        service.validate_promotion(run)
    cancelled = await service.cancel(run["id"])
    assert cancelled["status"] == "cancelled"
    assert training_memory_gb(request.model_copy(update={"max_length": 4096})) > training_memory_gb(request)


@pytest.mark.asyncio
async def test_training_rejects_missing_splits_and_unavailable_capacity(store, mock_k8s):
    service = TrainingService(store, mock_k8s)
    small = dataset(store, 1)
    with pytest.raises(DataError, match="nonempty"):
        await service.launch(training_request(small["id"]), {}, "admin")
    full = dataset(store)
    with pytest.raises(DataError, match="No available GPU"):
        await service.launch(training_request(full["id"]), {"gpu_pools": [{"available_count": 1, "vram_gb": 8}]}, "admin")


@pytest.mark.asyncio
async def test_invalid_training_result_cannot_complete_run(store, mock_k8s):
    version = dataset(store)
    service = TrainingService(store, mock_k8s)
    run = await service.launch(training_request(version["id"]), await mock_k8s.get_cluster_gpu_inventory(), "admin")
    for updates in [{"dataset_manifest_sha256": "wrong"}, {"candidate_test_loss": float("nan")}, {"model_revision": "main"}, {"model_id": "other/repo"}]:
        path = store.artifacts.root / f"runs/{run['id']}/result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(result_for(run, **updates)))
        with pytest.raises(DataError):
            service.load_result(run)
    assert run["status"] == "queued"


@pytest.fixture
def data_client(store, monkeypatch):
    from api.data_routes import data_store
    from api.main import app
    from api.training_routes import training_service

    monkeypatch.setenv("DATA_CAPTURE_TOKEN", "capture-token")
    app.dependency_overrides[data_store] = lambda: store
    training_service.cache_clear()
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.pop(data_store, None)
    training_service.cache_clear()


def test_api_capture_key_cannot_read_or_review_and_admin_cannot_capture(data_client):
    payload = event().model_dump(mode="json")
    capture = {"Authorization": "Bearer capture-token"}
    admin = {"Authorization": "Bearer test-token"}
    assert data_client.post("/api/v1/data/events", json=payload, headers=admin).status_code == 401
    assert data_client.post("/api/v1/data/events", json=payload, headers=capture).status_code == 202
    assert data_client.get("/api/v1/data/events", headers=capture).status_code == 401
    assert data_client.post("/api/v1/data/events/request-1/review", json={"status": "approved"}, headers=capture).status_code == 401
    response = data_client.post("/api/v1/data/events/request-1/review", json={"status": "approved"}, headers=admin)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert data_client.get("/api/v1/data/events?limit=999999", headers=admin).status_code == 422
    assert data_client.post("/api/v1/data/events", content=b" " * 131073, headers=capture).status_code == 413
    chunks = (chunk for chunk in [b" " * 65536, b" " * 65537])
    assert data_client.post("/api/v1/data/events", content=chunks, headers=capture).status_code == 413


@pytest.mark.asyncio
async def test_job_expiry_recovers_result_and_failure_state(store, mock_k8s, monkeypatch):
    from api import training
    from kubernetes_asyncio.client.rest import ApiException

    service = TrainingService(store, mock_k8s)
    run = await service.launch(training_request(dataset(store)["id"]), await mock_k8s.get_cluster_gpu_inventory(), "admin")
    store.artifacts.put(f"runs/{run['id']}/result.json", canonical(result_for(run)))
    mock_k8s.mock_mode = False
    batch = AsyncMock()
    batch.read_namespaced_job_status.side_effect = ApiException(status=404)
    monkeypatch.setattr(training.client, "BatchV1Api", lambda api: batch)
    completed = await service.status(run["id"])
    assert completed["status"] == "completed"
    assert store.get("run", run["id"])["result"]["model_revision"] == "b" * 40
