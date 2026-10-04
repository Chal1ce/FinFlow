"""Offline coverage reports for verified training packages; never copy sample text."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from core.flywheel_files import sha256, write_json
from training.flywheel_corpus import checksums, read_jsonl, verify
from training.sft_export import sample_hash, verify_dataset

SCHEMA = "finflow-quality-report-v1"
CPT_SCHEMAS = {"continued-pretraining-delta-v2", "continued-pretraining-snapshot-v2", "training-lineage-delta-v1"}
FIELDS = ("source_name", "language", "method", "task", "strategy", "modality")


def inspect_package(path):
    path = Path(path).resolve()
    manifest = verify(path)
    schema = manifest.get("schema_version")
    mixed = schema == "finflow-mixture-dataset-v1"
    if mixed:
        from training.mixture import verify_mixture

        verify_mixture(path)
        kind = manifest["kind"]
        selected = read_jsonl(path / "records.jsonl")
        mixed_rows = {r["id"]: r for r in selected}
        rows = [r["payload"] for r in selected]
        audit = []
    elif schema == "financial-sft-dataset-v2":
        verify_dataset(path)
        kind = "sft"
        rows = read_jsonl(path / "samples.jsonl")
        evidence = {e["evidence_uid"]: e for e in read_jsonl(path / "evidence.jsonl")}
        origins = None
        audit = read_jsonl(path / "audit.jsonl")
    elif schema in CPT_SCHEMAS:
        kind = "cpt"
        rows = read_jsonl(path / "corpus.jsonl")
        origins = defaultdict(list)
        for origin in read_jsonl(path / "provenance.jsonl"):
            origins[origin["sample_uid"]].append(origin)
        audit = read_jsonl(path / "excluded.jsonl")
    else:
        raise ValueError("quality reports require CPT v2, lineage v1 or SFT v2 packages")
    distributions = {field: Counter() for field in (*FIELDS, "split")}
    token_distributions = {field: Counter() for field in (*FIELDS, "split")}
    inventory, families, image_splits = {}, defaultdict(set), defaultdict(set)
    tokens = 0
    ids = set()
    for row in rows:
        uid, split = row["sample_id"], row["split"]
        if uid in ids or split not in {"train", "validation"}:
            raise ValueError("duplicate sample ID or unsupported split")
        ids.add(uid)
        if mixed:
            selected = mixed_rows[uid]
            content, metadata = selected["content_hash"], selected["metadata"]
            works = set(selected["works"])
            count = selected["token_count"]
            if count is not None:
                tokens += count
        elif kind == "sft":
            if row.get("decision", {}).get("status") != "accepted":
                raise ValueError("unaccepted SFT sample")
            content = sample_hash(row)
            source = row.get("source", evidence[row["evidence_uid"]].get("source", {}))
            metadata = [
                {
                    **source,
                    "method": "sft",
                    "task": row["task"],
                    "strategy": row.get("strategy", "direct"),
                    "modality": row.get("modality", "text"),
                }
            ]
            works = {row["work_uid"]}
            count = None
        else:
            if not isinstance(row.get("text"), str) or not row["text"].strip():
                raise ValueError("empty CPT text")
            content = hashlib.sha256(row["text"].encode()).hexdigest()
            linked = origins[uid]
            if not linked or any(o["content_hash"] != content or o["split"] != split for o in linked):
                raise ValueError("CPT provenance mismatch")
            if any(o.get("decision", {}).get("status") != "accepted" for o in linked):
                raise ValueError("unaccepted CPT origin")
            metadata = [{**o.get("source", {}), "method": o["method"], "modality": "text"} for o in linked]
            works = {o["work_uid"] for o in linked}
            count = row.get("token_count")
            if type(count) is not int or count <= 0:
                raise ValueError("invalid CPT token count")
            tokens += count
        entry = inventory.setdefault(content, {"copies": 0, "splits": []})
        entry["copies"] += 1
        entry["splits"] = sorted(set(entry["splits"]) | {split})
        for work in works:
            families[work].add(split)
        for image in row.get("image_sha256s", []):
            image_splits[image].add(split)
        for field in distributions:
            labels = {split} if field == "split" else {str(m.get(field) or "unknown") for m in metadata}
            for label in labels:
                distributions[field][label] += 1
                if count is not None:
                    token_distributions[field][label] += count
    expected_samples = manifest["train_samples"] + manifest["validation_samples"] if mixed else manifest.get("samples")
    if expected_samples != len(rows):
        raise ValueError("manifest sample count mismatch")
    audit_statuses = Counter(str(a.get("status") or "excluded") for a in audit)
    # Machine reason codes only: free-form judge explanations may contain source text.
    reasons = Counter(str(a.get("reason") or "unspecified") for a in audit)
    scope = (
        "mixture" if mixed else "snapshot" if "snapshot" in schema or kind == "sft" else "lineage_only" if "lineage" in schema else "delta"
    )
    warnings = []
    conflicts = {
        "content": sum(len(v["splits"]) > 1 for v in inventory.values()),
        "work": sum(len(v) > 1 for v in families.values()),
        "image": sum(len(v) > 1 for v in image_splits.values()),
    }
    if any(conflicts.values()):
        warnings.append("cross_split_conflicts")
    if not rows:
        warnings.append("no_training_samples")
    if rows and not distributions["split"]["validation"]:
        warnings.append("no_validation_samples_in_package")
    if manifest.get("status") == "partial":
        warnings.append("mixture_shortfall" if mixed else "partial_generation")
    return {
        "schema_version": SCHEMA,
        "package": {
            "dataset_id": manifest.get("dataset_id", path.name),
            "kind": kind,
            "scope": scope,
            "schema_version": schema,
            "manifest_sha256": sha256(path / "manifest.json"),
            "inventory_sha256": sha256(path / "checksums.sha256"),
            "recipe_uid": hashlib.sha256(json.dumps(manifest["recipe"], sort_keys=True).encode()).hexdigest()
            if mixed
            else manifest.get("recipe_uid"),
            "tokenizer": manifest.get("tokenizer"),
            "status": manifest.get("status", "unspecified"),
        },
        "metrics": {
            "samples": len(rows),
            "unique_content": len(inventory),
            "exact_duplicate_copies": len(rows) - len(inventory),
            "exact_duplicate_rate": (len(rows) - len(inventory)) / len(rows) if rows else None,
            "tokens": tokens if kind == "cpt" else None,
            "works": len(families),
            "images": len(image_splits),
        },
        "coverage": {k: dict(sorted(v.items())) for k, v in distributions.items()},
        "token_coverage": {k: dict(sorted(v.items())) for k, v in token_distributions.items()} if kind == "cpt" else None,
        "audit": {
            "entries": len(audit),
            "statuses": dict(audit_statuses),
            "reason_codes": dict(reasons),
            "pending_jobs": manifest.get("pending_jobs"),
            "acceptance_rate": None,
        },
        "split_conflicts": conflicts,
        "warnings": warnings,
        "content_inventory": inventory,
        "limitations": [
            "coverage labels may overlap; totals across labels can exceed samples/tokens",
            "language labels describe recorded source metadata, not detected generated-text language",
            "audit entries are not a unique candidate denominator; acceptance rate is unavailable",
            "exact overlap within selected packages only; no semantic deduplication or correctness score",
            "SFT supervised token counts require a training tokenizer and chat template",
        ],
    }


def compare(current, previous):
    if previous.get("schema_version") != SCHEMA:
        raise ValueError("unsupported baseline report")
    for field in ("kind", "scope", "tokenizer"):
        if current["package"][field] != previous["package"][field]:
            raise ValueError("incomparable reports: " + field)
    old, new = set(previous["content_inventory"]), set(current["content_inventory"])
    return {
        "baseline_manifest_sha256": previous["package"]["manifest_sha256"],
        "recipe_changed": previous["package"]["recipe_uid"] != current["package"]["recipe_uid"],
        "added_content": len(new - old),
        "removed_content": len(old - new),
        "retained_content": len(old & new),
        "split_changed_content": sum(
            previous["content_inventory"][h]["splits"] != current["content_inventory"][h]["splits"] for h in old & new
        ),
        "metric_delta": {
            k: v - previous["metrics"][k] for k, v in current["metrics"].items() if v is not None and previous["metrics"].get(k) is not None
        },
        "coverage_delta": {
            field: {
                label: current["coverage"][field].get(label, 0) - previous["coverage"][field].get(label, 0)
                for label in sorted(set(values) | set(previous["coverage"][field]))
            }
            for field, values in current["coverage"].items()
        },
    }


def coverage_gaps(report, targets):
    gaps = []
    for target in targets:
        if set(target) != {"field", "label", "min_samples"} or target["field"] not in FIELDS:
            raise ValueError("targets require field, label and min_samples")
        if not isinstance(target["label"], str) or type(target["min_samples"]) is not int or target["min_samples"] < 0:
            raise ValueError("invalid coverage target")
        actual = report["coverage"][target["field"]].get(target["label"], 0)
        gaps.append({**target, "actual_samples": actual, "missing_samples": max(0, target["min_samples"] - actual)})
    return gaps


def markdown(report):
    def cell(value):
        return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")

    lines = [
        "# Training data quality and coverage",
        "",
        f"Dataset: {cell(report['package']['dataset_id'])}",
        "",
        f"Scope: {report['package']['scope']} · Kind: {report['package']['kind']}",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    lines.extend(f"| {k} | {v if v is not None else 'unavailable'} |" for k, v in report["metrics"].items())
    for field, values in report["coverage"].items():
        lines.extend(["", f"## {field}", "", "| Label | Samples |", "| --- | --- |"])
        lines.extend(f"| {cell(k)} | {v} |" for k, v in values.items())
    for key in ("audit", "split_conflicts", "comparison", "coverage_targets"):
        if key in report:
            lines.extend(["", f"## {key}", "", "<pre>" + html.escape(json.dumps(report[key], ensure_ascii=False, indent=2)) + "</pre>"])
    lines.extend(["", "## Warnings and interpretation", ""])
    lines.extend("- " + cell(w) for w in report["warnings"] + report["limitations"])
    return "\n".join(lines) + "\n"


def publish_report(package, output, *, baseline=None, targets=()):
    package, output = Path(package).resolve(), Path(output).resolve()
    if package == output or package in output.parents:
        raise ValueError("report output must be outside the immutable input package")
    if output.exists():
        raise FileExistsError(output)
    report = inspect_package(package)
    if baseline:
        previous = verify(baseline)
        if previous.get("schema_version") != SCHEMA:
            raise ValueError("unsupported baseline package")
        report["comparison"] = compare(report, json.loads((Path(baseline) / "report.json").read_text(encoding="utf-8")))
    report["coverage_targets"] = coverage_gaps(report, targets)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".report-", dir=output.parent))
    try:
        write_json(staging / "report.json", report)
        write_json(staging / "manifest.json", {"schema_version": SCHEMA, "package": report["package"]})
        (staging / "report.md").write_text(markdown(report), encoding="utf-8")
        checksums(staging)
        verify(staging)
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"status": "success", "path": str(output), "metrics": report["metrics"], "warnings": report["warnings"]}


def daily_reports(root, run_id, datasets):
    results = {}
    for name, dataset in datasets.items():
        if not dataset.get("path"):
            results[name] = {"status": "not_available"}
            continue
        try:
            results[name] = publish_report(dataset["path"], Path(root) / "reports" / "training" / run_id / name)
        except Exception as exc:
            # Keep published training packages and avoid exposing source/provider messages.
            results[name] = {"status": "failed", "error_type": type(exc).__name__}
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="verified CPT/SFT package")
    parser.add_argument("--output", type=Path, required=True, help="new report directory outside input")
    parser.add_argument("--baseline", type=Path, help="previous verified report directory")
    parser.add_argument("--targets", type=Path, help="JSON list of field/label/min_samples targets")
    args = parser.parse_args(argv)
    try:
        targets = json.loads(args.targets.read_text(encoding="utf-8")) if args.targets else []
        if not isinstance(targets, list):
            raise ValueError("targets must be a list")
        result = publish_report(args.input, args.output, baseline=args.baseline, targets=targets)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
