"""Deterministic synthetic financial tasks for the first RL training run."""

from __future__ import annotations

import hashlib
import random
import shutil
from decimal import Decimal
from pathlib import Path

from agent.datasets.files import write_case
from agent.environment.cases import AGENT_ROOT, ENVIRONMENT_VERSION, REWARD_VERSION, write_json
from agent.environment.research import arithmetic
from agent.verifiers.research import rounded

DATASET_VERSION = "synthetic-research-0.2.0"
FAMILIES = ("revenue_growth", "cashflow_share", "cashflow_share_change", "dual_metric")
DIFFICULTY = {
    "revenue_growth": "easy",
    "cashflow_share": "medium",
    "cashflow_share_change": "hard",
    "dual_metric": "hard",
}


def _program(operation, *operands):
    return {"operation": operation, "operands": [item if isinstance(item, dict) else {"cell_ref": item} for item in operands]}


def _case(seed, split, index):
    key = hashlib.sha256(f"{DATASET_VERSION}:{seed}:{split}:{index}".encode()).hexdigest()[:12]
    rng = random.Random(key)
    family = FAMILIES[index % len(FAMILIES)]
    year = rng.randint(2020, 2025)
    periods = [str(year - 1), str(year)]
    entity, name = "entity-" + key, "合成企业" + key[:4]
    scope = rng.choice(["consolidated", "parent_company"])
    scope_label = {"consolidated": "合并", "parent_company": "母公司"}[scope]
    task_id, case_id = "task-" + key, "case-" + key
    tables, target_cells = [], None
    for table_scope in ("consolidated", "parent_company"):
        table_key = hashlib.sha256((key + table_scope).encode()).hexdigest()[:12]
        revenue0 = Decimal(rng.randint(10000, 200000)) / 100
        revenue1 = revenue0 * Decimal(rng.randint(65, 155)) / 100
        cash0 = revenue0 * Decimal(rng.randint(-10, 40)) / 100
        cash1 = revenue1 * Decimal(rng.randint(-10, 40)) / 100
        cells = [
            {"cell_id": f"cell-{table_key}-{position}", "row": row, "period": period, "value": format(value, "f"), "unit": "CNY_million"}
            for position, (row, period, value) in enumerate(
                [
                    ("营业收入", periods[0], revenue0),
                    ("经营活动现金流量净额", periods[0], cash0),
                    ("营业收入", periods[1], revenue1),
                    ("经营活动现金流量净额", periods[1], cash1),
                ]
            )
        ]
        rng.shuffle(cells)
        tables.append(
            {
                "table_id": "table-" + table_key,
                "title": "营业收入与经营活动现金流量净额",
                "scope": table_scope,
                "page": rng.randint(10, 80),
                "cells": cells,
            }
        )
        if table_scope == scope:
            target_cells = {int(cell["cell_id"].rsplit("-", 1)[1]): cell for cell in cells}
    rng.shuffle(tables)
    refs = {index: cell["cell_id"] for index, cell in target_cells.items()}
    values = {index: Decimal(cell["value"]) for index, cell in target_cells.items()}
    old, new = periods
    if family == "revenue_growth":
        question = f"计算营业收入从{old}年到{new}年的增长率（百分数）"
        metric_hint = "revenue_growth"
        specs = [
            {
                "metric_id": "revenue_growth",
                "unit": "percent",
                "periods": [old, new],
                "program": _program("growth_rate", refs[2], refs[0]),
                "evidence": [refs[0], refs[2]],
                "expected": arithmetic("growth_rate", values[2], values[0]),
            }
        ]
    elif family == "cashflow_share":
        question = f"计算{new}年经营活动现金流量净额占营业收入的比例（百分数）"
        metric_hint = "operating_cash_flow_share"
        specs = [
            {
                "metric_id": "operating_cash_flow_share",
                "unit": "percent",
                "periods": [new],
                "program": _program("percentage", refs[3], refs[2]),
                "evidence": [refs[2], refs[3]],
                "expected": arithmetic("percentage", values[3], values[2]),
            }
        ]
    elif family == "cashflow_share_change":
        question = f"计算经营活动现金流量净额占营业收入的比例，从{old}年到{new}年变化了多少个百分点"
        metric_hint = "operating_cash_flow_share_change"
        specs = [
            {
                "metric_id": "operating_cash_flow_share_change",
                "unit": "percentage_point",
                "periods": [old, new],
                "program": _program(
                    "pp_difference",
                    _program("percentage", refs[3], refs[2]),
                    _program("percentage", refs[1], refs[0]),
                ),
                "evidence": list(refs.values()),
                "expected": arithmetic(
                    "pp_difference",
                    arithmetic("percentage", values[3], values[2]),
                    arithmetic("percentage", values[1], values[0]),
                ),
            }
        ]
    else:
        question = (
            f"分别计算营业收入从{old}年到{new}年的增长率（百分数），"
            f"以及{new}年经营活动现金流量净额占营业收入的比例（百分数）"
        )
        metric_hint = "revenue_growth 和 operating_cash_flow_share（两条结果都要提交）"
        specs = [
            {
                "metric_id": "revenue_growth",
                "unit": "percent",
                "periods": [old, new],
                "program": _program("growth_rate", refs[2], refs[0]),
                "evidence": [refs[0], refs[2]],
                "expected": arithmetic("growth_rate", values[2], values[0]),
            },
            {
                "metric_id": "operating_cash_flow_share",
                "unit": "percent",
                "periods": [new],
                "program": _program("percentage", refs[3], refs[2]),
                "evidence": [refs[2], refs[3]],
                "expected": arithmetic("percentage", values[3], values[2]),
            },
        ]
    task = {
        "schema_version": "0.1.0",
        "task_id": task_id,
        "track": "research",
        "instruction": f"根据{name}的{year}年度报告，在{scope_label}口径下{question}。结果保留两位小数，"
        f"说明口径并提供支持单元格。结果 metric_id 使用 {metric_hint}。",
        "context": {"scope_id": "scope-" + key},
        "allowed_tools": ["search_documents", "read_table", "calculate", "submit_answer"],
        "budgets": {"max_actions": 8, "max_context_tokens": 8192, "max_output_tokens_per_turn": 512, "max_total_output_tokens": 4096},
        "output_contract": "research-answer-v0.1",
    }
    fixture = {
        "schema_version": "0.1.0",
        "synthetic": True,
        "scope_id": "scope-" + key,
        "track": "research",
        "initial_state_version": 0,
        "documents": [
            {
                "document_id": "doc-" + key,
                "work_id": "work-" + key,
                "entity_id": entity,
                "entity_name": name,
                "report_title": f"{name}{year}年度合成报告",
                "report_period": str(year),
                "currency": "CNY",
                "scale": "million",
                "tables": tables,
            }
        ],
        "files": [],
        "recovery_sources": [],
        "database": {"tasks": [], "sources": [], "candidates": [], "escalations": []},
        "protected_subjects": [],
        "lineage_edges": [],
    }
    oracle = {
        "schema_version": "0.1.0",
        "task_id": task_id,
        "track": "research",
        "accepted_outcomes": ["answered"],
        "checks": [
            {
                "kind": "research_result",
                "metric_id": spec["metric_id"],
                "expected_value": format(rounded(spec["expected"], 2), ".2f"),
                "unit": spec["unit"],
                "entity_id": entity,
                "scope": scope,
                "periods": spec["periods"],
                "decimal_places": 2,
                "evidence_alternatives": [spec["evidence"]],
                "reference_program": spec["program"],
            }
            for spec in specs
        ]
        + [{"kind": "evidence_observed", "all_submitted_refs": True}],
    }
    manifest = {
        "schema_version": "0.1.0",
        "case_id": case_id,
        "task_id": task_id,
        "track": "research",
        "synthetic": True,
        "split": split,
        "grouping": {
            "source_group": "work-" + key,
            "base_snapshot_group": "snapshot-" + key,
            "template_family": family,
            "variant_family": "variants-" + key,
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
    return manifest, {"task": task, "fixture": fixture, "oracle": oracle}


def build_research_dataset(output, *, train_cases=128, validation_cases=32, test_cases=0, seed=42):
    output = Path(output).resolve()
    if (
        type(train_cases) is not int
        or train_cases < 1
        or type(validation_cases) is not int
        or validation_cases < 0
        or type(test_cases) is not int
        or test_cases < 0
    ):
        raise ValueError("train-cases must be positive; validation/test-cases must be non-negative")
    if output.exists():
        raise ValueError("dataset output already exists; use a new dataset directory")
    output.mkdir(parents=True)
    shutil.copyfile(AGENT_ROOT / "tools.json", output / "tools.json")
    manifest = {
        "format": "finflow-research-dataset-v1",
        "version": DATASET_VERSION,
        "synthetic": True,
        "seed": seed,
        "purpose": "training integration; not a financial capability benchmark",
        "splits": {},
    }
    for split, count in (("train", train_cases), ("validation", validation_cases), ("test", test_cases)):
        manifest["splits"][split] = []
        for index in range(count):
            case, values = _case(seed, split, index)
            manifest["splits"][split].append(write_case(output, case, values))
    write_json(output / "manifest.json", manifest)
    return {
        "output": str(output),
        "version": DATASET_VERSION,
        "synthetic": True,
        "cases": {split: len(rows) for split, rows in manifest["splits"].items()},
    }
