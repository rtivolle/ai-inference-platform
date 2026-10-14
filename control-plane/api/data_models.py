"""Validated, text-only contracts for the first data/training workflow."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Message(StrictModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=16384)


class CaptureEvent(StrictModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    conversation_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    model_revision: str | None = Field(default=None, max_length=128)
    occurred_at: datetime
    outcome: Literal["success", "failure", "incomplete"]
    latency_ms: float = Field(ge=0, le=86400000)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    consent: bool = False
    messages: list[Message] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_capture(self):
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        if not self.consent:
            self.messages = []
        return self


class ReviewRequest(StrictModel):
    status: Literal["approved", "rejected"]
    corrected_response: str | None = Field(default=None, min_length=1, max_length=16384)
    note: str = Field(default="", max_length=2000)


class DatasetRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    event_ids: list[str] = Field(min_length=1, max_length=10000)
    seed: str = Field(default="v1", min_length=1, max_length=128)
    validation_fraction: float = Field(default=0.1, gt=0, lt=0.5)
    test_fraction: float = Field(default=0.1, gt=0, lt=0.5)

    @model_validator(mode="after")
    def validate_fractions(self):
        if self.validation_fraction + self.test_fraction >= 1:
            raise ValueError("The training split must be nonempty")
        if len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("event_ids must be unique")
        return self


class TrainingRequest(StrictModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    base_model: Literal["Qwen/Qwen2.5-0.5B-Instruct"] = "Qwen/Qwen2.5-0.5B-Instruct"
    base_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    output_model_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    epochs: int = Field(default=1, ge=1, le=10)
    learning_rate: float = Field(default=0.0002, gt=0, le=0.01)
    max_length: int = Field(default=1024, ge=128, le=4096)
    lora_rank: int = Field(default=16, ge=4, le=64)
    seed: int = Field(default=42, ge=0, le=2147483647)


class PromotionRequest(StrictModel):
    deployment_name: str = Field(pattern=r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
    max_model_len: int = Field(default=1024, ge=128, le=4096)
