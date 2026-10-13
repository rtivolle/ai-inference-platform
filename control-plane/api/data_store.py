"""Durable single-replica metadata and immutable dataset artifacts.

SQLite is intentionally separate from LiteLLM's internal database. All operations
run in the threadpool through the API; a lock keeps review/build transactions atomic.
"""

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .data_models import CaptureEvent, DatasetRequest, ReviewRequest


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def redact(text: str) -> str:
    """Conservative baseline, not a complete PII detector; human review is required."""
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED_KEY]", text)
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", text)
    return text


class DataError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class ArtifactStore:
    def __init__(self, root: str, bucket: str = "", endpoint: str | None = None):
        self.root = Path(root)
        self.bucket = bucket
        self.endpoint = endpoint

    def client(self):
        import boto3
        from botocore.config import Config

        return boto3.client("s3", endpoint_url=self.endpoint, config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3}))

    @staticmethod
    def valid_key(key: str):
        if not re.fullmatch(r"[a-zA-Z0-9_./-]+", key) or any(part in ("", ".", "..") for part in key.split("/")):
            raise ValueError("Invalid artifact key")

    def put(self, key: str, body: bytes):
        self.valid_key(key)
        if self.bucket:
            self.client().put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/octet-stream", IfNoneMatch="*")
        else:
            path = self.root / key
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output:
                output.write(body)

    def get(self, key: str) -> bytes:
        self.valid_key(key)
        if self.bucket:
            return self.client().get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return (self.root / key).read_bytes()


class DataStore:
    def __init__(self, path: str, artifacts: ArtifactStore):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(kind,id))")
        self.db.commit()
        self.lock = threading.RLock()
        self.artifacts = artifacts

    def get(self, kind: str, identifier: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
            if not row:
                raise DataError(404, f"{kind} was not found")
            return json.loads(row[0])

    def save(self, kind: str, identifier: str, value: dict):
        with self.lock, self.db:
            self.db.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body", (kind, identifier, canonical(value).decode()))

    def list(self, kind: str, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT body FROM records WHERE kind=? ORDER BY rowid DESC LIMIT ? OFFSET ?", (kind, limit, offset)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def capture(self, event: CaptureEvent) -> dict:
        value = event.model_dump(mode="json")
        for message in value["messages"]:
            message["content"] = redact(message["content"])
        fingerprint = digest(canonical(value))
        with self.lock:
            try:
                previous = self.get("event", event.request_id)
            except DataError:
                previous = None
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise DataError(409, "request_id already contains a different event")
                return {"request_id": event.request_id, "duplicate": True}
            value.update(fingerprint=fingerprint, review={"status": "pending"}, received_at=datetime.now(UTC).isoformat())
            self.save("event", event.request_id, value)
        return {"request_id": event.request_id, "duplicate": False}

    def review(self, identifier: str, request: ReviewRequest, reviewer: str) -> dict:
        with self.lock:
            value = self.get("event", identifier)
            messages = value["messages"]
            if request.status == "approved":
                if not value["consent"] or value["outcome"] != "success" or not messages or messages[-1]["role"] != "assistant" or not any(m["role"] == "user" for m in messages[:-1]):
                    raise DataError(409, "Only consented, complete user/assistant examples can be approved")
            review = request.model_dump()
            if review["corrected_response"]:
                review["corrected_response"] = redact(review["corrected_response"])
            review["note"] = redact(review["note"])
            review.update(reviewer=reviewer, reviewed_at=datetime.now(UTC).isoformat())
            value["review"] = review
            self.save("event", identifier, value)
            return value

    def build(self, request: DatasetRequest) -> dict:
        with self.lock:
            events = [self.get("event", identifier) for identifier in sorted(request.event_ids)]
            rows = []
            for event in events:
                if event["review"]["status"] != "approved":
                    raise DataError(409, "Every selected event must be approved")
                messages = [dict(message) for message in event["messages"]]
                if event["review"].get("corrected_response"):
                    messages[-1]["content"] = event["review"]["corrected_response"]
                rows.append((event, messages, digest(canonical(messages))))

            # Connect conversations sharing identical examples before assigning splits.
            # This prevents both conversation leakage and duplicate-content leakage.
            parents = {event["conversation_id"]: event["conversation_id"] for event in events}

            def group(key):
                while parents[key] != key:
                    parents[key] = parents[parents[key]]
                    key = parents[key]
                return key

            owners = {}
            for event, _, content_hash in rows:
                conversation = event["conversation_id"]
                if content_hash in owners:
                    left, right = sorted((group(conversation), group(owners[content_hash])))
                    parents[right] = left
                else:
                    owners[content_hash] = conversation

            splits = {"train": [], "validation": [], "test": []}
            provenance, seen = [], set()
            for event, messages, content_hash in rows:
                group_id = group(event["conversation_id"])
                score = int(digest(canonical([request.seed, group_id]))[:16], 16) / 2**64
                split = "test" if score < request.test_fraction else "validation" if score < request.test_fraction + request.validation_fraction else "train"
                provenance.append({"request_id": event["request_id"], "fingerprint": event["fingerprint"], "review": event["review"], "conversation_id": event["conversation_id"], "content_sha256": content_hash, "split": split, "deduplicated": content_hash in seen})
                if content_hash not in seen:
                    splits[split].append({"messages": messages})
                    seen.add(content_hash)

            identifier = str(uuid.uuid4())
            manifest = {"id": identifier, "name": request.name, "created_at": datetime.now(UTC).isoformat(), "configuration": request.model_dump(), "format": "chat-jsonl-v1", "files": {}, "provenance": provenance}
            for split, examples in splits.items():
                body = b"".join(canonical(example) + b"\n" for example in examples)
                key = f"datasets/{identifier}/{split}.jsonl"
                self.artifacts.put(key, body)
                manifest["files"][split] = {"key": key, "sha256": digest(body), "count": len(examples)}
            manifest_body = canonical(manifest)
            self.artifacts.put(f"datasets/{identifier}/manifest.json", manifest_body)
            manifest["manifest_sha256"] = digest(manifest_body)
            self.save("dataset", identifier, manifest)
            return manifest

    def export(self, identifier: str, split: str) -> bytes:
        dataset = self.get("dataset", identifier)
        artifact = dataset["files"][split]
        body = self.artifacts.get(artifact["key"])
        if digest(body) != artifact["sha256"]:
            raise DataError(409, "Dataset artifact failed checksum verification")
        return body
