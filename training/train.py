"""One GPU LoRA SFT, held-out evaluation, durable artifacts, private Hub export."""

import hashlib
import json
import math
import os
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(body):
    return hashlib.sha256(body).hexdigest()


def checked_dataset(s3, bucket, artifact, destination):
    body = s3.get_object(Bucket=bucket, Key=artifact["key"])["Body"].read()
    if digest(body) != artifact["sha256"]:
        raise ValueError("Dataset checksum mismatch")
    if len(body.splitlines()) != artifact["count"] or artifact["count"] == 0:
        raise ValueError("Dataset must contain the expected nonempty split")
    destination.write_bytes(body)


def main():
    import boto3
    import torch
    from botocore.config import Config
    from datasets import load_dataset
    from huggingface_hub import HfApi
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer, TrainerCallback, set_seed
    from trl import SFTConfig, SFTTrainer

    config = json.loads(os.environ["TRAINING_CONFIG"])
    request, dataset = config["request"], config["dataset"]
    set_seed(request["seed"])
    if not torch.cuda.is_available():
        raise RuntimeError("The training job requires a CUDA GPU")
    s3 = boto3.client("s3", endpoint_url=config.get("endpoint"), config=Config(connect_timeout=5, read_timeout=60, retries={"max_attempts": 3}))
    bucket = config["bucket"]
    hub = HfApi(token=os.environ["HF_TOKEN"])
    if hub.repo_exists(request["output_model_id"]):
        raise ValueError("Use a new private output repository for each run")
    manifest = s3.get_object(Bucket=bucket, Key=f"datasets/{dataset['id']}/manifest.json")["Body"].read()
    if digest(manifest) != dataset["manifest_sha256"]:
        raise ValueError("Dataset manifest checksum mismatch")
    parsed_manifest = json.loads(manifest)
    if parsed_manifest["files"] != dataset["files"]:
        raise ValueError("Dataset file provenance mismatch")
    work = Path("/work")
    work.mkdir(parents=True, exist_ok=True)
    paths = {}
    for split, artifact in dataset["files"].items():
        path = work / f"{split}.jsonl"
        checked_dataset(s3, bucket, artifact, path)
        paths[split] = str(path)
    examples = load_dataset("json", data_files=paths)
    # Prompt/completion training puts loss on the final reviewed assistant answer.
    def prompt_completion(row):
        messages = row["messages"]
        if messages[-1]["role"] != "assistant":
            raise ValueError("A training example must end with an assistant response")
        return {"prompt": messages[:-1], "completion": messages[-1:]}

    examples = examples.map(prompt_completion, remove_columns=["messages"])
    tokenizer = AutoTokenizer.from_pretrained(request["base_model"], revision=request["base_revision"], trust_remote_code=False)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    # Fail instead of silently truncating the target answer out of an example.
    for split in examples.values():
        for example in split:
            tokens = tokenizer.apply_chat_template(example["prompt"] + example["completion"], tokenize=True)
            if len(tokens) > request["max_length"]:
                raise ValueError("An example exceeds max_length; curate or increase the limit")
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    model = AutoModelForCausalLM.from_pretrained(request["base_model"], revision=request["base_revision"], torch_dtype=dtype,
                                               trust_remote_code=False, attn_implementation="sdpa")
    model.config.use_cache = False
    prefix = f"runs/{config['run_id']}"

    class DurableProgress(TrainerCallback):
        def on_log(self, args, state, control, logs=None, **kwargs):
            progress = {"run_id": config["run_id"], "global_step": state.global_step, "metrics": logs or {}}
            s3.put_object(Bucket=bucket, Key=f"{prefix}/progress.json", Body=canonical(progress), ContentType="application/json")

        def on_save(self, args, state, control, **kwargs):
            checkpoint = Path(args.output_dir) / f"checkpoint-{state.global_step}"
            for path in sorted(checkpoint.rglob("*")):
                if path.is_file():
                    s3.upload_file(str(path), bucket, f"{prefix}/{path.relative_to(work).as_posix()}")

    args = SFTConfig(output_dir=str(work / "checkpoints"), num_train_epochs=request["epochs"], learning_rate=request["learning_rate"],
                     max_length=request["max_length"], per_device_train_batch_size=1, per_device_eval_batch_size=1,
                     gradient_accumulation_steps=8, gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
                     bf16=dtype == torch.bfloat16, fp16=dtype == torch.float16, completion_only_loss=True,
                     seed=request["seed"], data_seed=request["seed"], report_to="none", logging_steps=10, save_strategy="epoch")
    trainer = SFTTrainer(model=model, args=args, processing_class=tokenizer, train_dataset=examples["train"], eval_dataset=examples["validation"],
                         peft_config=LoraConfig(r=request["lora_rank"], lora_alpha=2 * request["lora_rank"], lora_dropout=0.05, target_modules="all-linear", task_type="CAUSAL_LM"),
                         callbacks=[DurableProgress()])
    result = {"run_id": config["run_id"], "dataset_manifest_sha256": dataset["manifest_sha256"], "request_sha256": digest(canonical(request)),
              "base_validation_loss": trainer.evaluate(examples["validation"])["eval_loss"], "base_test_loss": trainer.evaluate(examples["test"])["eval_loss"]}
    trainer.train()
    result.update(candidate_validation_loss=trainer.evaluate(examples["validation"])["eval_loss"], candidate_test_loss=trainer.evaluate(examples["test"])["eval_loss"],
                  peak_gpu_memory_bytes=torch.cuda.max_memory_allocated(), training_history=trainer.state.log_history)
    if any(not math.isfinite(result[key]) for key in ("base_validation_loss", "base_test_loss", "candidate_validation_loss", "candidate_test_loss")):
        raise ValueError("Evaluation returned nonfinite losses")
    adapter = work / "adapter"
    trainer.save_model(str(adapter))
    merged = work / "model"
    trainer.model.merge_and_unload().save_pretrained(merged, safe_serialization=True)
    tokenizer.save_pretrained(merged)
    artifacts = []
    for folder in (adapter, merged, work / "checkpoints"):
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                key = f"{prefix}/{path.relative_to(work).as_posix()}"
                s3.upload_file(str(path), bucket, key)
                with path.open("rb") as source:
                    checksum = hashlib.file_digest(source, "sha256").hexdigest()
                artifacts.append({"key": key, "sha256": checksum, "bytes": path.stat().st_size})
    # The run request explicitly names the private repository to create/update.
    hub.create_repo(request["output_model_id"], private=True, exist_ok=False)
    commit = hub.upload_folder(repo_id=request["output_model_id"], folder_path=str(merged), commit_message=f"Fine-tuning run {config['run_id']}")
    result.update(model_id=request["output_model_id"], model_revision=commit.oid, artifacts=artifacts)
    body = canonical(result)
    s3.put_object(Bucket=bucket, Key=f"{prefix}/result.json", Body=body, ContentType="application/json", IfNoneMatch="*")
    print(json.dumps({"run_id": config["run_id"], "status": "completed", "model_revision": commit.oid}))


if __name__ == "__main__":
    main()
