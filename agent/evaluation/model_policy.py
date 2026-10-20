"""Optional local-model policy for frozen evaluation.

This module is imported lazily by ``rl-eval``; the base package never imports
torch/transformers. It intentionally uses the same chat template as training and
reuses the SFT/RL LoRA adapter, so evaluation is a policy run rather than a
re-tokenized rollout claim.
"""

from __future__ import annotations


class ModelPolicy:
    def __init__(self, model, *, adapter=None, max_new_tokens=512, temperature=0.9, top_p=0.95):
        try:
            import torch
            from peft import PeftModel
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("rl-eval --policy model requires torch/transformers/peft") from exc
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model, trust_remote_code=True)
        try:
            self.model = AutoModelForCausalLM.from_pretrained(
                model,
                torch_dtype=torch.bfloat16,
                device_map="auto",
                trust_remote_code=True,
            )
        except (ValueError, OSError) as first_error:
            # Some Qwen3.5 checkpoints are registered as image-text-to-text models.
            from transformers import AutoModelForImageTextToText

            try:
                self.model = AutoModelForImageTextToText.from_pretrained(
                    model,
                    torch_dtype=torch.bfloat16,
                    device_map="auto",
                    trust_remote_code=True,
                )
            except (ValueError, OSError):
                raise first_error
        if adapter:
            self.model = PeftModel.from_pretrained(self.model, adapter)
        self.model.eval()
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.top_p = top_p

    def _render(self, messages):
        kwargs = {"tokenize": False, "add_generation_prompt": True, "enable_thinking": False}
        try:
            return self.tokenizer.apply_chat_template(messages, **kwargs)
        except TypeError:
            kwargs.pop("enable_thinking", None)
            return self.tokenizer.apply_chat_template(messages, **kwargs)

    def act(self, messages):
        text = self._render(messages)
        inputs = self.tokenizer(text, return_tensors="pt").to(self.model.device)
        generate = {
            "max_new_tokens": self.max_new_tokens,
            "pad_token_id": self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
        }
        if self.temperature and self.temperature > 0:
            generate.update(do_sample=True, temperature=self.temperature, top_p=self.top_p)
        else:
            generate.update(do_sample=False)
        with self.torch.inference_mode():
            output = self.model.generate(**inputs, **generate)
        new_tokens = output[0][inputs["input_ids"].shape[1] :]
        return self.tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
