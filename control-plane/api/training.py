"""Restart-safe Kubernetes training job lifecycle and evaluation gates."""

import asyncio
import json
import math
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException
from starlette.concurrency import run_in_threadpool

from .data_models import TrainingRequest
from .data_store import DataError, DataStore, canonical, digest

NAMESPACE = "inference-training"


def training_memory_gb(request: TrainingRequest) -> float:
    """Conservative planning estimate for this one Qwen model, with checkpointing.

Includes BF16 base weights, FP32 adapter/gradient/Adam state, estimated retained
activations, CUDA/merge buffers, and headroom. This is not the inference KV formula.
Actual peak memory must be checked in the GPU smoke test.
"""
    weights = 494_032_768 * 2
    adapter_parameters = 24 * request.lora_rank * (2 * (896 + 896) + 2 * (896 + 128) + 3 * (896 + 4864))
    optimizer = adapter_parameters * 16
    activations = request.max_length * 896 * 24 * 16
    return round((weights + optimizer + activations + 4 * 1024**3) * 1.25 / 1024**3, 2)


def render_training_job(run: dict, bucket: str, endpoint: str | None, gpu_sku: str) -> dict:
    dataset = {key: run["dataset"][key] for key in ("id", "files", "manifest_sha256")}
    config = {"run_id": run["id"], "request": run["request"], "dataset": dataset, "bucket": bucket, "endpoint": endpoint}
    env: list[dict[str, Any]] = [{"name": "TRAINING_CONFIG", "value": canonical(config).decode()},
           {"name": "HF_HOME", "value": "/work/hf"}]
    for variable, key in [("AWS_ACCESS_KEY_ID", "access-key-id"), ("AWS_SECRET_ACCESS_KEY", "secret-access-key"), ("HF_TOKEN", "hf-token")]:
        env.append({"name": variable, "valueFrom": {"secretKeyRef": {"name": "training-runtime", "key": key}}})
    env.append({"name": "AWS_DEFAULT_REGION", "value": os.getenv("AWS_DEFAULT_REGION", "us-east-1")})
    spec = {
        "serviceAccountName": "training-worker", "automountServiceAccountToken": False, "restartPolicy": "Never",
        "nodeSelector": {"nvidia.com/gpu.product": gpu_sku},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
        "containers": [{"name": "trainer", "image": run["image"], "env": env,
                        "resources": {"requests": {"cpu": "2", "memory": "16Gi", "nvidia.com/gpu": "1"}, "limits": {"cpu": "8", "memory": "32Gi", "nvidia.com/gpu": "1"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "work", "mountPath": "/work"}, {"name": "shm", "mountPath": "/dev/shm"}]}],
        "volumes": [{"name": "work", "emptyDir": {"sizeLimit": "40Gi"}}, {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}}],
    }
    if os.getenv("TRAINING_IMAGE_PULL_SECRET"):
        spec["imagePullSecrets"] = [{"name": os.environ["TRAINING_IMAGE_PULL_SECRET"]}]
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": run["job_name"], "namespace": NAMESPACE, "labels": {"app.kubernetes.io/name": "inference-trainer", "inference.platform/run-id": run["id"]}},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 21600, "ttlSecondsAfterFinished": 604800, "template": {"metadata": {"labels": {"inference.platform/run-id": run["id"]}}, "spec": spec}}}


