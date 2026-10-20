"""Reuse accepted SFT table transcriptions to build financial research tasks.

No model calls, review queue, independent split assignment, or invented values.
The first adapter supports explicit annual periods, CNY units and statement scope.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal
from itertools import product
from pathlib import Path

from agent.datasets.files import write_case
from agent.datasets.research import DIFFICULTY, FAMILIES, _program
from agent.environment.cases import AGENT_ROOT, ENVIRONMENT_VERSION, REWARD_VERSION, digest, write_json
from agent.verifiers.research import evaluate, rounded
from training.flywheel_corpus import jsonl, read_jsonl, verify

DATASET_VERSION = "governed-research-0.2.0"
ROW_NAMES = {
    "营业收入": "revenue",
    "经营活动产生的现金流量净额": "cashflow",
    "经营活动现金流量净额": "cashflow",
}
SCALES = {"元": "yuan", "万元": "ten_thousand", "百万元": "million", "亿元": "hundred_million"}


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


def _compact(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))


def _number(raw):
    if not isinstance(raw, str):
        return None
    value = _compact(raw).replace("−", "-")
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]
    # Commas must be valid thousands separators. Blank/dash/%/footnotes are not zero.
    if len(value) > 64 or not re.fullmatch(r"-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", value):
        return None
    value = value.replace(",", "")
    if len(value.replace("-", "").replace(".", "")) > 32:
        return None
    return format(Decimal(value), "f")


def _unit(raw):
    value = _compact(raw)
    # A bare 元 does not identify currency. Do not infer it from company location.
    if not any(currency in value for currency in ("人民币", "CNY", "RMB")):
        raise ValueError("currency_not_explicit")
    value = value.replace("单位:", "").replace("币种:", "")
    value = re.sub(r"人民币|CNY|RMB", "", value).strip("(),，;；")
    if value not in SCALES:
        raise ValueError("unsupported_or_ambiguous_unit")
    return "CNY", SCALES[value]


def _metadata(sample):
    source = sample["source"]
    code, year = str(source.get("stock_code", "")), str(source.get("report_year", ""))
    if not re.fullmatch(r"[0-9]{6}", code) or not re.fullmatch(r"(?:19|20)[0-9]{2}", year):
        raise ValueError("missing_financial_report_identity")
    if source.get("report_type") != "annual":
        raise ValueError("annual_reports_only")
    return code, int(year)


def _table(sample, evidence):
    code, year = _metadata(sample)
    table = sample.get("table")
    if not isinstance(table, dict) or set(table) != {"title", "unit", "columns", "rows"}:
        raise ValueError("missing_structured_table")
    title = table["title"]
    if not isinstance(title, str) or not title.strip():
        raise ValueError("missing_table_title")
    scopes = [scope for label, scope in (("合并", "consolidated"), ("母公司", "parent_company")) if label in title]
    if len(scopes) != 1:
        raise ValueError("scope_not_explicit_or_ambiguous")
    currency, scale = _unit(table["unit"])
    columns, rows = table["columns"], table["rows"]
    if not isinstance(columns, list) or not columns or not all(isinstance(column, str) for column in columns):
        raise ValueError("invalid_table_columns")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
        raise ValueError("non_rectangular_table")
    periods = {}
    for index, column in enumerate(columns):
        match = re.fullmatch(r"((?:19|20)[0-9]{2})(?:年|年度)?", _compact(column))
        if match and int(match[1]) in {year - 1, year}:
            if match[1] in periods.values():
                raise ValueError("duplicate_annual_period_columns")
            periods[index] = match[1]
    if not periods:
        raise ValueError("no_explicit_annual_period_columns")
    page = evidence.get("page")
    if type(page) is not int or page < 1:
        raise ValueError("missing_source_page")
    cells, coordinates, metrics = [], [], {}
    table_id = "table-" + _key(sample["sample_id"], sample["content_hash"])
    for row_index, row in enumerate(rows):
        # Locate the row label outside the numerical period columns, without assuming
        # it is column zero (an extra note/reference column is common).
        labels = []
        for column_index, raw in enumerate(row):
            if column_index not in periods and isinstance(raw, str):
                label = re.sub(r"^(?:其中:|[一二三四五六七八九十]+[、.])", "", _compact(raw))
                if label in ROW_NAMES:
                    labels.append((ROW_NAMES[label], raw))
        if len(labels) != 1:
            continue
        metric, label = labels[0]
        for column_index, period in periods.items():
            raw, value = row[column_index], _number(row[column_index])
            if value is None:
                continue
            if (metric, period) in metrics:
                raise ValueError("duplicate_metric_rows")
            cell = {
                "cell_id": "cell-" + _key(table_id, row_index, column_index),
                "row": label.strip(),
                "period": period,
                "value": value,
                "unit": currency + "_" + scale,
            }
            cells.append(cell)
            metrics[metric, period] = cell
            coordinates.append({"cell_id": cell["cell_id"], "row_index": row_index, "column_index": column_index, "raw_value": raw})
    if not cells:
        raise ValueError("no_supported_numeric_rows")
    return {
        "code": code,
        "year": year,
        "currency": currency,
        "scale": scale,
        "work_uid": sample["work_uid"],
        "split": sample["split"],
        "source": sample["source"],
        "metrics": metrics,
        "table": {"table_id": table_id, "title": title + "（受控数值行投影）", "scope": scopes[0], "page": page, "cells": cells},
        "provenance": {"table_id": table_id, "sample": sample, "evidence": evidence, "cell_coordinates": coordinates},
    }


def _case(tables, chosen, family, bindings):
    first = tables[0]
    year, work, split = first["year"], first["work_uid"], first["split"]
    old, new = str(year - 1), str(year)
    scope = first["table"]["scope"]
    required_keys = {
        "revenue_growth": [("revenue", old), ("revenue", new)],
        "cashflow_share": [("revenue", new), ("cashflow", new)],
        "cashflow_share_change": [("revenue", old), ("cashflow", old), ("revenue", new), ("cashflow", new)],
    }[family]
    if any(key not in chosen for key in required_keys):
        raise ValueError("missing_or_conflicting_required_cells")
    cells = {key: chosen[key]["cell"] for key in required_keys}
    denominators = [("revenue", old)] if family == "revenue_growth" else [("revenue", new)]
    if family == "cashflow_share_change":
        denominators.append(("revenue", old))
    if any(Decimal(cells[key]["value"]) == 0 for key in denominators):
        raise ValueError("zero_denominator")
    refs = {key: cell["cell_id"] for key, cell in cells.items()}
    if family == "revenue_growth":
        metric, unit, periods = "revenue_growth", "percent", [old, new]
        question = f"计算营业收入从{old}年到{new}年的增长率（百分数）"
        program = _program("growth_rate", refs["revenue", new], refs["revenue", old])
    elif family == "cashflow_share":
        metric, unit, periods = "operating_cash_flow_share", "percent", [new]
        question = f"计算{new}年经营活动现金流量净额占营业收入的比例（百分数）"
        program = _program("percentage", refs["cashflow", new], refs["revenue", new])
    else:
        metric, unit, periods = "operating_cash_flow_share_change", "percentage_point", [old, new]
        question = f"计算经营活动现金流量净额占营业收入的比例，从{old}年到{new}年变化了多少个百分点"
        program = _program(
            "pp_difference",
            _program("percentage", refs["cashflow", new], refs["revenue", new]),
            _program("percentage", refs["cashflow", old], refs["revenue", old]),
        )
    # At most two required tables + distractors, so the reference path fits eight actions.
    required_ids = {chosen[key]["table_id"] for key in required_keys}
    selected = [item for item in tables if item["table"]["table_id"] in required_ids]
    selected += [item for item in tables if item["table"]["table_id"] not in required_ids][: max(0, 4 - len(selected))]
    if len(required_ids) > 2:
        raise ValueError("reference_path_exceeds_table_budget")
    key = _key(DATASET_VERSION, bindings["manifest_sha256"], work, scope, first["scale"], family)
    case_id, task_id, scope_id = "case-" + key, "task-" + key, "scope-" + key
    source = first["source"]
    entity = "stock:" + first["code"]
    name = source.get("company_name") or first["code"]
    title = source.get("title") or f"{first['code']} {year}年度报告"
    task = {
        "schema_version": "0.1.0",
        "task_id": task_id,
        "track": "research",
        "instruction": f"根据《{title}》中提供的数值行，在{'合并' if scope == 'consolidated' else '母公司'}口径下{question}。"
        f"结果保留两位小数，说明口径并提供支持单元格。结果 metric_id 使用 {metric}。",
        "context": {"scope_id": scope_id},
        "allowed_tools": ["search_documents", "read_table", "calculate", "submit_answer"],
        "budgets": {"max_actions": 8, "max_context_tokens": 8192, "max_output_tokens_per_turn": 512, "max_total_output_tokens": 4096},
        "output_contract": "research-answer-v0.1",
    }
    fixture = {
        "schema_version": "0.1.0",
        "synthetic": False,
        "scope_id": scope_id,
        "track": "research",
        "initial_state_version": 0,
        "documents": [
            {
                "document_id": "doc-" + _key(work),
                "work_id": work,
                "entity_id": entity,
                "entity_name": name,
                "report_title": title,
                "report_period": str(year),
                "currency": first["currency"],
                "scale": first["scale"],
                "tables": [item["table"] for item in selected],
            }
        ],
        "files": [],
        "recovery_sources": [],
        "database": {"tasks": [], "sources": [], "candidates": [], "escalations": []},
        "protected_subjects": [],
        "lineage_edges": [],
    }
    nodes = {cell["cell_id"]: {"value": Decimal(cell["value"])} for cell in cells.values()}
    primary_support = sorted(refs.values())
    equivalent_options = [
        [
            item["metrics"][metric]["cell_id"]
            for item in selected
            if metric in item["metrics"] and Decimal(item["metrics"][metric]["value"]) == Decimal(cells[metric]["value"])
        ]
        for metric in required_keys
    ]
    supports = sorted({tuple(sorted(support)) for support in product(*equivalent_options)})
    # Keep the reference program's own cells first for offline demonstration execution.
    alternatives = [primary_support, *[list(support) for support in supports if list(support) != primary_support]]
    oracle = {
        "schema_version": "0.1.0",
        "task_id": task_id,
        "track": "research",
        "accepted_outcomes": ["answered"],
        "checks": [
            {
                "kind": "research_result",
                "metric_id": metric,
                "expected_value": format(rounded(evaluate(program, nodes), 2), ".2f"),
                "unit": unit,
                "entity_id": entity,
                "scope": scope,
                "periods": periods,
                "decimal_places": 2,
                "evidence_alternatives": alternatives,
                "reference_program": program,
            },
            {"kind": "evidence_observed", "all_submitted_refs": True},
        ],
    }
    case = {
        "schema_version": "0.1.0",
        "case_id": case_id,
        "task_id": task_id,
        "track": "research",
        "synthetic": False,
        "split": split,
        "grouping": {
            "source_group": work,
            "base_snapshot_group": "report-" + _key(work),
            "template_family": family,
            "variant_family": "variants-" + _key(work),
            "difficulty": DIFFICULTY[family],
        },
        "versions": {
            "environment": ENVIRONMENT_VERSION,
            "reward": REWARD_VERSION,
            "fingerprint": "research-observation-state-0.2.0",
            "dataset": DATASET_VERSION,
        },
        "files": {},
    }
    provenance = {
        "format": "finflow-research-provenance-v1",
        "case_id": case_id,
        "work_uid": work,
        "split": split,
        "source": bindings,
        "tables": [item["provenance"] for item in selected],
    }
    return case, {"task": task, "fixture": fixture, "oracle": oracle, "provenance": provenance}


def build_governed_dataset(dataset, output):
    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    if output.exists() or dataset == output or dataset in output.parents or output in dataset.parents:
        raise ValueError("governed RL output needs a new directory outside the source SFT snapshot")
    # Reuse the existing frozen-package checksum contract. No semantic/image re-review.
    upstream = verify(dataset)
    if upstream.get("schema_version") != "financial-sft-dataset-v2" or upstream.get("status") not in {"success", "partial"}:
        raise ValueError("rl-import requires an existing governed financial-sft-dataset-v2 snapshot")
    bindings = {
        "path": str(dataset),
        "manifest_sha256": digest(dataset / "manifest.json"),
        "samples_sha256": digest(dataset / "samples.jsonl"),
        "evidence_sha256": digest(dataset / "evidence.jsonl"),
    }
    evidence_rows = read_jsonl(dataset / "evidence.jsonl")
    evidence = {row["evidence_uid"]: row for row in evidence_rows}
    if len(evidence) != len(evidence_rows):
        raise ValueError("duplicate evidence identity in source SFT snapshot")
    buckets, excluded, groups, identities, sample_ids = defaultdict(list), [], {}, {}, set()
    for sample in sorted(read_jsonl(dataset / "samples.jsonl"), key=lambda row: row["sample_id"]):
        if sample.get("task") != "table_structure":
            continue
        uid = sample["sample_id"]
        if uid in sample_ids:
            raise ValueError("duplicate source table sample identity")
        sample_ids.add(uid)
        if sample.get("decision", {}).get("status") != "accepted":
            excluded.append({"sample_id": uid, "reason": "upstream_sample_not_accepted"})
            continue
        item = evidence.get(sample["evidence_uid"])
        if not item or any(sample[key] != item[key] for key in ("work_uid", "candidate_uid", "source", "split")):
            raise ValueError("accepted table/evidence identity or inherited split mismatch")
        split = sample["split"]
        if split not in {"train", "validation"}:
            excluded.append({"sample_id": uid, "reason": "no_inherited_training_split"})
            continue
        if not sample.get("review_artifact_uid") or sample.get("image_sha256s") != [item.get("image_sha256")]:
            raise ValueError("accepted table review/image binding is missing")
        for group in (("work", item["work_uid"]), ("image", item["image_sha256"]), ("content", item["content_hash"])):
            previous = groups.setdefault(group, split)
            if previous != split:
                raise ValueError("inherited source/content/image group crosses splits")
        try:
            table = _table(sample, item)
        except ValueError as exc:
            excluded.append({"sample_id": uid, "reason": str(exc)})
            continue
        identity = (table["code"], table["year"])
        previous = identities.setdefault(table["work_uid"], identity)
        if identity != previous:
            raise ValueError("one logical work has conflicting financial report identities")
        buckets[table["work_uid"], table["table"]["scope"], table["currency"], table["scale"]].append(table)
    cases = []
    for bucket, tables in sorted(buckets.items()):
        tables.sort(key=lambda item: item["table"]["table_id"])
        candidates = defaultdict(list)
        for table in tables:
            for metric, cell in table["metrics"].items():
                candidates[metric].append({"cell": cell, "table_id": table["table"]["table_id"]})
        chosen = {}
        for metric, options in sorted(candidates.items()):
            if len({Decimal(option["cell"]["value"]) for option in options}) == 1:
                chosen[metric] = options[0]
            else:
                excluded.append({"work_uid": bucket[0], "scope": bucket[1], "metric": list(metric), "reason": "conflicting_numeric_values"})
        for family in FAMILIES[:3]:
            try:
                cases.append(_case(tables, chosen, family, bindings))
            except ValueError as exc:
                excluded.append({"work_uid": bucket[0], "scope": bucket[1], "family": family, "reason": str(exc)})
    counts = Counter(case["split"] for case, _ in cases)
    reasons = dict(sorted(Counter(row["reason"] for row in excluded).items()))
    if not counts["train"]:
        raise ValueError("no_supported_training_cases: " + json.dumps(reasons, ensure_ascii=False))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".research-import-", dir=output.parent) as temporary:
        staging = Path(temporary) / "dataset"
        staging.mkdir()
        shutil.copyfile(AGENT_ROOT / "tools.json", staging / "tools.json")
        manifest = {
            "format": "finflow-research-dataset-v1",
            "version": DATASET_VERSION,
            "synthetic": False,
            "purpose": "research training from accepted financial table transcriptions; not a frozen benchmark",
            "observation_modality": "structured_table_text",
            "governance": "inherited_from_accepted_sft_tables",
            "source": bindings,
            "splits": {"train": [], "validation": []},
            "exclusions": reasons,
        }
        for case, values in cases:
            manifest["splits"][case["split"]].append(write_case(staging, case, values))
        jsonl(staging / "excluded.jsonl", excluded)
        manifest["excluded_sha256"] = digest(staging / "excluded.jsonl")
        write_json(staging / "manifest.json", manifest)
        staging.rename(output)
    return {"output": str(output), "version": DATASET_VERSION, "synthetic": False, "cases": dict(counts), "exclusions": reasons}
