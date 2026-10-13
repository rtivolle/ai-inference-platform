"""
LiteLLM Dynamic Model Synchronizer adhering to Contract 3.
Registers models into LiteLLM Proxy upon endpoint readiness, and deregisters on deletion.
"""

import os
import logging
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger("litellm_sync")

DEFAULT_LITELLM_URL = os.environ.get(
    "LITELLM_PROXY_URL",
    "http://litellm-service.ai-gateway.svc.cluster.local:4000"
)
DEFAULT_LITELLM_KEY = os.environ.get(
    "LITELLM_MASTER_KEY",
    "sk-internal-litellm-master-key"
)

class LiteLLMSync:
    def __init__(
        self,
        base_url: str = DEFAULT_LITELLM_URL,
        master_key: str = DEFAULT_LITELLM_KEY,
        mock_mode: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.master_key = master_key
        self.mock_mode = mock_mode
        self._mock_registry: Dict[str, Dict[str, Any]] = {}

    async def register_model(
        self,
        model_name: str,
        original_model_id: str,
        service_name: str,
        namespace: str = "dynamo-workloads",
        port: int = 8000,
        rpm: int = 1000,
        timeout: int = 600,
    ) -> Dict[str, Any]:
        """
        Executes POST /model/new adhering to Contract 3.
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

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                logger.info(f"Successfully registered model {model_name} into LiteLLM Proxy.")
                return resp.json()
            except Exception as e:
                logger.error(f"LiteLLM registration failed for {model_name}: {e}")
                # Fallback to local mock record so local flows don't crash when offline
                self._mock_registry[model_name] = payload
                return {"status": "mock_recorded", "data": payload}

    async def deregister_model(self, model_name: str) -> bool:
        """
        Executes POST /model/delete to withdraw the model from routing.
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

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                logger.info(f"Successfully deregistered model {model_name} from LiteLLM.")
                return True
            except Exception as e:
                logger.warning(f"LiteLLM deregistration call failed for {model_name}: {e}")
                self._mock_registry.pop(model_name, None)
                return True
