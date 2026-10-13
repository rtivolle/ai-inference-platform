"""
LiteLLM Dynamic Model Synchronizer adhering to Contract 3 (R1 Revision).
- No silent mock fallback.
- Exponential backoff retry (3 attempts).
- Raises error on failure; deregister_model blocks K8s deletion if deregistration fails.
"""

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("litellm_sync")

DEFAULT_LITELLM_URL = os.environ.get("LITELLM_PROXY_URL", "http://litellm-service.ai-gateway.svc.cluster.local:4000")


class LiteLLMSync:
    def __init__(
        self,
        base_url: str = DEFAULT_LITELLM_URL,
        master_key: str | None = None,
        mock_mode: bool | None = None,
    ):
        env_mock = os.environ.get("CONTROL_PLANE_MOCK", "0") in ("1", "true", "True")
        is_in_cluster = os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")

        if is_in_cluster and (mock_mode or env_mock):
            raise RuntimeError("Mock mode (CONTROL_PLANE_MOCK=1) is strictly forbidden in-cluster!")

        self.mock_mode = mock_mode if mock_mode is not None else env_mock
        self.base_url = base_url.rstrip("/")

        # In non-mock mode, key must be provided
        key = master_key or os.environ.get("LITELLM_MASTER_KEY")
        if not key and not self.mock_mode:
            raise RuntimeError(
                "LITELLM_MASTER_KEY must be provided via Secret/environment variable. "
                "No default credentials permitted in non-mock mode."
            )
        self.master_key = key or "mock-key"
        self._mock_registry: dict[str, dict[str, Any]] = {}

    async def register_model(
        self,
        model_name: str,
        original_model_id: str,
        service_name: str,
        namespace: str = "dynamo-workloads",
        port: int = 8000,
        rpm: int = 1000,
        timeout: int = 600,
    ) -> dict[str, Any]:
        """
        Executes POST /model/new with exponential retry (3 attempts).
        """
        api_base = f"http://{service_name}.{namespace}.svc.cluster.local:{port}/v1"
        payload = {
            "model_name": model_name,
            "litellm_params": {
                "model": f"openai/{original_model_id}",
                "api_base": api_base,
                "api_key": "internal-dynamo-key",
                "rpm": rpm,
                "timeout": timeout,
            },
            "model_info": {
                "id": f"dgd-{model_name}",
                "mode": "chat",
                "supports_function_calling": True,
            },
        }

        if self.mock_mode:
            self._mock_registry[model_name] = payload
            logger.info(f"[MOCK] Registered model {model_name} in LiteLLM: {payload}")
            return {"status": "success", "data": payload}

        url = f"{self.base_url}/model/new"
        headers = {
            "Authorization": f"Bearer {self.master_key}",
            "Content-Type": "application/json",
        }

        last_error = None
        for attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    logger.info(f"Successfully registered model {model_name} into LiteLLM Proxy on attempt {attempt}.")
                    return resp.json()
            except Exception as e:
                last_error = e
                logger.warning(f"Attempt {attempt}/3 failed registering {model_name} in LiteLLM: {e}")
                if attempt < 3:
                    await asyncio.sleep(2**attempt)

        logger.error(f"Failed to register model {model_name} into LiteLLM after 3 attempts.")
        raise RuntimeError(f"LiteLLM registration failed for {model_name}: {last_error}")

    async def deregister_model(self, model_name: str) -> bool:
        """
        Executes POST /model/delete with exponential retry.
        Blocks and raises error on failure (never silently returns True!).
        """
        model_id = f"dgd-{model_name}"
        if self.mock_mode:
            self._mock_registry.pop(model_name, None)
            logger.info(f"[MOCK] Deregistered model {model_name} from LiteLLM.")
            return True

        url = f"{self.base_url}/model/delete"
        headers = {
            "Authorization": f"Bearer {self.master_key}",
            "Content-Type": "application/json",
        }
        payload = {"id": model_id}

        last_error = None
        for attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    logger.info(f"Successfully deregistered model {model_name} from LiteLLM on attempt {attempt}.")
                    return True
            except Exception as e:
                last_error = e
                logger.warning(f"Attempt {attempt}/3 failed deregistering {model_name} in LiteLLM: {e}")
                if attempt < 3:
                    await asyncio.sleep(2**attempt)

        logger.error(f"Failed to deregister model {model_name} from LiteLLM: {last_error}")
        raise RuntimeError(f"LiteLLM deregistration failed for {model_name}: {last_error}")
