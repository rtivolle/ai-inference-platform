"""Offline CPU check of the pinned trainer stack, completion masks, and merging.

Run inside the built trainer image. No Hub access, credentials, S3, or GPU needed.
This proves library integration, not production model quality or GPU compatibility.
"""

import tempfile

import torch
from datasets import Dataset
from peft import LoraConfig
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from transformers import PreTrainedTokenizerFast, Qwen2Config, Qwen2ForCausalLM, set_seed
from trl import SFTConfig, SFTTrainer


def main():
    set_seed(42)
    torch.set_num_threads(2)
    vocabulary = {"<pad>": 0, "<unk>": 1, "<eos>": 2, "user": 3, "assistant": 4, "hello": 5, "world": 6}
    backend = Tokenizer(WordLevel(vocabulary, unk_token="<unk>"))
    backend.pre_tokenizer = Whitespace()
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=backend, pad_token="<pad>", unk_token="<unk>", eos_token="<eos>")
    tokenizer.chat_template = "{% for message in messages %}{{ message['role'] + ' ' + message['content'] + ' ' }}{% endfor %}{% if add_generation_prompt %}{{ 'assistant ' }}{% endif %}"
    model = Qwen2ForCausalLM(Qwen2Config(vocab_size=len(vocabulary), hidden_size=32, intermediate_size=64, num_hidden_layers=1,
                                       num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=64, pad_token_id=0, eos_token_id=2))
    examples = Dataset.from_list([{"prompt": [{"role": "user", "content": "hello"}], "completion": [{"role": "assistant", "content": "world"}]}] * 4)
    with tempfile.TemporaryDirectory() as output:
        trainer = SFTTrainer(model=model, processing_class=tokenizer, train_dataset=examples, eval_dataset=examples,
                             peft_config=LoraConfig(r=4, lora_alpha=8, target_modules="all-linear", task_type="CAUSAL_LM"),
                             args=SFTConfig(output_dir=output, use_cpu=True, bf16=False, fp16=False, max_length=32, completion_only_loss=True,
                                            per_device_train_batch_size=2, max_steps=2, learning_rate=0.001, report_to="none", save_strategy="no", disable_tqdm=True))
        mask = trainer.train_dataset[0]["completion_mask"]
        assert 0 in mask and 1 in mask, "Prompt tokens must be excluded from loss"
        trainer.train()
        loss = trainer.evaluate()["eval_loss"]
        assert torch.isfinite(torch.tensor(loss)), "Evaluation must produce finite loss"
        assert any(torch.count_nonzero(parameter).item() > 0 for name, parameter in trainer.model.named_parameters() if "lora_B" in name), "Training must update adapters"
        inputs = tokenizer("user hello assistant world", return_tensors="pt")
        trainer.model.eval()
        with torch.no_grad():
            before = trainer.model(**inputs).logits
            merged = trainer.model.merge_and_unload()
            after = merged(**inputs).logits
        torch.testing.assert_close(before, after, atol=1e-5, rtol=1e-4)
        merged.save_pretrained(output, safe_serialization=True)
        restored = Qwen2ForCausalLM.from_pretrained(output)
        restored.eval()
        with torch.no_grad():
            torch.testing.assert_close(after, restored(**inputs).logits, atol=1e-5, rtol=1e-4)
    print("Trainer smoke check passed: completion masking, LoRA updates, evaluation, merge, and reload")


if __name__ == "__main__":
    main()
