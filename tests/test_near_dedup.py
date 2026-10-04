import hashlib
import random
import sqlite3
import string

import pytest

from core.flywheel_files import write_json
from training.flywheel_corpus import checksums, jsonl, read_jsonl, verify
from training.mixture import build, plan, verify_mixture
from training.near_dedup import Index, features, index_packages, scan, settings
from training.sft_export import export_files, sample_hash


def text():
    rng = random.Random(13)
    return " ".join("".join(rng.choices(string.ascii_lowercase, k=8)) for _ in range(100))


def cpt(root, name, texts, splits=None):
    path = root / name
    path.mkdir()
    rows, origins = [], []
    for i, body in enumerate(texts):
        split = splits[i] if splits else "train"
        uid = f"{name}-{i}"
        rows.append({"sample_id": uid, "text": body, "split": split, "token_count": 10})
        origins.append(
            {
                "sample_uid": uid,
                "content_hash": hashlib.sha256(body.encode()).hexdigest(),
                "split": split,
                "work_uid": uid,
                "method": "original",
                "decision": {"status": "accepted"},
                "source": {"source_name": name},
            }
        )
    jsonl(path / "corpus.jsonl", rows)
    jsonl(path / "provenance.jsonl", origins)
    jsonl(path / "excluded.jsonl", [])
    write_json(
        path / "manifest.json",
        {
            "schema_version": "continued-pretraining-snapshot-v2",
            "dataset_id": name,
            "samples": len(rows),
            "tokenizer": {"sha256": "tokenizer"},
        },
    )
    checksums(path)
    return str(path)


def test_incremental_index_idempotency_and_verified_near_match(tmp_path):
    body = text()
    old, new = cpt(tmp_path, "old", [body]), cpt(tmp_path, "new", [body + " z"])
    db = tmp_path / "index.db"
    assert index_packages([old], db)["added"] == 1
    assert index_packages([old], db)["added"] == 0
    assert index_packages([new], db)["total"] == 2
    output = tmp_path / "scan"
    scan([new, old], db, output)
    decisions = read_jsonl(output / "decisions.jsonl")
    assert [d["action"] for d in decisions] == ["keep", "exclude"]
    assert decisions[1]["representative"] == decisions[0]["id"]
    relation = read_jsonl(output / "relations.jsonl")[0]
    assert 0.9 <= relation["jaccard"] < 1
    assert relation["origins"][0]["source"]["source_name"] == "old"
    assert body not in (output / "relations.jsonl").read_text()
    verify(output)


def test_absent_history_is_report_only(tmp_path):
    old, new = cpt(tmp_path, "old", [text()]), cpt(tmp_path, "new", [text() + " z"])
    db = tmp_path / "index.db"
    index_packages([old], db)
    index_packages([new], db)
    scan([new], db, tmp_path / "scan")
    assert read_jsonl(tmp_path / "scan" / "decisions.jsonl")[0]["action"] == "keep"
    assert read_jsonl(tmp_path / "scan" / "relations.jsonl")[0]["historical_only"] is True


def test_same_content_new_package_must_register_its_origins(tmp_path):
    old = cpt(tmp_path, "old", [text()])
    new = cpt(tmp_path, "new", [text()])
    db = tmp_path / "index.db"
    index_packages([old], db)
    with pytest.raises(ValueError, match="input packages"):
        scan([new], db, tmp_path / "scan")
    assert index_packages([new], db)["added"] == 0
    scan([new], db, tmp_path / "scan")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM origins").fetchone()[0] == 2


def test_transitive_similarity_does_not_remove_unmatched_endpoint(tmp_path):
    rng = random.Random(99)
    suffixes = ["".join(rng.choices(string.ascii_lowercase, k=50)) for _ in range(2)]
    a = text()
    b = a + suffixes[0]
    c = b + suffixes[1]
    paths = [cpt(tmp_path, name, [value]) for name, value in zip(("a", "b", "c"), (a, b, c))]
    db = tmp_path / "index.db"
    for path in paths:
        index_packages([path], db, {"threshold": 0.93})
    scan(paths, db, tmp_path / "scan", {"threshold": 0.93})
    assert [d["action"] for d in read_jsonl(tmp_path / "scan" / "decisions.jsonl")] == ["keep", "exclude", "keep"]


def test_unrelated_database_untouched_and_failed_initial_batch_recoverable(tmp_path):
    source = cpt(tmp_path, "source", [text()])
    unrelated = tmp_path / "pipeline.db"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE business(value TEXT)")
    with pytest.raises(ValueError, match="dedicated"):
        index_packages([source], unrelated)
    with sqlite3.connect(unrelated) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("business",)]
    db = tmp_path / "new.db"
    with pytest.raises(ValueError, match="max_chars"):
        index_packages([source], db, {"max_chars": 100})
    assert index_packages([source], db)["added"] == 1


@pytest.mark.parametrize("suffix", ["收入2024亿元", "收入2025万元"])
def test_numbers_and_units_block_automatic_match(tmp_path, suffix):
    old = cpt(tmp_path, "old", [text() + "收入2025亿元"])
    new = cpt(tmp_path, "new", [text() + suffix])
    db = tmp_path / "index.db"
    index_packages([old, new], db)
    result = scan([old, new], db, tmp_path / "scan")
    assert result["counts"] == {"keep": 2}
    assert result["relations"] == 0


