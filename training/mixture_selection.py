"""DEITA-inspired score-first diversity selection from content-bound sidecar records."""

from __future__ import annotations

import math
import re

from core.flywheel_files import sha256
from training.flywheel_corpus import read_jsonl


class Selector:
    def __init__(self, config, records):
        self.metric = config.get("metric", "jaccard")
        self.threshold = config.get("threshold", 0.9)
        if self.metric not in {"jaccard", "cosine"}:
            raise ValueError("selection metric must be jaccard or cosine")
        if type(self.threshold) not in (int, float) or not math.isfinite(self.threshold) or not 0 < self.threshold <= 1:
            raise ValueError("selection threshold must be in (0,1]")
        self.scores = {}
        for score in read_jsonl(config["scores_file"]):
            if score["id"] in self.scores:
                raise ValueError("duplicate score ID")
            for key in ("quality", "complexity"):
                if type(score.get(key)) not in (int, float) or not math.isfinite(score[key]) or not 0 <= score[key] <= 5:
                    raise ValueError("quality and complexity must be finite in [0,5]")
            self.scores[score["id"]] = score
        self.features, self.chosen = {}, []
        dimensions = set()
        for row in records:
            if row["split"] != "train":
                continue
            score = self.scores.get(row["id"])
            if score is None or score.get("content_hash") != row["content_hash"]:
                raise ValueError("scores must cover every training sample and match its content_hash")
            if self.metric == "cosine":
                vector = score.get("embedding")
                if not isinstance(vector, list) or not vector or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
                    raise ValueError("cosine selection requires finite nonempty embeddings")
                norm = math.sqrt(sum(v * v for v in vector))
                if not math.isfinite(norm) or norm <= 0:
                    raise ValueError("invalid embedding norm")
                dimensions.add(len(vector))
                feature = [v / norm for v in vector]
            else:
                p = row["payload"]
                # Exclude repeated source context; compare target answers to avoid rejecting all questions about one source.
                text = p.get("text")
                if text is None:
                    text = " ".join(
                        m["content"] if isinstance(m["content"], str) else " ".join(b.get("text", "") for b in m["content"])
                        for m in p["messages"]
                        if m["role"] == "assistant"
                    )
                text = re.sub(r"\s+", "", text).casefold()
                feature = {text[i : i + 3] for i in range(max(1, len(text) - 2))}
            self.features[row["id"]] = feature
        if len(dimensions) > 1:
            raise ValueError("embedding dimensions differ")
        self.identity = {**config, "scores_sha256": sha256(config["scores_file"]), "method": "deita-inspired-v1"}

    def rank(self, row):
        score = self.scores[row["id"]]
        return (-score["quality"] * score["complexity"], -score["quality"], row["id"])

    def accept(self, row):
        feature = self.features[row["id"]]
        for previous in self.chosen:
            if self.metric == "cosine":
                similarity = sum(a * b for a, b in zip(feature, previous))
            else:
                similarity = len(feature & previous) / max(1, len(feature | previous))
            if similarity >= self.threshold:
                return False
        self.chosen.append(feature)
        return True
