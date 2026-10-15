# Inference data, datasets, and fine-tuning

This is an API-first MVP. It connects LiteLLM terminal events to reviewed text
datasets, single-GPU LoRA training, held-out loss evaluation, and explicit
deployment of a pinned model version. It is disabled by default.

## Boundaries and storage

Inference traffic stays in LiteLLM. Its optional callback queues up to 256 events
per gateway worker, retries delivery three times, and drops oversized events or
events it cannot deliver. Delivery never blocks an inference response. The queue
is in memory: a gateway crash can lose queued events. This is best-effort capture,
not an audit-grade message broker.

The control plane stores event/review/run metadata in a separate SQLite database,
with WAL, on a persistent volume. Deploy **one replica and one Uvicorn worker**.
The configuration renderer selects a Recreate rollout to prevent concurrent API
processes using this backend. Back up SQLite with its backup API, not by copying
only the main database while WAL writes are active. An HA deployment requires
migrating this store and lifecycle locks to a shared transactional backend.

Dataset JSONL files and manifests are immutable artifacts with SHA-256 checksums.
Local artifact storage supports development; live training requires an S3 bucket
or an S3-compatible endpoint supporting conditional `If-None-Match` writes.
Training metrics, epoch checkpoints, adapters, merged model files, and final
results are stored under `runs/<run-id>/`. A new private Hugging Face repository
is created only when an explicitly requested training job exports its model.

## Local capture and curation

Use Python 3.11 and the normal development installation. For local mock mode:

```bash
export CONTROL_PLANE_MOCK=1
export DATA_WORKFLOW_ENABLED=1
export DATA_DB_PATH=/tmp/inference-data/metadata.sqlite3
export DATA_ARTIFACT_ROOT=/tmp/inference-data/artifacts
export DATA_CAPTURE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn api.main:app --host 127.0.0.1 --port 8000
```

The administrator API token is `test-token` in mock mode. Capture uses the
separate `DATA_CAPTURE_TOKEN`; that token cannot list, review, export, train, or
promote. Administrator credentials cannot authenticate to the capture endpoint.
Browser administrator sessions also work, with the existing CSRF requirements.

Post a gateway event to `POST /api/v1/data/events` using the capture token, or
import an existing text example through `POST /api/v1/data/examples` using
administrator authentication. Both accept:

```json
{
  "request_id": "example-001",
  "conversation_id": "conversation-001",
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "occurred_at": "2026-10-13T12:00:00Z",
  "outcome": "success",
  "latency_ms": 125,
  "input_tokens": 12,
  "output_tokens": 8,
  "consent": true,
  "messages": [
    {"role": "user", "content": "How do I reset my password?"},
    {"role": "assistant", "content": "Use the password reset link on the sign-in page."}
  ]
}
```

Capture bodies are limited to 128 KiB. Unknown fields are rejected. Without
consent, messages are removed before storage. Basic bearer-key, `sk-` key, and
email redaction runs at both gateway and ingestion; it is **not a complete PII
detector**. Human review remains required. Replaying the same request ID and
payload is idempotent; conflicting payloads return 409.

List pending examples with `GET /api/v1/data/events?limit=100&offset=0`, inspect
one with `GET /api/v1/data/events/<request-id>`, and review it with
`POST /api/v1/data/events/<request-id>/review`:

```json
{"status":"approved","corrected_response":"A verified answer.","note":"Checked against the support guide."}
```

Review records include the reviewer and timestamp. Failure/incomplete events,
metadata-only events, and transcripts without a final assistant response cannot
be approved. Only text system/user/assistant messages are supported; multimodal
and tool-call conversations are excluded from this training MVP.

Create a dataset with `POST /api/v1/data/datasets`:

```json
{"name":"support-v1","event_ids":["example-001","example-002"],"seed":"support-v1","validation_fraction":0.1,"test_fraction":0.1}
```

Every selected example must be approved. Whole conversations stay together, as
do conversations sharing the same prompt, even when their corrected answers
differ. Identical complete transcripts are deduplicated. Hash-based splits are
deterministic; small datasets may have empty splits. Create enough independent
conversation groups to populate train, validation, and test before training.
This handles exact overlaps; human review must still check semantic duplicates.

