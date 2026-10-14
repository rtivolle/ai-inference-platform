import json
import time

import httpx
import pytest
from api.metrics import MetricsUnavailableError, UsageMetrics


@pytest.mark.asyncio
async def test_metrics_preserve_zero_gaps_and_cache():
    requests = []
    now = time.time()

    def respond(request):
        requests.append(request)
        assert 'namespace="dynamo-workloads"' in request.url.params["query"]
        assert request.headers["authorization"] == "Bearer monitoring-test-token"
        history = request.url.path.endswith("query_range")
        result = {"metric": {}, "values": [[now - 15, "0"], [now, "NaN"]]} if history else {"metric": {}, "value": [now, "0"]}
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix" if history else "vector", "result": [result]}})

    metrics = UsageMetrics("https://prometheus.invalid", "monitoring-test-token", httpx.MockTransport(respond))
    snapshot = await metrics.snapshot()
    assert snapshot["status"] == "ready"
    assert snapshot["metrics"]["request_rate"]["value"] == 0
    assert snapshot["metrics"]["request_rate"]["history"][-1][1] is None
    assert await metrics.snapshot() == snapshot
    assert len(requests) == 16
    json.dumps(snapshot, allow_nan=False)


@pytest.mark.asyncio
async def test_missing_series_and_missing_source_are_not_zero():
    async def respond(request):
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    empty = await UsageMetrics("https://prometheus.invalid", transport=httpx.MockTransport(respond)).snapshot()
    assert empty["status"] == "partial"
    assert all(metric["value"] is None for metric in empty["metrics"].values())
    missing = await UsageMetrics(base_url="").snapshot()
    assert missing["status"] == "not_configured"
    assert all(metric["value"] is None for metric in missing["metrics"].values())


@pytest.mark.asyncio
async def test_monitoring_failure_is_explicit_and_drain_fails_closed():
    metrics = UsageMetrics("https://prometheus.invalid", transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    assert (await metrics.snapshot())["status"] == "unavailable"
    with pytest.raises(MetricsUnavailableError):
        await metrics.running_requests("model")
    with pytest.raises(MetricsUnavailableError):
        await UsageMetrics(base_url="").running_requests("model")


@pytest.mark.asyncio
async def test_drain_requires_a_fresh_model_scoped_sample():
    def respond(request):
        query = request.url.params["query"]
        assert 'model_name="org/model"' in query
        assert "timestamp(" in query and "< 60" in query
        return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [time.time(), "0"]}]}})

    metrics = UsageMetrics("https://prometheus.invalid", transport=httpx.MockTransport(respond))
    assert await metrics.running_requests("org/model") == 0
    metrics.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "success", "data": {"result": []}}))
    with pytest.raises(MetricsUnavailableError):
        await metrics.running_requests("org/model")
    metrics.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "success", "warnings": ["partial response"], "data": {"result": [{"value": [time.time(), "0"]}]}}))
    with pytest.raises(MetricsUnavailableError, match="partial"):
        await metrics.running_requests("org/model")


def test_metrics_endpoint_requires_auth_and_valid_windows(api_client):
    assert api_client.get("/api/v1/metrics/usage").status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    assert api_client.get("/api/v1/metrics/usage?window_minutes=6", headers=headers).status_code == 422
    assert api_client.get("/api/v1/metrics/usage", headers=headers).json()["status"] == "not_configured"
