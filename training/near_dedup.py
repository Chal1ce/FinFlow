"""Persistent MinHash/LSH candidate retrieval with verified lexical similarity."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path

from core.flywheel_files import write_json
from training.flywheel_corpus import checksums, jsonl, read_jsonl, verify
from training.mixture_data import load_pool
from workflow.flywheel_config import digest

VERSION = "finflow-minhash-v1"
REPORT = "finflow-near-dedup-v1"
DEFAULTS = {"ngram": 5, "num_perm": 64, "bands": 16, "min_chars": 80, "threshold": 0.9, "max_candidates": 10000, "max_chars": 200000}
PRIME = (1 << 61) - 1
NUMBERS = re.compile(r"[+-]?\d+(?:[,.]\d+)*%?")
UNITS = re.compile(r"亿元|万元|千元|百万元|元|美元|人民币|港元|亿|万|%|百分比|百分点|千克|公斤|吨|万元/吨|usd|rmb|cny|hkd")


def settings(config=None):
    config = {} if config is None else config
    if not isinstance(config, dict) or set(config) - set(DEFAULTS):
        raise ValueError("unknown near-dedup settings")
    options = DEFAULTS | config
    for name, bounds in {
        "ngram": (2, 20),
        "num_perm": (16, 256),
        "bands": (1, 64),
        "min_chars": (1, 10000),
        "max_candidates": (1, 100000),
        "max_chars": (100, 1000000),
    }.items():
        if type(options[name]) is not int or not bounds[0] <= options[name] <= bounds[1]:
            raise ValueError("invalid near-dedup " + name)
    if options["num_perm"] % options["bands"] or options["min_chars"] > options["max_chars"]:
        raise ValueError("incompatible near-dedup dimensions")
    threshold = options["threshold"]
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0,1]")
    return options


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


def features(record, options, *, fingerprint=True):
    payload = record["payload"]
    scope = {"kind": record["kind"], "split": record["split"]}
    if record["kind"] == "cpt":
        text = normalize(payload["text"])
        scope["tokenizer"] = record["tokenizer"]
    else:
        scope.update(task=payload["task"], images=payload.get("image_sha256s", []))
        parts, contexts = [], []
        for message in payload["messages"]:
            content = message["content"]
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content)
            if message["role"] == "system":
                scope["system"] = digest(normalize(content))
                continue
            if message["role"] == "user" and content.startswith("材料：\n") and "\n\n任务：\n" in content:
                context, content = content.rsplit("\n\n任务：\n", 1)
                contexts.append(digest(normalize(context)))
            parts.append(message["role"] + ":" + normalize(content))
        scope["contexts"] = contexts
        text = "\n".join(parts)
    if len(text) > options["max_chars"]:
        raise ValueError("sample exceeds max_chars; increase limit explicitly, never silently truncate")
    if not fingerprint:
        return {"scope": digest(scope)}
    # Numerical changes and selected unit changes must not become automatic duplicates.
    guard = digest([NUMBERS.findall(text), UNITS.findall(text)])
    size = options["ngram"]
    shingles = sorted(
        {
            int.from_bytes(hashlib.blake2b(text[i : i + size].encode(), digest_size=8).digest(), "big") % PRIME
            for i in range(max(1, len(text) - size + 1))
        }
    )
    normalized_hash = hashlib.sha256(text.encode()).hexdigest()
    bands = []
    if len(text) >= options["min_chars"]:
        rng = random.Random(42)
        signature = []
        for _ in range(options["num_perm"]):
            a, b = rng.randrange(1, PRIME), rng.randrange(PRIME)
            signature.append(min((a * x + b) % PRIME for x in shingles))
        width = options["num_perm"] // options["bands"]
        bands = [digest(signature[i : i + width]) for i in range(0, len(signature), width)]
    return {
        "scope": digest(scope),
        "guard": guard,
        "normalized_hash": normalized_hash,
        "shingles": shingles,
        "bands": bands,
        "chars": len(text),
    }


class Index:
    def __init__(self, path, options, *, readonly=False):
        self.options = settings(options)
        self.path = Path(path).resolve()
        if readonly:
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.readonly = readonly

    def __enter__(self):
        try:
            if not self.readonly:
                tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables and tables != {"metadata", "documents", "buckets", "origins", "packages"}:
                    raise ValueError("not a near-dedup database; choose a dedicated index file")
                self.db.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS documents (
                        seq INTEGER PRIMARY KEY, uid TEXT UNIQUE NOT NULL, scope TEXT NOT NULL,
                        guard TEXT NOT NULL, normalized_hash TEXT NOT NULL, chars INTEGER NOT NULL,
                        shingles TEXT NOT NULL, bands TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS exact_lookup ON documents(scope,normalized_hash);
                    CREATE TABLE IF NOT EXISTS buckets (scope TEXT, band INTEGER, hash TEXT, uid TEXT,
                        PRIMARY KEY(scope,band,hash,uid));
                    CREATE TABLE IF NOT EXISTS origins (uid TEXT, origin_hash TEXT, origin_json TEXT,
                        PRIMARY KEY(uid,origin_hash));
                    CREATE TABLE IF NOT EXISTS packages (uid TEXT PRIMARY KEY, metadata_json TEXT);
                """)
                self.db.execute("BEGIN IMMEDIATE")
            else:
                self.db.execute("BEGIN")
            identity = json.dumps({"version": VERSION, "settings": self.options}, sort_keys=True)
            previous = self.db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            if previous is None and not self.readonly:
                self.db.execute("INSERT INTO metadata VALUES('identity',?)", (identity,))
            elif previous is None or previous[0] != identity:
                raise ValueError("index configuration differs; use a new index")
            return self
        except BaseException:
            self.db.close()
            raise

    def __exit__(self, error_type, *_):
        try:
            if error_type is None:
                self.db.commit()
            else:
                self.db.rollback()
        finally:
            self.db.close()

    def add(self, rows, manifest):
        added = 0
        for row in rows:
            previous = self.db.execute("SELECT scope FROM documents WHERE uid=?", (row["id"],)).fetchone()
            feature = features(row, self.options, fingerprint=previous is None)
            if previous and previous[0] != feature["scope"]:
                raise ValueError("indexed sample changed task, tokenizer, context or split")
            if not previous:
                self.db.execute(
                    "INSERT INTO documents(uid,scope,guard,normalized_hash,chars,shingles,bands) VALUES(?,?,?,?,?,?,?)",
                    (
                        row["id"],
                        feature["scope"],
                        feature["guard"],
                        feature["normalized_hash"],
                        feature["chars"],
                        json.dumps(feature["shingles"]),
                        json.dumps(feature["bands"]),
                    ),
                )
                self.db.executemany(
                    "INSERT INTO buckets VALUES(?,?,?,?)",
                    [(feature["scope"], band, value, row["id"]) for band, value in enumerate(feature["bands"])],
                )
                added += 1
            for origin in row["origins"]:
                self.db.execute(
                    "INSERT OR IGNORE INTO origins VALUES(?,?,?)", (row["id"], digest(origin), json.dumps(origin, ensure_ascii=False))
                )
        for package in manifest["inputs"]:
            self.db.execute("INSERT OR IGNORE INTO packages VALUES(?,?)", (package["package_uid"], json.dumps(package)))
        return {"status": "success", "added": added, "total": self.db.execute("SELECT count(*) FROM documents").fetchone()[0]}

    def candidates(self, feature):
        limit = self.options["max_candidates"]
        found = {
            r[0]
            for r in self.db.execute(
                "SELECT uid FROM documents WHERE scope=? AND normalized_hash=? LIMIT ?",
                (feature["scope"], feature["normalized_hash"], limit + 1),
            )
        }
        for band, value in enumerate(feature["bands"]):
            found.update(
                r[0]
                for r in self.db.execute(
                    "SELECT uid FROM buckets WHERE scope=? AND band=? AND hash=? LIMIT ?", (feature["scope"], band, value, limit + 1)
                )
            )
            if len(found) > limit:
                raise ValueError("candidate limit exceeded; no complete report can be published")
        if len(found) > limit:
            raise ValueError("candidate limit exceeded; no complete report can be published")
        return found

    def similarity(self, left, right):
        if left["guard"] != right["guard"]:
            return None
        if left["normalized_hash"] == right["normalized_hash"]:
            return 1.0
        if min(left["chars"], right["chars"]) < self.options["min_chars"]:
            return None
        a, b = set(json.loads(left["shingles"])), set(json.loads(right["shingles"]))
        score = len(a & b) / len(a | b)
        return score if score >= self.options["threshold"] else None


