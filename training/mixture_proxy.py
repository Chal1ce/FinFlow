"""Optional local PyTorch causal proxy training. Imported only by explicit experiment commands."""

from __future__ import annotations

import json
import math
import os
import random
from importlib.metadata import version
from pathlib import Path

import torch
from torch import nn
from torch.nn import functional as F

from core.flywheel_files import sha256, write_json
from training.flywheel_corpus import read_jsonl, verify
from training.mixture import normalize_weights
from training.method_cards import CARDS
from workflow.flywheel_config import digest


class ProxyLM(nn.Module):
    """Small pre-norm decoder trained from scratch; no remote code or model downloads."""

    def __init__(self, vocab, length, config):
        super().__init__()
        width, heads, layers = config["width"], config["heads"], config["layers"]
        self.token = nn.Embedding(vocab, width)
        self.position = nn.Embedding(length, width)
        # Construct layers independently instead of cloning equal initial layer weights.
        self.layers = nn.ModuleList(
            [
                nn.TransformerEncoderLayer(width, heads, width * 4, dropout=0.0, activation="gelu", batch_first=True, norm_first=True)
                for _ in range(layers)
            ]
        )
        self.norm = nn.LayerNorm(width)
        for module in self.modules():
            if isinstance(module, (nn.Embedding, nn.Linear)):
                nn.init.normal_(module.weight, std=0.02)
                if isinstance(module, nn.Linear) and module.bias is not None:
                    nn.init.zeros_(module.bias)
            elif isinstance(module, nn.MultiheadAttention):
                nn.init.normal_(module.in_proj_weight, std=0.02)
                nn.init.zeros_(module.in_proj_bias)

    def forward(self, ids):
        size = ids.shape[1]
        value = self.token(ids) + self.position(torch.arange(size, device=ids.device))[None, :, :]
        mask = torch.ones(size, size, device=ids.device, dtype=torch.bool).triu(1)
        for layer in self.layers:
            value = layer(value, src_mask=mask)
        return F.linear(self.norm(value), self.token.weight)


def options(config):
    defaults = {
        "width": 128,
        "heads": 4,
        "layers": 2,
        "steps": 500,
        "reference_steps": 500,
        "batch_size": 4,
        "seed": 42,
        "checkpoint_every": 50,
        "eval_blocks_per_domain": 64,
        "threads": 4,
        "learning_rate": 0.0003,
        "weight_decay": 0.01,
        "grad_clip": 1.0,
        "device": "cpu",
        "doremi_eta": 1.0,
        "doremi_smoothing": 0.001,
    }
    extra = config.get("proxy", {})
    if not isinstance(extra, dict) or set(extra) - set(defaults):
        raise ValueError("unknown proxy setting")
    p = defaults | extra
    for key in (
        "width",
        "heads",
        "layers",
        "steps",
        "reference_steps",
        "batch_size",
        "checkpoint_every",
        "eval_blocks_per_domain",
        "threads",
    ):
        if type(p[key]) is not int or p[key] < 1:
            raise ValueError("proxy " + key + " must be a positive integer")
    if type(p["seed"]) is not int or p["seed"] < 0 or p["width"] % p["heads"]:
        raise ValueError("seed must be nonnegative; width must be divisible by heads")
    for key in ("learning_rate", "grad_clip", "doremi_eta"):
        if type(p[key]) not in (float, int) or not math.isfinite(p[key]) or p[key] <= 0:
            raise ValueError(key + " must be positive and finite")
    if type(p["weight_decay"]) not in (float, int) or not math.isfinite(p["weight_decay"]) or p["weight_decay"] < 0:
        raise ValueError("invalid weight_decay")
    if type(p["doremi_smoothing"]) not in (float, int) or not 0 < p["doremi_smoothing"] < 1:
        raise ValueError("doremi_smoothing must be in (0,1)")
    if p["device"] not in {"cpu", "cuda", "mps"}:
        raise ValueError("device must be cpu, cuda or mps")
    return p