def test_short_text_only_normalized_exact_match_and_validation_retained(tmp_path):
    inputs = [
        cpt(tmp_path, "data", ["Hello world", "Hello   world", "Hello worlds", "Hello  WORLD"], ["train", "train", "train", "validation"])
    ]
    db = tmp_path / "index.db"
    index_packages(inputs, db)
    result = scan(inputs, db, tmp_path / "scan")
    assert result["counts"] == {"keep": 3, "exclude": 1}
    assert all(d["action"] == "keep" for d in read_jsonl(tmp_path / "scan" / "decisions.jsonl") if d["split"] == "validation")


def test_mixture_filters_and_bundles_frozen_decisions(tmp_path):
    old = cpt(tmp_path, "old", [text()])
    new = cpt(tmp_path, "new", [text() + " z"])
    inputs = [old, new]
    db, report = tmp_path / "index.db", tmp_path / "scan"
    index_packages([old], db)
    index_packages([new], db)
    scan(inputs, db, report)
    config = {
        "schema_version": "finflow-mixture-v1",
        "inputs": inputs,
        "near_dedup_report": str(report),
        "unit": "samples",
        "budget": 2,
        "allow_shortfall": True,
    }
    result = build(config, tmp_path / "mixture")
    assert result["train_samples"] == 1
    assert result["shortfall"] == 1
    assert len(read_jsonl(tmp_path / "mixture" / "near-dedup" / "origins.jsonl")) == 2
    assert verify_mixture(tmp_path / "mixture")["near_dedup"]["counts"]["exclude"] == 1
    with pytest.raises(ValueError, match="input packages changed"):
        plan(config | {"inputs": [old]})
    with pytest.raises(ValueError, match="learned weights"):
        plan(config | {"method": "doremi"})


def test_index_configuration_rejected_and_transaction_rolls_back(tmp_path):
    one = cpt(tmp_path, "one", [text()])
    db = tmp_path / "index.db"
    index_packages([one], db)
    with pytest.raises(ValueError, match="configuration"):
        index_packages([one], db, {"ngram": 3})
    with Index(db, settings(), readonly=True) as index:
        assert index.db.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    # All inserts in a batch roll back if a later sample fails.
    from training.mixture_data import load_pool

    rows, manifest = load_pool([one], ["method"])
    bad = dict(rows[0], id="new-id", payload={"text": "x" * 200001})
    with pytest.raises(ValueError, match="max_chars"):
        with Index(db, settings()) as index:
            index.add([dict(rows[0], id="temporary"), bad], manifest)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 1


def test_overflow_does_not_publish_incomplete_report(tmp_path):
    inputs = [cpt(tmp_path, "data", ["hello world", "hello  world"])]
    db = tmp_path / "index.db"
    index_packages(inputs, db, {"max_candidates": 1})
    with pytest.raises(ValueError, match="candidate limit"):
        scan(inputs, db, tmp_path / "scan", {"max_candidates": 1})
    assert not (tmp_path / "scan").exists()


def test_invalid_package_and_missing_index_fail_without_outputs(tmp_path):
    inputs = [cpt(tmp_path, "data", [text()])]
    with pytest.raises(sqlite3.OperationalError):
        scan(inputs, tmp_path / "missing.db", tmp_path / "scan")
    assert not (tmp_path / "missing.db").exists()
    with pytest.raises(ValueError, match="outside"):
        index_packages(inputs, tmp_path / "data" / "index.db")
    (tmp_path / "data" / "corpus.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="checksum"):
        index_packages(inputs, tmp_path / "new.db")
    assert not (tmp_path / "new.db").exists()


def test_sft_context_and_task_protection_and_portable_export(tmp_path):
    paths = []
    for name, answer, context in (
        ("old", text(), "original evidence"),
        ("new", text() + " z", "original evidence"),
        ("other", text() + " z", "different evidence"),
    ):
        path = tmp_path / name
        path.mkdir()
        row = {
            "sample_id": name,
            "evidence_uid": name,
            "work_uid": name,
            "artifact_uid": name,
            "task": "document_qa",
            "split": "train",
            "decision": {"status": "accepted"},
            "messages": [
                {"role": "system", "content": "use evidence"},
                {"role": "user", "content": "材料：\n" + context + "\n\n任务：\nExplain this passage"},
                {"role": "assistant", "content": answer},
            ],
        }
        row["content_hash"] = sample_hash(row)
        evidence = [{"evidence_uid": name, "source": {}}]
        jsonl(path / "samples.jsonl", [row])
        jsonl(path / "evidence.jsonl", evidence)
        media = export_files(tmp_path, path, [row], evidence)
        write_json(
            path / "manifest.json", {"schema_version": "financial-sft-dataset-v2", "dataset_id": name, "samples": 1, "recipe": {}, **media}
        )
        checksums(path)
        paths.append(str(path))
    db = tmp_path / "index.db"
    for path in paths:
        index_packages([path], db)
    result = scan(paths, db, tmp_path / "scan")
    assert result["counts"] == {"keep": 2, "exclude": 1}
    config = {
        "schema_version": "finflow-mixture-v1",
        "inputs": paths,
        "unit": "samples",
        "budget": 2,
        "near_dedup_report": str(tmp_path / "scan"),
    }
    build(config, tmp_path / "mixture")
    assert verify_mixture(tmp_path / "mixture")["train_samples"] == 2
    record = {"payload": row, "kind": "sft", "split": "train"}
    before = features(record, settings())["scope"]
    row["task"] = "extraction"
    assert before != features(record, settings())["scope"]
    row["image_sha256s"] = ["another-image"]
    assert before != features(record, settings())["scope"]