class TrainingService:
    def __init__(self, store: DataStore, k8s):
        self.store = store
        self.k8s = k8s
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, identifier: str):
        # The metadata backend is intentionally deployed with one API worker.
        return self.locks.setdefault(identifier, asyncio.Lock())

    async def launch(self, request: TrainingRequest, inventory: dict, creator: str) -> dict:
        dataset = await run_in_threadpool(self.store.get, "dataset", request.dataset_id)
        if any(dataset["files"][split]["count"] == 0 for split in ("train", "validation", "test")):
            raise DataError(409, "Training requires nonempty train, validation, and test splits")
        image = os.getenv("TRAINING_IMAGE", "")
        if not self.k8s.mock_mode:
            if not self.store.artifacts.bucket:
                raise DataError(503, "Live training requires DATA_S3_BUCKET")
            if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image):
                raise DataError(503, "TRAINING_IMAGE must be pinned by digest")
            owner = os.getenv("TRAINING_HF_OWNER", "")
            if not owner or request.output_model_id.split("/")[0] != owner:
                raise DataError(422, "Output repository must belong to TRAINING_HF_OWNER")
        estimated = training_memory_gb(request)
        pool = next((pool for pool in inventory.get("gpu_pools", []) if pool["available_count"] >= 1 and pool["vram_gb"] >= max(16, estimated)), None)
        if not pool:
            raise DataError(409, "No available GPU has the training memory headroom (minimum 16 GiB)")
        identifier = str(uuid.uuid4())
        run = {"id": identifier, "job_name": f"ft-{identifier}", "status": "submitting", "created_at": datetime.now(UTC).isoformat(), "creator": creator,
               "request": request.model_dump(), "dataset": dataset, "image": image or "mock-trainer", "estimated_vram_gb": estimated, "gpu_sku": pool["gpu_sku"]}
        await run_in_threadpool(self.store.save, "run", identifier, run)
        manifest = render_training_job(run, self.store.artifacts.bucket, self.store.artifacts.endpoint, pool["gpu_sku"])
        try:
            if not self.k8s.mock_mode:
                async with client.ApiClient() as api:
                    await client.BatchV1Api(api).create_namespaced_job(NAMESPACE, manifest)  # type: ignore[arg-type]
            run["status"] = "queued"
            await run_in_threadpool(self.store.save, "run", identifier, run)
        except ApiException as error:
            run["status"] = "submission_unknown"
            await run_in_threadpool(self.store.save, "run", identifier, run)
            raise DataError(503, "Job submission could not be confirmed; inspect the saved run before retrying") from error
        return run

    async def status(self, identifier: str) -> dict:
        run = await run_in_threadpool(self.store.get, "run", identifier)
        if self.k8s.mock_mode or run["status"] in ("completed", "failed", "cancelled", "promoted"):
            return run
        try:
            async with client.ApiClient() as api:
                job = await client.BatchV1Api(api).read_namespaced_job_status(run["job_name"], NAMESPACE)
            conditions = {condition.type for condition in job.status.conditions or [] if condition.status == "True"}
            if "Complete" in conditions:
                try:
                    await run_in_threadpool(self.load_result, run)
                except (BotoCoreError, ClientError, FileNotFoundError) as artifact_error:
                    raise DataError(503, "Completed job results are unavailable; retry after artifact storage recovers") from artifact_error
            elif "Failed" in conditions:
                run["status"] = "failed"
            else:
                run["status"] = "running" if job.status.active else "queued"
        except ApiException as error:
            if error.status != 404:
                raise DataError(503, "Training job status is unavailable") from error
            # A completed Job can expire before the next read. Recover durable results.
            try:
                await run_in_threadpool(self.load_result, run)
            except (FileNotFoundError, DataError):
                run["status"] = "job_missing"
            except Exception as artifact_error:
                if isinstance(artifact_error, ClientError) and artifact_error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                    run["status"] = "job_missing"
                else:
                    raise DataError(503, "Training result storage is unavailable") from artifact_error
        if run["status"] in ("queued", "running"):
            try:
                progress = await run_in_threadpool(self.store.artifacts.get, f"runs/{run['id']}/progress.json")
                progress = json.loads(progress)
                if progress.get("run_id") == run["id"]:
                    run["progress"] = progress
            except (FileNotFoundError, BotoCoreError, ClientError, ValueError):
                # Progress is optional; durable final results remain mandatory.
                pass
        await run_in_threadpool(self.store.save, "run", identifier, run)
        return run

    def load_result(self, run: dict):
        result = json.loads(self.store.artifacts.get(f"runs/{run['id']}/result.json"))
        if result.get("run_id") != run["id"] or result.get("dataset_manifest_sha256") != run["dataset"]["manifest_sha256"] or result.get("request_sha256") != digest(canonical(run["request"])):
            raise DataError(409, "Training result provenance does not match the run")
        if result.get("model_id") != run["request"]["output_model_id"] or not re.fullmatch(r"[a-f0-9]{40}", result.get("model_revision", "")):
            raise DataError(409, "Training result has no valid immutable model revision")
        for key in ("base_validation_loss", "candidate_validation_loss", "base_test_loss", "candidate_test_loss"):
            value = result.get(key)
            if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                raise DataError(409, "Training evaluation is missing or invalid")
        run["result"] = result
        run["status"] = "completed"

    async def cancel(self, identifier: str) -> dict:
        run = await self.status(identifier)
        if run["status"] in ("completed", "promoted", "failed", "cancelled"):
            raise DataError(409, "This run is already terminal")
        if not self.k8s.mock_mode:
            async with client.ApiClient() as api:
                try:
                    await client.BatchV1Api(api).delete_namespaced_job(run["job_name"], NAMESPACE, propagation_policy="Foreground")
                except ApiException as error:
                    if error.status != 404:
                        raise DataError(503, "Training cancellation failed") from error
        run["status"] = "cancelled"
        await run_in_threadpool(self.store.save, "run", identifier, run)
        return run

    async def logs(self, identifier: str, tail_lines: int = 100) -> dict:
        run = await run_in_threadpool(self.store.get, "run", identifier)
        if self.k8s.mock_mode:
            return {"status": "mock", "logs": [], "message": "Mock runs have no live trainer logs"}
        async with client.ApiClient() as api:
            batch = client.BatchV1Api(api)
            core = client.CoreV1Api(api)
            try:
                job = await batch.read_namespaced_job(run["job_name"], NAMESPACE)
            except ApiException as error:
                if error.status == 404:
                    return {"status": "expired", "logs": [], "training_history": run.get("result", {}).get("training_history", [])}
                raise DataError(503, "Trainer logs are unavailable") from error
            pods = await core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={run['job_name']}")
            owned = [pod for pod in pods.items if any(owner.kind == "Job" and owner.uid == job.metadata.uid for owner in pod.metadata.owner_references or [])]
            if not owned:
                return {"status": "pending", "logs": []}
            # backoffLimit=0 means one trainer pod; never expose unrelated containers.
            pod = owned[0]
            text = await core.read_namespaced_pod_log(pod.metadata.name, NAMESPACE, container="trainer", tail_lines=tail_lines,
                                                       limit_bytes=131072, timestamps=True, _request_timeout=5)
            return {"status": "ready", "logs": [{"pod": pod.metadata.name, "text": text}]}

    @staticmethod
    def validate_promotion(run: dict):
        result = run.get("result", {})
        if run["status"] != "completed" or not result:
            raise DataError(409, "Only completed, evaluated runs can be promoted")
        if result["candidate_validation_loss"] >= result["base_validation_loss"] or result["candidate_test_loss"] > result["base_test_loss"]:
            raise DataError(409, "Candidate must improve validation loss without regressing held-out test loss")