class Experiment:
    def __init__(self, config, data, output, method):
        self.config, self.p = config, options(config)
        self.root = Path(output).resolve()
        self.data_path = Path(data).resolve()
        self.manifest = verify(self.data_path)
        if self.manifest.get("schema_version") != "finflow-domain-data-v1":
            raise ValueError("prepare a finflow-domain-data-v1 package first")
        if (
            config.get("group_fields", ["source_name"]) != self.manifest["group_fields"]
            or config.get("sequence_length", 256) != self.manifest["sequence_length"]
            or sha256(config["tokenizer"]) != self.manifest["tokenizer_sha256"]
            or sorted(str(Path(p).resolve()) for p in config["inputs"]) != sorted(i["path"] for i in self.manifest["inputs"])
        ):
            raise ValueError("experiment configuration does not match prepared data")
        self.names = self.manifest["domains"]
        self.method = method
        self.device = torch.device(self.p["device"])
        self.data = {}
        for split in ("train", "validation"):
            self.data[split] = {k: [] for k in self.names}
            for row in read_jsonl(self.data_path / f"{split}.jsonl"):
                ids = row["input_ids"]
                if (
                    row["split"] != split
                    or row["group"] not in self.names
                    or not 2 <= len(ids) <= self.manifest["sequence_length"] + 1
                    or any(type(t) is not int or not 0 <= t < self.manifest["vocab_size"] for t in ids)
                    or (split == "train" and len(ids) != self.manifest["sequence_length"] + 1)
                ):
                    raise ValueError("invalid prepared token block")
                self.data[split][row["group"]].append(row)
            if any(not values for values in self.data[split].values()):
                raise ValueError("every domain must have nonempty train/validation blocks")
        train_families = {r["family"] for values in self.data["train"].values() for r in values}
        val_families = {r["family"] for values in self.data["validation"].values() for r in values}
        if train_families & val_families:
            raise ValueError("prepared document families cross splits")
        self.eval = {
            k: sorted(values, key=lambda r: digest([self.p["seed"], r["id"]]))[: self.p["eval_blocks_per_domain"]]
            for k, values in self.data["validation"].items()
        }
        self.eval_weights = normalize_weights(config.get("validation_weights", dict.fromkeys(self.names, 1)), self.names)
        versions = {"torch": str(torch.__version__)}
        if method == "regmix":
            versions.update({key: version(key) for key in ("numpy", "lightgbm")})
        self.identity = digest(
            {
                "engine": "finflow-proxy-v1",
                "method": method,
                "config": config,
                "proxy": self.p,
                "data": sha256(self.data_path / "manifest.json"),
                "data_inventory": sha256(self.data_path / "checksums.sha256"),
                "versions": versions,
            }
        )
        run = self.root / "run.json"
        if run.exists():
            if json.loads(run.read_text())["experiment_uid"] != self.identity:
                raise ValueError("output directory belongs to another experiment; use a new directory")
        else:
            if self.root.exists() and any(p.name != ".flywheel.lock" for p in self.root.iterdir()):
                raise ValueError("output directory is not an empty experiment directory")
            self.root.mkdir(parents=True, exist_ok=True)
            write_json(
                run,
                {
                    "experiment_uid": self.identity,
                    "config": config,
                    "proxy": self.p,
                    "method": method,
                    "data_manifest_sha256": sha256(self.data_path / "manifest.json"),
                    "data_inventory_sha256": sha256(self.data_path / "checksums.sha256"),
                    "versions": versions,
                    "validation_weights": self.eval_weights,
                    "validation_blocks": {k: [r["id"] for r in v] for k, v in self.eval.items()},
                    "implementation": "single-device algorithm adaptation; not original-paper scale or benchmark reproduction",
                    "method_card": CARDS[method],
                },
            )
        torch.set_num_threads(self.p["threads"])
        # Dropout is disabled; per-step Python RNG is derived from step ID, making resume independent of sampler state.
        torch.use_deterministic_algorithms(True)

    def model(self):
        torch.manual_seed(self.p["seed"])
        return ProxyLM(self.manifest["vocab_size"], self.manifest["sequence_length"], self.p).to(self.device)

    def batch(self, rows):
        length = self.manifest["sequence_length"]
        pad = self.manifest["eos_token_id"]
        inputs, labels = [], []
        for row in rows:
            ids = row["input_ids"]
            inputs.append(ids[:-1] + [pad] * (length - len(ids) + 1))
            labels.append(ids[1:] + [-100] * (length - len(ids) + 1))
        return torch.tensor(inputs, device=self.device), torch.tensor(labels, device=self.device)

    def losses(self, model, rows):
        ids, labels = self.batch(rows)
        logits = model(ids)
        losses = F.cross_entropy(logits.reshape(-1, logits.shape[-1]), labels.flatten(), reduction="none").reshape(labels.shape)
        return losses, labels != -100

    def evaluate(self, model):
        model.eval()
        result = {}
        with torch.no_grad():
            for name, rows in self.eval.items():
                total, tokens = 0.0, 0
                for i in range(0, len(rows), self.p["batch_size"]):
                    losses, mask = self.losses(model, rows[i : i + self.p["batch_size"]])
                    total += losses[mask].sum().item()
                    tokens += mask.sum().item()
                result[name] = {"loss": total / tokens, "tokens": tokens, "blocks": len(rows)}
        score = sum(self.eval_weights[k] * v["loss"] for k, v in result.items())
        if not math.isfinite(score):
            raise ValueError("nonfinite validation loss")
        return {"loss": score, "domains": result}

    def train(self, name, weights, *, steps=None, reference=None):
        """Train/resume a fixed-mixture proxy or stratified DoReMi proxy; checkpoint every N updates."""
        weights = normalize_weights(weights, self.names)
        steps = steps or self.p["steps"]
        is_doremi = reference is not None
        phase_uid = digest([self.identity, name, weights, steps, is_doremi])
        checkpoint = self.root / (name + ".pt")
        result_path = self.root / (name + ".json")
        model = self.model()
        optimizer = torch.optim.AdamW(model.parameters(), lr=self.p["learning_rate"], weight_decay=self.p["weight_decay"])
        alpha = torch.tensor([weights[k] for k in self.names], dtype=torch.float64)
        average = torch.zeros_like(alpha)
        token_counts = dict.fromkeys(self.names, 0)
        history, start = [], 0
        if checkpoint.exists():
            state = torch.load(checkpoint, map_location="cpu", weights_only=True)
            if state["phase_uid"] != phase_uid:
                raise ValueError("proxy checkpoint identity mismatch")
            model.load_state_dict(state["model"])
            optimizer.load_state_dict(state["optimizer"])
            start, alpha, average = state["step"], state["alpha"], state["average"]
            token_counts, history = state["token_counts"], state["history"]
        if result_path.exists():
            result = json.loads(result_path.read_text())
            if result["phase_uid"] != phase_uid or result["checkpoint_sha256"] != sha256(checkpoint) or start != steps:
                raise ValueError("completed proxy result/checkpoint mismatch")
            return model, result
        if reference is not None:
            reference.eval()
            reference.requires_grad_(False)
        for step in range(start, steps):
            model.train()
            optimizer.zero_grad(set_to_none=True)
            rng = random.Random(digest([self.p["seed"], name, step]))
            if is_doremi:
                # Equal-size batches from EVERY domain give unbiased per-domain means without importance correction.
                means, excess = [], []
                for domain in self.names:
                    rows = rng.choices(self.data["train"][domain], k=self.p["batch_size"])
                    losses, mask = self.losses(model, rows)
                    with torch.no_grad():
                        ref, _ = self.losses(reference, rows)
                    means.append(losses[mask].mean())
                    excess.append((losses.detach()[mask] - ref[mask]).clamp_min(0).mean().item())
                    token_counts[domain] += int(mask.sum().item())
                scores = torch.tensor(excess, dtype=torch.float64)
                logits = alpha.log() + self.p["doremi_eta"] * scores
                updated = torch.softmax(logits, dim=0)
                eps = self.p["doremi_smoothing"]
                alpha = (1 - eps) * updated + eps / len(self.names)
                average += (alpha - average) / (step + 1)
                loss = sum(float(a) * mean for a, mean in zip(alpha, means))
            else:
                domains = rng.choices(self.names, weights=[weights[k] for k in self.names], k=self.p["batch_size"])
                rows = [rng.choice(self.data["train"][k]) for k in domains]
                losses, mask = self.losses(model, rows)
                loss = losses[mask].mean()
                for domain, count in zip(domains, mask.sum(dim=1).tolist()):
                    token_counts[domain] += count
            if not torch.isfinite(loss).item() or not torch.isfinite(alpha).all().item():
                raise ValueError("nonfinite training loss/weights; reduce learning rate in a new experiment")
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), self.p["grad_clip"], error_if_nonfinite=True)
            optimizer.step()
            if (step + 1) % self.p["checkpoint_every"] == 0 or step + 1 == steps:
                entry = {"step": step + 1, "loss": loss.item(), "tokens": sum(token_counts.values())}
                if is_doremi:
                    entry.update({"weights": dict(zip(self.names, alpha.tolist())), "clipped_excess": dict(zip(self.names, excess))})
                history.append(entry)
                temporary = checkpoint.with_suffix(".pt.tmp")
                torch.save(
                    {
                        "phase_uid": phase_uid,
                        "step": step + 1,
                        "model": model.state_dict(),
                        "optimizer": optimizer.state_dict(),
                        "alpha": alpha,
                        "average": average,
                        "token_counts": token_counts,
                        "history": history,
                    },
                    temporary,
                )
                os.replace(temporary, checkpoint)
                print(json.dumps({"phase": name, **entry}), flush=True)
        result = {
            "phase_uid": phase_uid,
            "steps": steps,
            "weights": weights,
            "token_counts": token_counts,
            "effective_domain_epochs": {k: v / self.manifest["tokens"]["train:" + k] for k, v in token_counts.items()},
            "validation": self.evaluate(model),
            "history": history,
            "checkpoint_sha256": sha256(checkpoint),
            "averaged_weights": dict(zip(self.names, average.tolist())) if is_doremi else None,
            "parameter_count": sum(p.numel() for p in model.parameters()),
        }
        write_json(result_path, result)
        return model, result

    def export(self, weights, details):
        result = {
            "schema_version": "finflow-learned-weights-v1",
            "method": self.method,
            "weights": normalize_weights(weights, self.names),
            "experiment_uid": self.identity,
            "pool_uid": self.manifest["pool_uid"],
            "group_fields": self.manifest["group_fields"],
            "tokenizer_sha256": self.manifest["tokenizer_sha256"],
            "details": details,
            "scope": "proxy-derived recommendation; evaluate downstream model before claiming improvement",
            "method_card": CARDS[self.method],
        }
        write_json(self.root / "weights.json", result)
        return {"status": "success", "path": str(self.root / "weights.json"), **result}


def doremi(config, data, output):
    experiment = Experiment(config, data, output, "doremi")
    initial = normalize_weights(config.get("initial_weights", dict.fromkeys(experiment.names, 1)), experiment.names)
    if any(w <= 0 for w in initial.values()):
        raise ValueError("DoReMi initial weights must be strictly positive")
    reference, baseline = experiment.train("reference", initial, steps=experiment.p["reference_steps"])
    _, learned = experiment.train("doremi", initial, reference=reference)
    return experiment.export(
        learned["averaged_weights"],
        {
            "reference_validation": baseline["validation"],
            "proxy_validation": learned["validation"],
            "reference_checkpoint_sha256": baseline["checkpoint_sha256"],
            "proxy_checkpoint_sha256": learned["checkpoint_sha256"],
            "algorithm": "tokenwise positive excess; exponentiated domain ascent; uniform smoothing; averaged weights",
            "adaptation": "stratified equal-size domain batches, small local decoder, one device",
        },
    )