def index_packages(inputs, database, options=None):
    options = settings(options)
    for path in inputs:
        if Path(path).resolve() in Path(database).resolve().parents or Path(path).resolve() == Path(database).resolve():
            raise ValueError("index must be outside input packages")
    rows, manifest = load_pool(inputs, ["method"])
    with Index(database, options) as index:
        return index.add(rows, manifest)


def scan(inputs, database, output, options=None):
    options = settings(options)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    if any(Path(p).resolve() == output or Path(p).resolve() in output.parents for p in inputs):
        raise ValueError("report must be outside input packages")
    rows, pool = load_pool(inputs, ["method"])
    current = {r["id"]: r for r in rows}
    decisions, relations, keepers = [], [], set()
    with Index(database, options, readonly=True) as index:
        for package in pool["inputs"]:
            if not index.db.execute("SELECT 1 FROM packages WHERE uid=?", (package["package_uid"],)).fetchone():
                raise ValueError("index all current input packages before scanning")
        documents = {}
        for row in rows:
            document = index.db.execute("SELECT * FROM documents WHERE uid=?", (row["id"],)).fetchone()
            if document is None or document["scope"] != features(row, options, fingerprint=False)["scope"]:
                raise ValueError("index all current inputs with the same settings before scanning")
            documents[row["id"]] = document
        for uid in sorted(current, key=lambda key: documents[key]["seq"]):
            document = documents[uid]
            feature = dict(document) | {"bands": json.loads(document["bands"])}
            matches = []
            for candidate in index.candidates(feature) - {uid}:
                other = index.db.execute("SELECT * FROM documents WHERE uid=?", (candidate,)).fetchone()
                score = index.similarity(document, other)
                if score is not None:
                    matches.append((other["seq"], candidate, score))
            matches.sort()
            keeper = next((other for _, other, _ in matches if other in keepers), None)
            # Historical matches absent from this selected pool are report-only.
            action = "exclude" if keeper and current[uid]["split"] == "train" else "keep"
            if action == "keep":
                keepers.add(uid)
            decisions.append(
                {
                    "id": uid,
                    "content_hash": current[uid]["content_hash"],
                    "split": current[uid]["split"],
                    "action": action,
                    "representative": keeper if action == "exclude" else uid,
                    "reason": "near_duplicate_of_retained_sample" if action == "exclude" else "retained",
                }
            )
            for _, other, score in matches:
                if other in current and documents[other]["seq"] > document["seq"]:
                    continue
                relations.append(
                    {
                        "id": uid,
                        "matched_id": other,
                        "jaccard": score,
                        "historical_only": other not in current,
                        "origins": [
                            json.loads(r[0])
                            for r in index.db.execute("SELECT origin_json FROM origins WHERE uid=? ORDER BY origin_hash", (other,))
                        ],
                    }
                )
        index_packages_ids = [r[0] for r in index.db.execute("SELECT uid FROM packages ORDER BY uid")]
        manifest = {
            "schema_version": REPORT,
            "algorithm": VERSION,
            "settings": options,
            "inputs": pool["inputs"],
            "index_packages": index_packages_ids,
            "counts": dict(Counter(d["action"] for d in decisions)),
            "relations": len(relations),
            "policy": "oldest-indexed-current-representative; direct similarity; train-only removal",
            "scope": "lexical-near-duplicates; same split/task/context/image; not semantic equivalence",
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".near-dedup-", dir=output.parent))
    try:
        jsonl(staging / "decisions.jsonl", decisions)
        jsonl(staging / "relations.jsonl", relations)
        jsonl(staging / "origins.jsonl", [{"id": r["id"], "origins": r["origins"]} for r in rows])
        write_json(staging / "manifest.json", manifest)
        checksums(staging)
        verify(staging)
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"status": "success", "path": str(output), **manifest}


