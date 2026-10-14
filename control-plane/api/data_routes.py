"""Authenticated capture, review, and immutable dataset export endpoints."""

import hmac
import os
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials
from starlette.concurrency import run_in_threadpool

from .auth import security_bearer, verify_auth
from .data_models import CaptureEvent, DatasetRequest, ReviewRequest
from .data_store import ArtifactStore, DataStore

router = APIRouter(prefix="/api/v1/data", tags=["Data"])


@lru_cache(maxsize=1)
def data_store() -> DataStore:
    if os.getenv("DATA_WORKFLOW_ENABLED", "0") != "1":
        raise HTTPException(503, "Data workflow is disabled")
    path = os.getenv("DATA_DB_PATH", "")
    if not path:
        raise HTTPException(503, "DATA_DB_PATH must point to persistent storage")
    artifacts = ArtifactStore(os.getenv("DATA_ARTIFACT_ROOT", "/var/lib/inference-data/artifacts"), os.getenv("DATA_S3_BUCKET", ""), os.getenv("DATA_S3_ENDPOINT") or None)
    try:
        retention_days = int(os.getenv("DATA_EVENT_RETENTION_DAYS", "30"))
        if not 1 <= retention_days <= 365:
            raise ValueError
    except ValueError as error:
        raise HTTPException(503, "DATA_EVENT_RETENTION_DAYS must be an integer between 1 and 365") from error
    return DataStore(path, artifacts, retention_days)


async def verify_capture(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security_bearer)] = None):
    token = os.getenv("DATA_CAPTURE_TOKEN", "")
    if not token or not credentials or not hmac.compare_digest(token.encode(), credentials.credentials.encode()):
        raise HTTPException(401, "A valid capture token is required", headers={"WWW-Authenticate": "Bearer"})


@router.post("/events", dependencies=[Depends(verify_capture)], status_code=202)
async def capture_event(event: CaptureEvent, store: Annotated[DataStore, Depends(data_store)]):
    return await run_in_threadpool(store.capture, event)


@router.post("/examples", dependencies=[Depends(verify_auth)], status_code=201)
async def import_example(event: CaptureEvent, store: Annotated[DataStore, Depends(data_store)]):
    """Import a consented text example; it still requires a separate review."""
    return await run_in_threadpool(store.capture, event)


@router.get("/events", dependencies=[Depends(verify_auth)])
async def list_events(store: Annotated[DataStore, Depends(data_store)], limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    return await run_in_threadpool(store.list, "event", limit, offset)


@router.get("/events/{request_id}", dependencies=[Depends(verify_auth)])
async def get_event(request_id: str, store: Annotated[DataStore, Depends(data_store)]):
    return await run_in_threadpool(store.get, "event", request_id)


@router.post("/events/{request_id}/review")
async def review_event(request_id: str, review: ReviewRequest, store: Annotated[DataStore, Depends(data_store)], identity: Annotated[dict, Depends(verify_auth)]):
    return await run_in_threadpool(store.review, request_id, review, identity["username"])


@router.post("/datasets", dependencies=[Depends(verify_auth)], status_code=201)
async def create_dataset(payload: DatasetRequest, store: Annotated[DataStore, Depends(data_store)]):
    return await run_in_threadpool(store.build, payload)


@router.get("/datasets", dependencies=[Depends(verify_auth)])
async def list_datasets(store: Annotated[DataStore, Depends(data_store)], limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    return await run_in_threadpool(store.list, "dataset", limit, offset)


@router.get("/datasets/{dataset_id}", dependencies=[Depends(verify_auth)])
async def get_dataset(dataset_id: str, store: Annotated[DataStore, Depends(data_store)]):
    return await run_in_threadpool(store.get, "dataset", dataset_id)


@router.get("/datasets/{dataset_id}/export", dependencies=[Depends(verify_auth)])
async def export_dataset(dataset_id: str, store: Annotated[DataStore, Depends(data_store)], split: Literal["train", "validation", "test"] = "train"):
    body = await run_in_threadpool(store.export, dataset_id, split)
    return Response(body, media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="{split}.jsonl"'})