Use `GET /api/v1/data/datasets/<id>/export?split=train|validation|test` for JSONL.
Snapshots preserve corrected answers, source fingerprints, and review provenance.
Later reviews do not change previously created versions. Exports verify checksums.

## Kubernetes setup

Build the updated control-plane and warmer images using the existing delivery
pipeline. Build the training image from the repository root:

```bash
docker build -t <registry>/inference-trainer:<tag> -f training/Dockerfile .
```

The default build uses PyTorch's official CUDA 12.8 wheel index. For a small
CPU-only integration check, pass
`--build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu` and run
`docker run --rm --network none --entrypoint python <image> /app/smoke_test.py`.
The check verifies completion masking, adapter updates, evaluation, model merging,
and reload using a tiny random model; use the CUDA build for real training Jobs.

Publish that image and use its digest. The trainer pins Torch, Transformers, TRL,
PEFT, and Datasets in `training/requirements.txt`; validate it on your CUDA driver
and GPU before running production training. The callback also needs a LiteLLM
image tested with its documented custom-logger/terminal-streaming interface.

Provision these Secrets through your secret manager; never commit their values:

| Namespace / Secret | Required keys |
|---|---|
| `dynamo-control-plane/data-runtime` | `capture-token`, `access-key-id`, `secret-access-key`, `hf-read-token` |
| `ai-gateway/data-runtime` | Same `capture-token` |
| `inference-training/training-runtime` | `access-key-id`, `secret-access-key`, `hf-token` with private-repository create/write access |
| `dynamo-workloads/hf-token-secret` | `token` with read access to the trained private repository |

The metadata PVC needs a working default StorageClass, or an explicit
`storageClassName` suited to the control-plane node. Configure bucket encryption,
backups, and lifecycle rules. Restrict the control-plane identity to dataset
read/write and run read access; the trainer needs dataset read and run write
access. The first version uses static credentials; scoped workload identities
can replace these environment credentials without changing the S3 interface.

Prepare the manifests and patches locally:

```bash
python -m scripts.configure_data_workflow \
  --output /tmp/data-workflow \
  --litellm-config gateway/config.example.yaml \
  --bucket <artifact-bucket> \
  --training-image <registry>/inference-trainer@sha256:<digest> \
  --hf-owner <your-hugging-face-owner>
```

Add `--endpoint https://<s3-compatible-endpoint>` if needed. Supply your actual
LiteLLM configuration to preserve existing callbacks/settings. The renderer
embeds `gateway/data_capture.py` alongside `config.yaml` in a new ConfigMap. It
contains no secret values. After updating images and provisioning Secrets:

```bash
kubectl apply -f infra/data-workflow.yaml
kubectl apply -f /tmp/data-workflow/gateway-capture.yaml
kubectl -n dynamo-control-plane patch deployment control-plane --type strategic --patch-file /tmp/data-workflow/control-plane.patch.json
kubectl -n ai-gateway patch deployment litellm-proxy --type strategic --patch-file /tmp/data-workflow/gateway.patch.json
kubectl -n ai-gateway rollout restart deployment litellm-proxy
```

The control plane needs its existing `control-plane` ServiceAccount. Training
RBAC is scoped to `inference-training`; worker pods receive no Kubernetes API
token. A quota permits one active training GPU. Training can consume otherwise
available inference capacity; use a dedicated GPU pool if your inference SLO
requires it. Secret changes and callback changes require a gateway restart.
Re-running the normal LiteLLM installer can restore its original ConfigMap;
reapply the capture patch after such an upgrade.

Clients must explicitly opt in to content capture:

```json
{"metadata":{"training_consent":true,"conversation_id":"stable-conversation-id"}}
```

Both `DATA_CAPTURE_CONTENT_ENABLED=1` and the request flag are required.
Always reuse the conversation ID across turns; without it, request IDs are used
and the platform cannot reliably group different turns of one conversation.
`no-log` requests are skipped. Final streaming responses are captured once;
intermediate chunks are ignored. Truncated/tool-call responses are ineligible.

