"""Bounded, namespace-scoped Prometheus queries for live inference usage."""

import asyncio
import json
import math
import os
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx


class MetricsUnavailableError(RuntimeError):
    pass


class UsageMetrics:
    def __init__(self, base_url: str | None = None, token: str | None = None, transport=None):
        self.base_url = (base_url if base_url is not None else os.getenv("PROMETHEUS_URL", "")).rstrip("/")
        self.token = token if token is not None else os.getenv("PROMETHEUS_BEARER_TOKEN")
        self.transport = transport
        if self.base_url:
            url = urlsplit(self.base_url)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("PROMETHEUS_URL must be an HTTP(S) URL without credentials, query, or fragment.")
        self.namespace = "dynamo-workloads"
        self._cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def definitions(self, model_id: str | None = None) -> dict[str, tuple[str, str, str]]:
        labels = f"namespace={json.dumps(self.namespace)}"
        if model_id is not None:
            labels += f",model_name={json.dumps(model_id)}"

        def total(metric: str) -> str:
            return f"sum({metric}{{{labels}}})"

        def rate(metric: str) -> str:
            return f"sum(rate({metric}{{{labels}}}[2m]))"

        gpu = f'namespace={json.dumps(self.namespace)}'
        return {
            "request_rate": (rate("vllm:request_success_total"), "requests/s", "Successful requests"),
            "output_tokens": (rate("vllm:generation_tokens_total"), "tokens/s", "Output tokens"),
            "running_requests": (total("vllm:num_requests_running"), "requests", "Running requests"),
            "waiting_requests": (
                f'sum(vllm:num_requests_waiting{{{labels},job!=""}}) or '
                f'sum(vllm_num_requests_waiting{{{labels}}}) or sum(vllm:num_requests_waiting{{{labels},job=""}})',
                "requests", "Queued requests",
            ),
            "ttft_p95": (
                f"histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{{{labels}}}[2m])))",
                "seconds", "Time to first token · p95",
            ),
            "gpu_utilization": (f"avg(DCGM_FI_DEV_GPU_UTIL{{{gpu}}})", "%", "Workload GPU utilization"),
            "gpu_memory": (
                f"100 * sum(DCGM_FI_DEV_FB_USED{{{gpu}}}) / "
                f"(sum(DCGM_FI_DEV_FB_USED{{{gpu}}}) + sum(DCGM_FI_DEV_FB_FREE{{{gpu}}}))",
                "%", "Workload GPU memory used",
            ),
            "kv_cache": (
                f"100 * (max(vllm:kv_cache_usage_perc{{{labels}}}) or max(vllm:gpu_cache_usage_perc{{{labels},job!=\"\"}}) "
                f"or max(vllm_gpu_cache_usage_factor{{{labels}}}) or max(vllm:gpu_cache_usage_perc{{{labels},job=\"\"}}))",
                "%", "Highest KV cache usage",
            ),
        }

    async def query(self, http: httpx.AsyncClient, expression: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        endpoint = "query_range" if params else "query"
        response = await http.get(f"{self.base_url}/api/v1/{endpoint}", params={"query": expression, **(params or {})})
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success" or not isinstance(payload.get("data", {}).get("result"), list):
            raise MetricsUnavailableError("Prometheus rejected the monitoring query.")
        return payload

    @staticmethod
    def sample(point: list) -> list[float | None]:
        stamp, raw_value = point
        value = float(raw_value)
        return [float(stamp), value if math.isfinite(value) else None]

    async def snapshot(self, window_minutes: int = 15) -> dict[str, Any]:
        if window_minutes not in (5, 15, 60):
            raise ValueError("Unsupported metrics window.")
        async with self._lock:
            cached = self._cache.get(window_minutes)
            if cached and time.monotonic() - cached[0] < 10:
                return cached[1]
            data = await self._snapshot(window_minutes)
            self._cache[window_minutes] = (time.monotonic(), data)
            return data

    async def _snapshot(self, window_minutes: int) -> dict[str, Any]:
        now = time.time()
        result: dict[str, Any] = {
            "status": "not_configured" if not self.base_url else "ready",
            "source": "prometheus", "namespace": self.namespace,
            "collected_at": datetime.now(UTC).isoformat(), "window_minutes": window_minutes,
            "metrics": {},
        }
        definitions = self.definitions()
        for key, (_, unit, label) in definitions.items():
            result["metrics"][key] = {"label": label, "unit": unit, "value": None, "history": [], "status": "no_data"}
        if not self.base_url:
            return result

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        params = {"start": now - window_minutes * 60, "end": now, "step": 15 if window_minutes < 60 else 60}
        async with httpx.AsyncClient(timeout=5, headers=headers, transport=self.transport, limits=httpx.Limits(max_connections=8)) as http:
            async def fetch(key: str, expression: str):
                metric = result["metrics"][key]
                try:
                    current, history = await asyncio.gather(self.query(http, expression), self.query(http, expression, params))
                    if current["data"]["result"]:
                        metric["sample_time"], metric["value"] = self.sample(current["data"]["result"][0]["value"])
                    if history["data"]["result"]:
                        metric["history"] = [self.sample(point) for point in history["data"]["result"][0].get("values", [])]
                    metric["status"] = "ready" if metric["value"] is not None else "no_data"
                    if current.get("warnings") or history.get("warnings"):
                        metric["status"] = "partial"
                except (httpx.HTTPError, MetricsUnavailableError, KeyError, ValueError, TypeError):
                    metric["status"] = "unavailable"
            await asyncio.gather(*(fetch(key, expression) for key, (expression, _, _) in definitions.items()))
        statuses = [metric["status"] for metric in result["metrics"].values()]
        if all(status == "unavailable" for status in statuses):
            result["status"] = "unavailable"
        elif any(status != "ready" for status in statuses):
            result["status"] = "partial"
        return result

    async def running_requests(self, model_id: str) -> int:
        """Drain checks must fail closed when monitoring has no usable sample."""
        if not self.base_url:
            raise MetricsUnavailableError("Prometheus is not configured; active requests cannot be verified.")
        expression = self.definitions(model_id)["running_requests"][0]
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            async with httpx.AsyncClient(timeout=5, headers=headers, transport=self.transport) as http:
                payload = await self.query(http, expression)
            samples = payload["data"]["result"]
            if not samples:
                raise MetricsUnavailableError("No running-request metrics are available for this model.")
            _, value = self.sample(samples[0]["value"])
            if value is None or value < 0:
                raise MetricsUnavailableError("Running-request metrics are invalid.")
            return math.ceil(value)
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise MetricsUnavailableError("Running requests could not be verified.") from exc
