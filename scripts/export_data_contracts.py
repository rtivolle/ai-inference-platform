"""Regenerate the executable OpenAPI contract and data request JSON schemas."""

import json
from pathlib import Path

import yaml
from api.data_models import CaptureEvent, DatasetRequest, PromotionRequest, ReviewRequest, TrainingRequest
from api.main import app
from pydantic.json_schema import models_json_schema

ROOT = Path(__file__).resolve().parents[1]


def data_schema():
    _, schema = models_json_schema([(model, "validation") for model in (CaptureEvent, DatasetRequest, ReviewRequest, TrainingRequest, PromotionRequest)], title="Inference data and training requests")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


def main():
    (ROOT / "contracts/control_plane_openapi.yaml").write_text(yaml.safe_dump(app.openapi(), sort_keys=False))
    (ROOT / "contracts/data_workflow.schema.json").write_text(json.dumps(data_schema(), indent=2) + "\n")


if __name__ == "__main__":
    main()
