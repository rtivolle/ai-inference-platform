"""
Pytest configuration and shared fixtures for unit and integration testing.
"""

import pytest
from api.dynamo_reconciler import DynamoReconciler
from api.k8s_client import K8sClient
from api.litellm_sync import LiteLLMSync
from autotuner.vram_calculator import ModelArchitectureSpecs, VRAMCalculator


@pytest.fixture
def vram_calc():
    return VRAMCalculator()


@pytest.fixture
def llama_70b_specs():
    return ModelArchitectureSpecs(
        num_params=70_553_706_496,
        num_hidden_layers=80,
        hidden_size=8192,
        num_attention_heads=64,
        num_key_value_heads=8,
        max_position_embeddings=131072,
        default_dtype="bf16",
    )


@pytest.fixture
def llama_8b_specs():
    return ModelArchitectureSpecs(
        num_params=8_030_261_248,
        num_hidden_layers=32,
        hidden_size=4096,
        num_attention_heads=32,
        num_key_value_heads=8,
        max_position_embeddings=8192,
        default_dtype="bf16",
    )


@pytest.fixture
def mock_k8s():
    return K8sClient(mock_mode=True)


@pytest.fixture
def mock_litellm():
    return LiteLLMSync(mock_mode=True)


@pytest.fixture
def reconciler(mock_k8s, mock_litellm):
    return DynamoReconciler(k8s_client=mock_k8s, litellm_sync=mock_litellm, drain_timeout_seconds=0)
