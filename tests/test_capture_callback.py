"""Exercise callback compatibility boundaries without installing the gateway SDK."""

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import httpx
import pytest


@pytest.fixture
def callback(monkeypatch):
    module = ModuleType("litellm.integrations.custom_logger")
    module.CustomLogger = object
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", module)
    spec = importlib.util.spec_from_file_location("capture_test", Path(__file__).resolve().parents[1] / "gateway/data_capture.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    monkeypatch.setenv("DATA_CAPTURE_ENABLED", "1")
    monkeypatch.setenv("DATA_CAPTURE_CONTENT_ENABLED", "1")
    monkeypatch.setenv("DATA_CAPTURE_URL", "http://capture/api/v1/data/events")
    monkeypatch.setenv("DATA_CAPTURE_TOKEN", "secret")
    return value


def inputs():
    kwargs = {"litellm_call_id": "request", "model": "test", "messages": [{"role": "user", "content": "email user@example.com"}],
              "litellm_params": {"metadata": {"training_consent": True, "conversation_id": "conversation", "secret": "never-export"}}}
    response = {"choices": [{"finish_reason": "stop", "message": {"content": "Answer", "role": "assistant"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    start = datetime.now(UTC)
    return kwargs, response, start, start + timedelta(seconds=1)


def test_terminal_stream_only_and_content_opt_in(callback, monkeypatch):
    kwargs, response, start, end = inputs()
    kwargs["stream"] = True
    assert callback.capture_payload(kwargs, response, start, end, "success") is None
    kwargs["complete_streaming_response"] = response
    payload = callback.capture_payload(kwargs, response, start, end, "success")
    assert payload["messages"][-1]["content"] == "Answer"
    assert payload["input_tokens"] == 10
    assert "user@example.com" not in json.dumps(payload)
    assert "never-export" not in json.dumps(payload)
    response["choices"][0]["finish_reason"] = "length"
    assert callback.capture_payload(kwargs, response, start, end, "success")["outcome"] == "incomplete"
    monkeypatch.setenv("DATA_CAPTURE_CONTENT_ENABLED", "0")
    assert callback.capture_payload(kwargs, response, start, end, "success")["messages"] == []
    kwargs["no-log"] = True
    assert callback.capture_payload(kwargs, response, start, end, "success") is None


@pytest.mark.asyncio
async def test_delivery_retries_and_does_not_expose_payload(callback, monkeypatch, caplog):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503 if len(attempts) < 3 else 202)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(callback.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    capture = callback.DataCapture()
    await capture.async_log_success_event(*inputs())
    await asyncio.wait_for(capture.queue.join(), timeout=3)
    assert capture.delivered == 1
    assert len(attempts) == 3
    assert attempts[-1].headers["authorization"] == "Bearer secret"
    assert "user@example.com" not in caplog.text
    capture.worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await capture.worker


@pytest.mark.asyncio
async def test_full_queue_drops_event_without_raising(callback):
    capture = callback.DataCapture()
    capture.queue = asyncio.Queue(maxsize=1)
    capture.queue.put_nowait(b"already queued")
    capture.worker = Mock()
    capture.worker.done.return_value = False
    await capture.async_log_success_event(*inputs())
    assert capture.dropped == 1