def apply_report(rows, pool, path):
    """Consume frozen decisions only when input identity and retained anchors match."""
    path = Path(path)
    manifest = verify(path)
    if manifest.get("schema_version") != REPORT:
        raise ValueError("unsupported near-dedup report")
    if sorted(p["package_uid"] for p in manifest["inputs"]) != sorted(p["package_uid"] for p in pool["inputs"]):
        raise ValueError("near-dedup report input packages changed; rescan")
    entries = read_jsonl(path / "decisions.jsonl")
    decisions = {d["id"]: d for d in entries}
    records = {r["id"]: r for r in rows}
    if len(entries) != len(decisions) or set(decisions) != set(records):
        raise ValueError("near-dedup decisions must cover the complete pool")
    for uid, row in records.items():
        decision = decisions[uid]
        if decision["content_hash"] != row["content_hash"] or decision["split"] != row["split"]:
            raise ValueError("near-dedup decision content mismatch")
        if decision["action"] not in {"keep", "exclude"}:
            raise ValueError("unknown near-dedup decision")
        if decision["action"] == "exclude":
            keeper = decisions.get(decision["representative"])
            if row["split"] != "train" or keeper is None or keeper["action"] != "keep" or keeper["split"] != row["split"]:
                raise ValueError("near-dedup exclusion has no retained training representative")
    return decisions, manifest
