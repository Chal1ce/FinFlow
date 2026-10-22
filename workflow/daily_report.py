"""Per-run and per-calendar-day reports; cumulative inventories are never summed."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.flywheel_files import write_json
from core.diagnostics import download_diagnostic, governance_diagnostic


def diagnose_run(root, store, run):
    """Recover useful context for legacy summaries as well as new structured errors."""
    run = dict(run)
    errors = []
    for error in run.get("errors", []):
        detail = dict(error)
        row = store.connection.execute(
            "SELECT payload_json FROM processing_task WHERE task_uid=?", (error.get("task_uid"),)
        ).fetchone()
        if row:
            payload = json.loads(row[0])
            source = payload.get("source") or payload
            detail.setdefault("source", source.get("source_name"))
            if error.get("stage") == "govern" and detail.get("code") in {None, "stage_failed", "governance_failed"}:
                steps = store.connection.execute(
                    "SELECT p.run_id,p.error_msg,p.metadata_json FROM pipeline_step p JOIN pipeline_run r ON r.run_id=p.run_id "
                    "WHERE p.step_name='govern' AND p.status='failed' AND r.batch_id=? "
                    "AND r.started_at>=? AND r.started_at<=? ORDER BY p.step_id DESC",
                    (run.get("batch_id"), run.get("started_at", ""), run.get("finished_at", run.get("created_at", ""))),
                )
                for step in steps:
                    metadata = json.loads(step["metadata_json"] or "{}")
                    if metadata.get("asset_uid") != payload.get("asset_uid") or metadata.get("backend") != payload.get("backend"):
                        continue
                    diagnostic = governance_diagnostic(dict(step))
                    if diagnostic.get("error_type"):
                        detail.update(diagnostic, details_from="nested_governance_step", nested_run_id=step["run_id"])
                    break
            if error.get("stage") == "download" and not detail.get("code"):
                manifest = Path(root) / "manifests" / "reports.jsonl"
                if manifest.is_file():
                    record = None
                    with manifest.open(encoding="utf-8") as handle:
                        for line in handle:
                            try:
                                candidate = json.loads(line)
                            except ValueError:
                                continue
                            if candidate.get("source_url") == source.get("source_url"):
                                record = candidate
                    if record and record.get("status") == "failed":
                        detail.update(download_diagnostic(record))
                        detail["details_from"] = "current_download_manifest"
        detail.setdefault("message", "阶段执行失败；旧版本只记录了错误类型。")
        detail.setdefault("hint", "查看 stage/task_uid 对应的本地任务记录；新运行会记录结构化原因。")
        errors.append(detail)
    run["errors"] = errors
    if "stop_reasons" not in run:
        deferred = store.connection.execute(
            "SELECT 1 FROM pipeline_step p JOIN processing_task t "
            "ON t.task_uid=json_extract(p.metadata_json,'$.task_uid') "
            "WHERE p.run_id=? AND p.status='deferred' AND json_extract(t.result_json,'$.reason')='model_budget' LIMIT 1",
            (run["run_id"],),
        ).fetchone()
        run["stop_reasons"] = (
            [{"code": "model_budget", "message": "本轮模型请求额度已用完，可续跑已有队列。"}] if deferred else []
        )
    return run


def inventory(store):
    return {
        name: store.connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
        for name, table in (
            ("candidates", "training_candidate"),
            ("visual_assets", "visual_asset"),
            ("cpt_samples", "training_sample"),
            ("cpt_origins", "training_origin"),
            ("artifacts", "artifact"),
        )
    } | {
        name: store.connection.execute("SELECT count(*) FROM flywheel_record WHERE kind=?", (kind,)).fetchone()[0]
        for name, kind in (("sft_samples", "sft-sample"), ("sources", "source"), ("documents", "document"))
    }


def processing_stages(store, run_id):
    return [
        dict(row)
        for row in store.connection.execute(
            "SELECT step_name,status,count(*) AS attempts, "
            "round(sum((julianday(finished_at)-julianday(started_at))*86400),3) AS elapsed_seconds "
            "FROM pipeline_step WHERE run_id=? GROUP BY step_name,status ORDER BY step_name,status",
            (run_id,),
        )
    ]


def refiner_summary(store, run_id):
    """Summarize the latest outcome of each plugin task attempted in this run."""
    records = [
        json.loads(row[0])
        for row in store.connection.execute(
            "SELECT t.result_json FROM processing_task t WHERE t.kind='refine' AND t.result_json IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM pipeline_step p WHERE p.run_id=? AND p.step_name='flywheel-refine' "
            "AND json_extract(p.metadata_json,'$.task_uid')=t.task_uid)",
            (run_id,),
        )
    ]
    return {
        "attempted_tasks": len(records),
        "completed": sum("refiner_status" in r for r in records),
        "actions": dict(Counter(r.get("action", "none") for r in records)),
        "statuses": dict(Counter(r.get("refiner_status", r.get("error_type", "deferred")) for r in records)),
        "response_cache_hits": sum(bool(r.get("response_cache_hit")) for r in records),
        "candidates": sum(bool(r.get("candidate_uid")) for r in records),
        "input_chars": sum(r.get("input_chars", 0) for r in records),
        "output_chars": sum(r.get("output_chars", 0) for r in records),
    }


def aggregate(runs, date, timezone):
    runs = sorted(runs, key=lambda r: (r["created_at"], r["run_id"]))
    additions = Counter()
    for run in runs:
        additions.update(run.get("additions", {}))
    refiner = {
        key: sum(run.get("refiner", {}).get(key, 0) for run in runs)
        for key in ("attempted_tasks", "completed", "response_cache_hits", "candidates", "input_chars", "output_chars")
    }
    for key in ("actions", "statuses"):
        counts = Counter()
        for run in runs:
            counts.update(run.get("refiner", {}).get(key, {}))
        refiner[key] = dict(counts)
    return {
        "schema_version": "finflow-daily-report-v1",
        "date": date,
        "timezone": timezone,
        "run_count": len(runs),
        "statuses": dict(Counter(r["status"] for r in runs)),
        "completed_tasks": sum(r.get("completed_tasks", 0) for r in runs),
        "model_requests": sum(r.get("model_requests") or 0 for r in runs),
        "errors": [{"run_id": run["run_id"], **error} for run in runs for error in run.get("errors", [])],
        "stop_reasons": [{"run_id": run["run_id"], **reason} for run in runs for reason in run.get("stop_reasons", [])],
        "usage_missing": any(r.get("model_usage") is None for r in runs),
        "additions": dict(additions),
        "refiner": refiner,
        "published_versions": sorted(
            {r["training_release"]["release_id"] for r in runs if r.get("training_release", {}).get("status") == "success"}
        ),
        "latest": runs[-1] if runs else None,
        "runs": [
            {
                k: r.get(k)
                for k in (
                    "run_id",
                    "status",
                    "created_at",
                    "dataset",
                    "sft_dataset",
                    "training_release",
                    "additions",
                    "errors",
                    "stop_reasons",
                    "processing_scope",
                    "active_queue",
                    "processing_stages",
                    "model_usage",
                    "sft_model_usage",
                    "quality_reports",
                    "refiner",
                    "elapsed_seconds",
                )
            }
            for r in runs
        ],
        "scope": "additions sum database insertions; queue, coverage and dataset sizes are snapshots",
    }


def markdown(report):
    latest = report["latest"] or {}
    lines = [
        f"# FinFlow 日报 {report['date']}",
        "",
        f"时区：{report['timezone']}",
        "",
        f"运行 {report['run_count']} 次；完成任务 {report['completed_tasks']}；模型请求 {report['model_requests']}。",
        "",
        "## 本日数据库新增",
        "",
        "| 项目 | 数量 |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {k} | {v} |" for k, v in report["additions"].items())
    lines += [
        "",
        "新增数按数据库登记计数；恢复登记也算新增。累计快照大小不代表今天生成量。",
        "",
        "## 最新运行与训练版本",
        "",
        f"运行状态：{latest.get('status', 'not_available')}",
        "",
        f"队列：`{json.dumps(latest.get('queue', {}), ensure_ascii=False)}`",
        "",
        f"该轮处理范围：{latest.get('processing_scope', 'all')}；范围内队列："
        f"`{json.dumps(latest.get('active_queue', latest.get('queue', {})), ensure_ascii=False)}`",
        "",
        f"候选审核累计：`{json.dumps(latest.get('candidates', {}), ensure_ascii=False)}`",
        "",
        f"图表累计：`{json.dumps(latest.get('visuals', {}), ensure_ascii=False)}`",
        "",
        f"本日精炼插件（各轮处理合计）：`{json.dumps(report.get('refiner', {}), ensure_ascii=False)}`",
        "",
    ]
    lines += ["## 失败与停止原因", ""]
    for error in report.get("errors", []):
        message = str(error.get("message") or error.get("error_type", "unknown")).replace("\n", " ")
        lines += [
            f"- {error['run_id']} / {error.get('stage', 'unknown')} / {error.get('source') or 'unknown'}：{message}",
            f"  - 建议：{error.get('hint', '查看同名 JSON 的任务信息。')}",
            f"  - task_uid：`{error.get('task_uid', 'none')}`；隔离文件：`{error.get('quarantine_path', 'none')}`",
        ]
    for reason in report.get("stop_reasons", []):
        lines.append(f"- {reason['run_id']}：{reason['message']}")
    if not report.get("errors") and not report.get("stop_reasons"):
        lines.append("本日所选运行没有记录错误或额度停止。")
    lines += [""]
    release = latest.get("training_release", {})
    lines += [
        f"发布状态：{release.get('status', 'disabled')}",
        "",
        f"原因：{release.get('reason', 'none')}；阶段：{release.get('stage', 'completed')}",
        "",
        f"当前可用版本：{(release.get('latest') or {}).get('release_id', 'none')}",
        "",
    ]
    for kind, track in release.get("tracks", {}).items():
        lines += [
            f"### {kind.upper()}",
            "",
            f"输入 {track['input_samples']}；近似去重排除 {track['near_dedup']['excluded']}；"
            f"训练 {track['train_samples']}；验证 {track['validation_samples']}。",
            "",
            f"配比目标 {track['target']} {track['unit']}；实际 {track['actual']}；缺口 {track['shortfall']}。",
            "",
            "| 分组 | 可用 | 目标 | 选入 |",
            "| --- | ---: | ---: | ---: |",
        ]
        lines.extend(f"| {k.replace('|', '/')} | {v['available']} | {v['quota']} | {v['selected']} |" for k, v in track["groups"].items())
        lines += [""]
    lines += ["## 各轮执行", "", "| 运行 | 状态 | 发布 | 错误类型 |", "| --- | --- | --- | --- |"]
    for run in report["runs"]:
        errors = ", ".join(e.get("error_type", "unknown") for e in run.get("errors") or [])
        lines.append(f"| {run['run_id']} | {run['status']} | {(run.get('training_release') or {}).get('status', 'disabled')} | {errors} |")
    lines += ["", "完整阶段恢复记录、覆盖变化、模型 usage 和失败阶段见同名 JSON 与版本 reports/。", ""]
    return "\n".join(lines)


def persist_daily(root, store, summary, *, timezone="Asia/Shanghai"):
    zone = ZoneInfo(timezone)
    root = Path(root)
    summary["report_timezone"] = timezone
    summary.update(diagnose_run(root, store, summary))
    write_json(root / "manifests" / "daily" / (summary["run_id"] + ".json"), summary)
    store.put("daily", summary["run_id"], summary)
    # Recover a run report written before its DB insert, without executing the pipeline again.
    for path in (root / "manifests" / "daily").glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if not store.get("daily", record["run_id"]):
            store.put("daily", record["run_id"], record)
    date = datetime.fromisoformat(summary["created_at"]).astimezone(zone).date().isoformat()
    runs = [
        diagnose_run(root, store, r)
        for r in store.records("daily")
        if datetime.fromisoformat(r["created_at"]).astimezone(zone).date().isoformat() == date
    ]
    report = aggregate(runs, date, timezone)
    directory = root / "reports" / "daily"
    write_json(directory / (date + ".json"), report)
    descriptor, name = tempfile.mkstemp(prefix=".daily-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(markdown(report))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, directory / (date + ".md"))
    finally:
        Path(name).unlink(missing_ok=True)


def recover_interrupted(root, store, timezone):
    """Called under the kernel lock, before starting a new execution attempt."""
    for start in store.records("daily-start"):
        if store.get("daily", start["run_id"]):
            continue
        path = Path(root) / "manifests" / "daily" / (start["run_id"] + ".json")
        if path.exists():
            persist_daily(root, store, json.loads(path.read_text(encoding="utf-8")), timezone=timezone)
            continue
        summary = {
            "run_id": start["run_id"],
            "batch_id": start["batch_id"],
            "created_at": start["started_at"],
            "status": "interrupted",
            "completed_tasks": 0,
            "refiner": refiner_summary(store, start["run_id"]),
            "model_requests": None,
            "model_usage": None,
            "queue": store.counts(),
            "additions": {k: v - start["inventory"][k] for k, v in inventory(store).items()},
            "errors": [{"stage": "daily", "error_type": "InterruptedRun"}],
            "limitations": ["requests and completed task counts unavailable after abrupt termination"],
        }
        persist_daily(root, store, summary, timezone=timezone)
        store.finish_run(start["run_id"], "interrupted", "InterruptedRun")
