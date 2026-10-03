import hashlib
import json

import pytest

from core.flywheel_files import write_json
from training.flywheel_corpus import checksums, jsonl, verify
from training.quality_report import daily_reports, inspect_package, publish_report
from training.sft_export import export_files, sample_hash


def package(root, name="dataset", texts=("private training text",), *, scope="snapshot", sources=("source-a",)):
    path = root / name
    path.mkdir()
    rows, origins = [], []
    for index, text in enumerate(texts):
        uid = f"sample-{index}"
        rows.append({"sample_id": uid, "text": text, "token_count": 5, "split": "train"})
        for source in sources:
            origins.append(
                {
                    "sample_uid": uid,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "split": "train",
                    "work_uid": f"work-{index}",
                    "method": "original",
                    "source": {"source_name": source},
                    "decision": {"status": "accepted"},
                }
            )
    jsonl(path / "corpus.jsonl", rows)
    jsonl(path / "provenance.jsonl", origins)
    jsonl(path / "excluded.jsonl", [{"reason": "source_not_approved"}])
    write_json(
        path / "manifest.json",
        {
            "schema_version": f"continued-pretraining-{scope}-v2",
            "dataset_id": name,
            "samples": len(rows),
            "tokenizer": {"sha256": "test-tokenizer"},
            "recipe_uid": "recipe",
        },
    )
    checksums(path)
    return path


def test_coverage_counts_each_source_once_and_keeps_text_out(tmp_path):
    path = package(tmp_path, texts=("secret sentence", "secret sentence"), sources=("a", "a", "b"))
    output = tmp_path / "report"
    result = publish_report(path, output, targets=[{"field": "task", "label": "table_calculation", "min_samples": 20}])
    report = json.loads((output / "report.json").read_text())
    assert result["metrics"]["samples"] == 2
    assert result["metrics"]["exact_duplicate_rate"] == 0.5
    assert report["coverage"]["source_name"] == {"a": 2, "b": 2}
    assert report["token_coverage"]["source_name"] == {"a": 10, "b": 10}
    assert report["coverage"]["language"] == {"unknown": 2}
    assert report["coverage_targets"][0]["missing_samples"] == 20
    assert "secret sentence" not in (output / "report.json").read_text()
    assert report["audit"]["acceptance_rate"] is None
    verify(output)


def test_comparison_rejects_delta_snapshot_and_finds_content_changes(tmp_path):
    first = package(tmp_path, "first", ("one", "two"))
    second = package(tmp_path, "second", ("two", "three"))
    publish_report(first, tmp_path / "baseline")
    publish_report(second, tmp_path / "next", baseline=tmp_path / "baseline")
    comparison = json.loads((tmp_path / "next" / "report.json").read_text())["comparison"]
    assert (comparison["added_content"], comparison["removed_content"], comparison["retained_content"]) == (1, 1, 1)
    delta = package(tmp_path, "delta", scope="delta")
    with pytest.raises(ValueError, match="scope"):
        publish_report(delta, tmp_path / "bad", baseline=tmp_path / "baseline")
    assert not (tmp_path / "bad").exists()


def test_corrupt_package_fails_and_report_does_not_modify_package(tmp_path):
    path = package(tmp_path)
    original = (path / "checksums.sha256").read_bytes()
    with pytest.raises(ValueError, match="outside"):
        publish_report(path, path / "report")
    publish_report(path, tmp_path / "report")
    with pytest.raises(FileExistsError):
        publish_report(path, tmp_path / "report")
    assert (path / "checksums.sha256").read_bytes() == original
    (path / "corpus.jsonl").write_text("{}\n")
    results = daily_reports(tmp_path, "run", {"cpt": {"path": str(path)}, "sft": {"status": "disabled"}})
    assert results["cpt"] == {"status": "failed", "error_type": "ValueError"}
    assert results["sft"] == {"status": "not_available"}


def test_empty_cpt_has_unavailable_rate(tmp_path):
    report = inspect_package(package(tmp_path, texts=()))
    assert report["metrics"]["tokens"] == 0
    assert report["metrics"]["exact_duplicate_rate"] is None
    assert "no_training_samples" in report["warnings"]


def test_partial_empty_sft_and_successful_daily_report(tmp_path):
    path = tmp_path / "sft"
    path.mkdir()
    for name in ("samples", "evidence", "audit", "images", "train", "validation", "train.vision", "validation.vision"):
        jsonl(path / (name + ".jsonl"), [])
    write_json(
        path / "manifest.json",
        {
            "schema_version": "financial-sft-dataset-v2",
            "dataset_id": "sft",
            "samples": 0,
            "images": 0,
            "modalities": {},
            "recipe": {},
            "status": "partial",
            "pending_jobs": 8,
        },
    )
    checksums(path)
    results = daily_reports(tmp_path, "run", {"sft": {"path": str(path)}})
    assert results["sft"]["status"] == "success"
    assert results["sft"]["metrics"]["tokens"] is None
    assert "partial_generation" in results["sft"]["warnings"]


def test_text_sft_counts_tasks_and_never_estimates_tokens(tmp_path):
    path = tmp_path / "sft"
    path.mkdir()
    row = {
        "sample_id": "s1",
        "evidence_uid": "e1",
        "work_uid": "work1",
        "task": "document_qa",
        "split": "validation",
        "decision": {"status": "accepted"},
        "messages": [
            {"role": "system", "content": "Use evidence"},
            {"role": "user", "content": "private context"},
            {"role": "assistant", "content": "private answer"},
        ],
    }
    row["content_hash"] = sample_hash(row)
    evidence = [{"evidence_uid": "e1", "source": {"source_name": "source-a", "language": "zh"}}]
    jsonl(path / "samples.jsonl", [row])
    jsonl(path / "evidence.jsonl", evidence)
    jsonl(path / "audit.jsonl", [{"status": "accepted", "reasons": ["private judge text"]}])
    media = export_files(tmp_path, path, [row], evidence)
    write_json(
        path / "manifest.json", {"schema_version": "financial-sft-dataset-v2", "dataset_id": "sft", "samples": 1, "recipe": {}, **media}
    )
    checksums(path)
    report = inspect_package(path)
    assert report["coverage"]["task"] == {"document_qa": 1}
    assert report["coverage"]["language"] == {"zh": 1}
    assert report["metrics"]["tokens"] is None
    assert report["token_coverage"] is None
    assert "private" not in json.dumps(report)
