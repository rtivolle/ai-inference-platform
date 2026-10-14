"""LiteLLM terminal-event callback. Disabled until explicitly configured.

This queue is bounded and best effort: process crashes can lose queued events.
Delivery failures never propagate into the inference request path.
"""

import asyncio
import json
import logging
import os
import re
from datetime import UTC

import httpx
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("inference_capture")


def redact(text):
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED_KEY]", text)
    return re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", text)


def as_dict(value):
    if isinstance(value, dict):
        return value
    return value.model_dump() if hasattr(value, "model_dump") else {}


def capture_payload(kwargs, response_obj, start_time, end_time, outcome):
    """Only allowlisted fields leave the gateway; never copy arbitrary metadata."""
    params = kwargs.get("litellm_params") or {}
    metadata = params.get("metadata") or {}
    if kwargs.get("no-log") or params.get("no-log"):
        return None
    request_id = kwargs.get("litellm_call_id")
    if not request_id:
        return None
    if kwargs.get("stream"):
        response_obj = kwargs.get("complete_streaming_response")
        if not response_obj and outcome == "success":
            return None  # Ignore intermediate chunks; only the final response is eligible.
    response = as_dict(response_obj)
    choices = response.get("choices") or []
    choice = as_dict(choices[0]) if choices else {}
    message = as_dict(choice.get("message"))
    consent = os.getenv("DATA_CAPTURE_CONTENT_ENABLED", "0") == "1" and metadata.get("training_consent") is True
    messages = []
    if consent and outcome == "success":
        if choice.get("finish_reason") != "stop" or not isinstance(message.get("content"), str) or message.get("tool_calls"):
            outcome = "incomplete"
        else:
            source = kwargs.get("messages") or []
            if any(m.get("role") not in ("system", "user", "assistant") or not isinstance(m.get("content"), str) or m.get("tool_calls") for m in source):
                outcome = "incomplete"
            else:
                messages = [{"role": m["role"], "content": redact(m["content"])} for m in source]
                messages.append({"role": "assistant", "content": redact(message["content"])})
    usage = as_dict(response.get("usage"))
    model_info = params.get("model_info") or {}
    return {
        "request_id": str(request_id), "conversation_id": str(metadata.get("conversation_id") or request_id),
        "model": str(kwargs.get("model") or "unknown"), "model_revision": model_info.get("revision"),
        "occurred_at": start_time.replace(tzinfo=UTC).isoformat() if start_time.tzinfo is None else start_time.isoformat(),
        "outcome": outcome, "latency_ms": max(0, (end_time - start_time).total_seconds() * 1000),
        "input_tokens": usage.get("prompt_tokens") or 0, "output_tokens": usage.get("completion_tokens") or 0,
        "consent": consent, "messages": messages,
    }


class DataCapture(CustomLogger):
    def __init__(self):
        super().__init__()
        self.queue = None
        self.worker = None
        self.delivered = self.dropped = self.failed = 0

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self.enqueue(kwargs, response_obj, start_time, end_time, "success")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self.enqueue(kwargs, response_obj, start_time, end_time, "failure")

    def enqueue(self, kwargs, response_obj, start_time, end_time, outcome):
        if os.getenv("DATA_CAPTURE_ENABLED", "0") != "1":
            return
        try:
            if not os.getenv("DATA_CAPTURE_URL") or not os.getenv("DATA_CAPTURE_TOKEN"):
                raise ValueError("Capture destination is not configured")
            payload = capture_payload(kwargs, response_obj, start_time, end_time, outcome)
            if payload is None:
                return
            body = json.dumps(payload).encode()
            if len(body) > 131072:
                raise ValueError("Capture payload exceeds the limit")
            if self.queue is None:
                self.queue = asyncio.Queue(maxsize=256)
            if self.worker is None or self.worker.done():
                self.worker = asyncio.create_task(self.deliver())
            self.queue.put_nowait(body)
        except (ValueError, TypeError, AttributeError, asyncio.QueueFull):
            self.dropped += 1
            logger.warning("capture_event_dropped total=%d", self.dropped)

    async def deliver(self):
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as http:
            while True:
                body = await self.queue.get()
                try:
                    for attempt in range(3):
                        try:
                            response = await http.post(os.environ["DATA_CAPTURE_URL"], content=body, headers={"Authorization": f"Bearer {os.environ['DATA_CAPTURE_TOKEN']}", "Content-Type": "application/json"})
                            response.raise_for_status()
                            self.delivered += 1
                            break
                        except httpx.HTTPError as error:
                            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
                            if attempt == 2 or (status and status < 500 and status != 429):
                                self.failed += 1
                                logger.warning("capture_delivery_failed total=%d status=%s", self.failed, status)
                                break
                            await asyncio.sleep(0.25 * 2**attempt)
                finally:
                    self.queue.task_done()


capture = DataCapture()