## Training, logs, evaluation, and promotion

Start with `POST /api/v1/training/runs`:

```json
{
  "dataset_id":"<dataset-version-id>",
  "base_model":"Qwen/Qwen2.5-0.5B-Instruct",
  "base_revision":"<40-character-commit-sha>",
  "output_model_id":"<configured-owner>/<new-repository-name>",
  "epochs":1,
  "learning_rate":0.0002,
  "max_length":1024,
  "lora_rank":16,
  "seed":42
}
```

Only this model family is supported initially. The planner uses a separate
training-memory estimate and requires one available GPU with at least 16 GiB;
the actual peak allocation is reported in the result. The Job uses one GPU,
gradient checkpointing, six-hour deadline, and no automatic retries. Examples
exceeding `max_length` fail rather than silently truncating reviewed targets.
LoRA loss targets the final assistant completion using TRL's prompt/completion
format. See [TRL 0.24 SFT documentation](https://huggingface.co/docs/trl/v0.24.0/en/sft_trainer).

`GET /api/v1/training/runs/<id>` reconciles status from Kubernetes and reports
available progress, results, peak GPU allocation, and artifact checksums.
`GET /api/v1/training/runs` lists persisted records; refresh individual runs for
live status. `POST /api/v1/training/runs/<id>/cancel` deletes an active Job.
Epoch checkpoints survive in object storage; automatic resume is not implemented.
Jobs expire after seven days, and completed results can be recovered after expiry.

`GET /api/v1/training/runs/<id>/logs?tail_lines=100` reads only the owned trainer
pod, limited to 500 lines and 128 KiB. After Job cleanup, final training history
remains available from results. Existing inference pod logs and Prometheus
metrics continue to serve operational diagnosis. Gateway stdout reports capture
queue drops and delivery failures without printing conversation bodies. There
is no centralized log-search backend in this MVP.

Workflow API access emits JSON event fields through the Uvicorn logger, with a
server-generated request ID also returned in `X-Request-ID`. It logs route
templates, method, status, and duration, excluding bodies, query strings, and
credentials. Review/run records provide the separate actor and artifact history.

The worker evaluates baseline and candidate completion loss on validation and
test splits. Promotion requires validation improvement and no test regression.
These loss checks are an initial gate, not evidence of task quality: run your
task-specific evaluation and serving latency/memory checks before directing
production traffic to a candidate.

Explicitly deploy a candidate with `POST /api/v1/training/runs/<id>/promote`:

```json
{"deployment_name":"support-candidate-v1","max_model_len":1024}
```

Inspection, warming, and worker model paths pin the exact exported Hub commit.
The full merged model is deployed under a new name; existing models remain
available for rollback. A 202 means provisioning was accepted, not readiness or
traffic cutover. Check the existing model status API, then explicitly select the
candidate model in gateway clients. No default routing alias is switched.

## Retention, recovery, and validation

`DATA_EVENT_RETENTION_DAYS` defaults to 30 (range 1–365). Expired source events
are removed on event reads/ingestion; run a periodic call to `DataStore.purge_events`
if cleanup must occur while idle. Deduplication guarantees apply while an event
is retained. Dataset snapshots intentionally outlive source events. Configure
object-storage lifecycle separately, and remove derived versions/checkpoints/
private Hub models when responding to an erasure request. SQLite expiry is not
a secure wipe of backups or previously allocated database pages.

Before rollout, validate captures with nonstreaming, completed streaming,
interrupted streaming, and opt-out requests against your pinned LiteLLM image.
Exercise an ingestion outage and verify inference stays available. Validate
metadata restart recovery, reviewed export checksums, and a small GPU training
run. Confirm checkpoints and results survive Job cleanup; then validate the
pinned model warms and serves successfully before traffic cutover.

Disable capture with `DATA_CAPTURE_ENABLED=0` and restart the gateway. Disabling
`DATA_WORKFLOW_ENABLED` makes workflow APIs unavailable without deleting their
data. Cancel outstanding Jobs separately. Restore the original gateway ConfigMap
to remove the callback, and preserve the metadata PVC/artifacts during rollback.
