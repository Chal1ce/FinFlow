"""Live, read-only status plus small atomic progress snapshots for new workers."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from core.flywheel_files import safe_path, write_json
from workflow.flywheel_scope import CollectionScope


STAGES = {
    "discover": "发现来源", "backfill": "登记已有 OCR", "download": "下载 PDF", "ocr": "OCR",
    "govern": "数据治理", "describe": "图表描述", "generate": "生成候选", "review": "审核候选",
    "refine": "文本精炼", "publish": "发布数据", "completed": "本轮结束", "discover_or_publish": "发现来源 / 发布数据",
}
PHASES = {"table_summary": "表格摘要", "document_metadata": "文档元数据", "chunk_context": "文本块上下文"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _age(timestamp):
    if not timestamp:
        return None
    return max(0, round((datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds(), 1))


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _lock_busy(root):
    try:
        with (Path(root) / ".flywheel.lock").open("r") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
    except FileNotFoundError:
        return False


class ProgressWriter:
    def __init__(self, root, context, *, model_limit, console=False):
        self.path = Path(root) / "manifests" / "progress" / f"{context.run_id}.json"
        self.console = console
        self.last_print = 0.0
        self.save_error_reported = False
        self.data = {
            "run_id": context.run_id, "started_at": context.started_at, "stage": "discover", "status": "running",
            "model": {"attempted": 0, "received": 0, "failed": 0, "cache_hits": 0, "limit": model_limit, "by_role": {}},
        }

    def emit(self, *, force=False):
        self.data["updated_at"] = _now()
        try:
            write_json(self.path, self.data)
        except OSError:
            if not self.save_error_reported:
                print("实时进度文件写入失败；处理继续，可通过 status 查看数据库中的进度。", file=sys.stderr, flush=True)
                self.save_error_reported = True
        if self.console and (force or time.monotonic() - self.last_print >= 5):
            phase = self.data.get("phase") or {}
            counter = f" {phase['completed']}/{phase['total']}" if phase else ""
            detail = PHASES.get(phase.get("name"), "")
            model = self.data["model"]
            print(
                f"[{datetime.now().astimezone():%H:%M:%S}] {STAGES.get(self.data['stage'], self.data['stage'])} "
                f"{detail}{counter} | 模型请求 {model['attempted']}/{model['limit']} | 缓存命中 {model['cache_hits']}",
                file=sys.stderr, flush=True,
            )
            self.last_print = time.monotonic()

    def stage(self, name, *, task=None, status="running"):
        self.data.update(stage=name, status=status)
        self.data.pop("phase", None)
        self.data.pop("model_waiting", None)
        self.data["task_uid"] = task["task_uid"] if task else None
        self.emit(force=True)

    def phase(self, name, *, completed, total):
        self.data["phase"] = {"name": name, "completed": completed, "total": total}
        self.emit()

    def model_event(self, event, role):
        model = self.data["model"]
        model[event] += 1
        counts = model["by_role"].setdefault(role, {"attempted": 0, "received": 0, "failed": 0, "cache_hits": 0})
        counts[event] += 1
        self.data["model_waiting"] = role if event == "attempted" else None
        self.emit()


def read_status(config):
    root = Path(config.root)
    result = {
        "status": "success", "queue": {}, "processing_scope": config.processing_scope, "active_queue": {},
        "candidates": {}, "visuals": {}, "split_conflicts": [], "training_latest": None, "training_stages": [],
        "progress": {"state": "idle", "run_id": None, "queue_by_stage": {}},
    }
    database = root / "state" / "pipeline.db"
    if not database.is_file():
        return result
    collection = json.loads(config.collection.read_text(encoding="utf-8"))
    scope = CollectionScope(collection) if config.processing_scope == "collection" else None
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "processing_task" not in tables:
            return result
        tasks = list(connection.execute("SELECT * FROM processing_task"))
        result["queue"] = dict(Counter(row["status"] for row in tasks))
        active = [
            row for row in tasks
            if (row["kind"] != "refine" or json.loads(row["payload_json"]).get("refiner_identity") == config.refiner_identity)
            and (scope is None or scope(json.loads(row["payload_json"])))
        ]
        result["active_queue"] = dict(Counter(row["status"] for row in active))
        stages = defaultdict(Counter)
        for row in active:
            stages[row["kind"]][row["status"]] += 1
        for table, key in (("training_candidate", "candidates"), ("visual_asset", "visuals")):
            if table in tables:
                result[key] = {row[0]: row[1] for row in connection.execute(f"SELECT status,count(*) FROM {table} GROUP BY status")}

        def records(kind):
            return [json.loads(row[0]) for row in connection.execute("SELECT data_json FROM flywheel_record WHERE kind=?", (kind,))]

        if "flywheel_record" not in tables:
            return result
        result.update(split_conflicts=records("split-conflict"), training_stages=records("training-stage"))
        latest = connection.execute("SELECT data_json FROM flywheel_record WHERE kind='training-latest' AND uid='current'").fetchone()
        result["training_latest"] = json.loads(latest[0]) if latest else None
        # daily-start identifies the outer run; governance creates nested pipeline_run rows.
        run = connection.execute(
            "SELECT r.* FROM pipeline_run r JOIN flywheel_record f ON f.uid=r.run_id "
            "WHERE f.kind='daily-start' ORDER BY r.started_at DESC LIMIT 1"
        ).fetchone()
        if not run:
            result["progress"]["queue_by_stage"] = {key: dict(value) for key, value in stages.items()}
            return result
        busy = _lock_busy(root)
        state = "running" if busy and not run["finished_at"] else ("interrupted" if not run["finished_at"] else run["status"])
        progress = {
            "state": state, "run_id": run["run_id"], "started_at": run["started_at"], "finished_at": run["finished_at"],
            "elapsed_seconds": _age(run["started_at"]), "queue_by_stage": {key: dict(value) for key, value in stages.items()},
            "model_limit_from_current_config": config.model_limit,
        }
        if run["finished_at"]:
            progress["elapsed_seconds"] = round(
                (datetime.fromisoformat(run["finished_at"]) - datetime.fromisoformat(run["started_at"])).total_seconds(), 1
            )
        current = next((row for row in active if row["status"] == "running" and row["owner"] == run["run_id"]), None)
        steps = list(connection.execute("SELECT * FROM pipeline_step WHERE run_id=? ORDER BY step_id DESC", (run["run_id"],)))
        progress["completed_tasks_in_run"] = sum(
            row["step_name"].startswith("flywheel-") and row["status"] in {"succeeded", "needs_review", "rejected"} for row in steps
        )
        if current:
            payload = json.loads(current["payload_json"])
            source = payload.get("source") or payload
            progress.update(stage=current["kind"], task_uid=current["task_uid"], document={
                key: source[key] for key in ("title", "source_name", "stock_code", "report_year") if source.get(key) is not None
            })
            step = next((row for row in steps if json.loads(row["metadata_json"] or "{}").get("task_uid") == current["task_uid"]), None)
            progress["task_elapsed_seconds"] = _age(step["started_at"]) if step else None
        else:
            progress["stage"] = "completed" if state != "running" else "discover_or_publish"
        activity = run["finished_at"] or run["started_at"]
        for row in steps:
            activity = max(activity, row["finished_at"] or row["started_at"])
        if "artifact" in tables:
            row = connection.execute("SELECT max(updated_at) FROM artifact WHERE run_id=?", (run["run_id"],)).fetchone()
            if row[0]:
                activity = max(activity, row[0])
        snapshot = _read_json(root / "manifests" / "progress" / f"{run['run_id']}.json")
        if snapshot and snapshot.get("run_id") == run["run_id"]:
            progress.update({key: snapshot[key] for key in ("model", "phase", "model_waiting") if key in snapshot})
            if state == "running":
                progress["stage"] = snapshot.get("stage", progress["stage"])
            activity = max(activity, snapshot.get("updated_at", activity))
            progress["model_count_source"] = "worker_snapshot"
        else:
            saved = 0
            latest_output = None
            if "artifact" in tables:
                row = connection.execute(
                    "SELECT count(*) FROM artifact WHERE run_id=? AND artifact_type='governance-model-response' AND created_at>=?",
                    (run["run_id"], run["started_at"]),
                ).fetchone()
                saved += row[0]
                latest_output = connection.execute(
                    "SELECT path FROM artifact WHERE run_id=? AND artifact_type='governance-model-response' "
                    "AND created_at>=? ORDER BY created_at DESC LIMIT 1", (run["run_id"], run["started_at"])
                ).fetchone()
            saved += connection.execute(
                "SELECT count(*) FROM flywheel_record WHERE kind='model-result' AND updated_at>=?", (run["started_at"],)
            ).fetchone()[0]
            progress.update(saved_model_responses=saved, model_count_source="saved_responses_lower_bound")
            if run["finished_at"]:
                final = connection.execute(
                    "SELECT data_json FROM flywheel_record WHERE kind='daily' AND uid=?", (run["run_id"],)
                ).fetchone()
                if final:
                    progress.update(final_model_requests=json.loads(final[0]).get("model_requests"), model_count_source="final_report")
            if latest_output and progress["stage"] == "govern":
                try:
                    response = _read_json(safe_path(root, latest_output["path"])) or {}
                except ValueError:
                    response = {}
                system = str((response.get("messages") or [{}])[0].get("content", ""))
                phase = "chunk_context" if "context_text" in system else (
                    "document_metadata" if "topics" in system else "table_summary" if "summary" in system else None
                )
                if phase:
                    progress["last_completed_phase"] = phase
        progress.update(last_activity_at=activity, last_activity_seconds_ago=_age(activity))
        result["progress"] = progress
    return result


def render_status(result):
    progress = result["progress"]
    names = {
        "running": "运行中", "idle": "尚未运行", "interrupted": "进程已退出，记录未收尾",
        "success": "已完成", "succeeded": "已完成", "partial": "部分完成", "failed": "失败", "no_change": "无变化",
    }
    lines = [f"状态：{names.get(progress['state'], progress['state'])} | 范围：{result['processing_scope']}"]
    if progress.get("run_id"):
        lines.append(f"运行：{progress['run_id']} | 已用 {progress.get('elapsed_seconds', 0):.0f} 秒")
        phase = progress.get("phase") or {}
        detail = PHASES.get(phase.get("name"), "")
        counter = f" {phase['completed']}/{phase['total']}" if phase else ""
        stage = STAGES.get(progress.get("stage"), progress.get("stage", "unknown"))
        stage_line = f"阶段：{stage}" + (f" / {detail}{counter}" if detail else "")
        if progress.get("task_elapsed_seconds") is not None:
            stage_line += f" | 当前任务已用 {progress['task_elapsed_seconds']:.0f} 秒"
        lines.append(stage_line)
        if not phase and progress.get("last_completed_phase"):
            lines.append(f"最近完成的模型步骤：{PHASES[progress['last_completed_phase']]}")
        doc = progress.get("document") or {}
        if doc:
            lines.append(f"文档：{doc.get('title', doc.get('stock_code', 'unknown'))} | 来源：{doc.get('source_name', 'unknown')}")
        if progress.get("model_count_source") == "worker_snapshot":
            model = progress["model"]
            lines.append(
                f"模型请求：{model['attempted']}/{model['limit']} | 收到响应 {model['received']} | "
                f"请求失败 {model['failed']} | 缓存命中 {model['cache_hits']}"
            )
            if progress.get("model_waiting"):
                lines.append(f"正在等待模型响应：{progress['model_waiting']}")
        elif progress.get("model_count_source") == "final_report":
            lines.append(
                f"本轮模型请求：{progress['final_model_requests']} | 已保存治理/派生响应：{progress['saved_model_responses']}"
            )
        else:
            lines.append(
                f"本轮已保存模型响应：{progress.get('saved_model_responses', 0)} "
                f"（旧进程未记录实时请求数；当前配置上限 {progress['model_limit_from_current_config']}）"
            )
        lines.append(
            f"本轮已结束任务：{progress.get('completed_tasks_in_run', 0)} | "
            f"最近活动：{progress.get('last_activity_seconds_ago', 0):.0f} 秒前"
        )
    columns = ("完成", "运行", "等待", "重试", "暂停", "失败", "待复核")
    lines += ["", "阶段" + " " * 14 + "".join(label.rjust(8 - len(label)) for label in columns)]
    for kind in sorted(progress.get("queue_by_stage", {}), key=lambda name: list(STAGES).index(name) if name in STAGES else len(STAGES)):
        counts = progress["queue_by_stage"][kind]
        waiting = counts.get("pending", 0)
        values = [counts.get(key, 0) for key in ("succeeded", "running")]
        values += [waiting] + [counts.get(key, 0) for key in ("retry_wait", "deferred", "failed", "needs_review")]
        label = STAGES.get(kind, kind)
        width = sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in label)
        lines.append(label + " " * max(1, 18 - width) + "".join(f"{value:>8}" for value in values))
    lines += ["", "任务会动态增加；表中是当前处理范围的队列快照，不是整条流水线的完成百分比。"]
    return "\n".join(lines)
