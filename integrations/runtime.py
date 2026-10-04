"""Background workers and deterministic chat commands."""

from __future__ import annotations

import json
import shlex
import threading

from integrations.channels.registry import ChannelRegistry
from integrations.channels.common import retry_delay
from integrations.service import FinFlowService
from integrations.store import IntegrationStore, ProcessLock
from workflow.flywheel import PipelineBusyError
from workflow.flywheel_config import digest

HELP = """FinFlow 命令：
/status — 任务、候选、通知状态
/report [YYYY-MM-DD] — 指定本地日期的日报
/audit — 待复核候选列表
/candidate ID — 候选正文、图像及复核卡片
/trace sample|candidate|artifact ID — 来源血缘
/job JOB_ID — 后台任务状态
/run [--no-discover] — 提交一轮运行
/retry TASK_ID 或 JOB_ID — 重试失败任务
/review TICKET accepted|rejected|needs_review 原因 — 人工复核
直接发送 PDF 可导入并排队处理。运行/导入需 operator，复核需 reviewer。"""
HELP += "\nDiscord / Slack 使用 ! 前缀（如 !status）；群内需 @ 机器人。"


def reply_target(event):
    target = {key: event[key] for key in ("app_id", "chat_id", "user_id", "chat_type")}
    target["channel"] = event.get("channel", "feishu")
    if event.get("thread_id"):
        target["thread_id"] = event["thread_id"]
    return target


def report_text(report):
    latest = report["latest"]
    text = (
        f"FinFlow 日报 {report['date']} ({report['timezone']})\n"
        f"完成轮次：{report['run_count']}\n处理任务：{report['completed_tasks']}\n模型请求：{report['model_requests']}"
    )
    if latest:
        text += (
            f"\n最近状态：{latest['status']}\n当前候选累计状态：{json.dumps(latest.get('candidates', {}), ensure_ascii=False)}"
            f"\n当前积压：{json.dumps(latest.get('queue', {}), ensure_ascii=False)}"
            f"\n本轮数据集：{json.dumps(latest.get('dataset', {}), ensure_ascii=False)}"
            f"\nSFT 数据集：{json.dumps(latest.get('sft_dataset', {'status': 'disabled'}), ensure_ascii=False)}"
        )
        release = latest.get("training_release", {})
        text += f"\n本日新增：{json.dumps(report.get('additions', {}), ensure_ascii=False)}"
        text += f"\n训练发布：{release.get('status', 'disabled')}；版本：{(release.get('latest') or {}).get('release_id', 'none')}"
        for kind, track in release.get("tracks", {}).items():
            text += (
                f"\n{kind.upper()}：输入 {track['input_samples']}，去重排除 {track['near_dedup']['excluded']}，"
                f"训练 {track['train_samples']}，验证 {track['validation_samples']}，配额缺口 {track['shortfall']} {track['unit']}"
            )
        if latest.get("errors"):
            text += "\n阶段异常：" + json.dumps(latest["errors"], ensure_ascii=False)[:3000]
    else:
        text += "\n该日期暂无已写入摘要的运行。"
    return text


class CommandRouter:
    def __init__(self, service: FinFlowService, client=None, *, registry=None):
        self.service = service
        self.config = service.config
        self.registry = registry or ChannelRegistry(self.config, {"feishu": client} if client else None)

    def handle(self, event):
        channel = event.get("channel", "feishu")  # persisted v1 Feishu events remain compatible
        if event["app_id"] != self.config.identity(channel):
            raise PermissionError("application identity changed")
        actor = self.config.actor(channel, event["user_id"])
        self.config.check_chat(channel, event["chat_id"], event.get("chat_type"))
        target = reply_target(event)
        request_id = event["event_key"]
        if event["kind"] == "file":
            actor.require("operator")
            temporary = self.registry.get(channel).download_event(event)
            try:
                result = self.service.import_pdf(actor, temporary, request_id, origin=event, reply=target, trusted_attachment=True)
            finally:
                temporary.unlink(missing_ok=True)
        elif event["kind"] == "notice":
            return {"kind": "text", "text": event["text"]}
        elif event["kind"] == "review":
            result = self.service.submit_review(
                actor, event["ticket"], event["decision"], "通过飞书复核卡片确认", event["chat_id"], reply=target
            )
        else:
            args = shlex.split(event.get("text", ""))
            if not args:
                return {"kind": "text", "text": HELP}
            command, params = args[0].lower(), args[1:]
            if command.startswith("!"):
                command = "/" + command[1:]
            if command == "/status" and not params:
                result = self.service.status(actor)
            elif command == "/report" and len(params) <= 1:
                return {"kind": "text", "text": report_text(self.service.report(actor, params[0] if params else None))}
            elif command == "/audit" and not params:
                result = self.service.candidates(actor)
            elif command == "/candidate" and len(params) == 1:
                if "reviewer" in actor.roles:
                    candidate, ticket = self.service.review_ticket(actor, params[0], event["chat_id"], chat_type=event["chat_type"])
                else:
                    candidate, ticket = self.service.candidate(actor, params[0]), None
                return {"kind": "candidate", "candidate": candidate, "ticket": ticket}
            elif command == "/trace" and len(params) == 2:
                result = self.service.lineage(actor, params[0], params[1])
            elif command == "/job" and len(params) == 1:
                result = self.service.job(actor, params[0])
            elif command == "/run" and params in ([], ["--no-discover"]):
                result = self.service.start_run(actor, request_id, discover=not params, reply=target)
            elif command == "/retry" and len(params) == 1:
                result = self.service.retry(actor, params[0], request_id, reply=target)
            elif command == "/review" and len(params) >= 3:
                result = self.service.submit_review(actor, params[0], params[1], " ".join(params[2:]), event["chat_id"], reply=target)
            else:
                return {"kind": "text", "text": HELP}
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if len(text) > 14000:
            text = text[:14000] + "\n结果已截断；请使用本机 CLI/MCP 查询完整记录。"
        return {"kind": "text", "text": text}


class Worker:
    def __init__(self, service, *, router=None, client=None, registry=None):
        self.service = service
        self.config = service.config
        self.registry = registry or ChannelRegistry(self.config, {"feishu": client} if client else None)
        self.router = router or CommandRouter(service, registry=self.registry)

    def job_once(self):
        try:
            with ProcessLock(self.config.root, "app-worker"), IntegrationStore(self.config.root) as store:
                store.recover_jobs()
                job = store.claim_job()
                if not job:
                    return False
                try:
                    result = self.service.execute(job)
                    state = result.get("status", "success")
                    status = "partial" if state == "partial" else "failed" if state == "failed" else "succeeded"
                    with store.db:
                        store.finish_job(job["job_id"], status, result)
                except PipelineBusyError:
                    # A scheduled flywheel run owns its lock; try this job later.
                    with store.db:
                        store.db.execute("UPDATE app_job SET status='pending' WHERE job_id=?", (job["job_id"],))
                except Exception as exc:
                    with store.db:
                        store.finish_job(job["job_id"], "failed", error_type=type(exc).__name__)
                return True
        except BlockingIOError:
            return False

    def ingress_once(self):
        try:
            with ProcessLock(self.config.root, "app-ingress"), IntegrationStore(self.config.root) as store:
                row = store.next_item("app_event")
                if not row:
                    return False
                event = json.loads(row["payload_json"])
                target = reply_target(event)
                try:
                    payload = self.router.handle(event)
                except PermissionError as exc:
                    # Never send data after access has been revoked.
                    with store.db:
                        store.db.execute(
                            "UPDATE app_event SET status='rejected',error_type=? WHERE event_key=?", (type(exc).__name__, row["event_key"])
                        )
                    return True
                except (ValueError, FileNotFoundError) as exc:
                    payload = {
                        "kind": "text",
                        "text": f"FinFlow 无法执行：{type(exc).__name__}。检查命令参数、候选版本或来源准入；/help 查看用法。",
                    }
                except Exception as exc:
                    store.fail_item("app_event", row["event_key"], type(exc).__name__, retry_after=retry_delay(exc))
                    return True
                with store.db:
                    store.queue_delivery("event:" + row["event_key"], target, payload)
                    store.db.execute("UPDATE app_event SET status='handled' WHERE event_key=?", (row["event_key"],))
                return True
        except BlockingIOError:
            return False

    def queue_reports(self):
        channels = {
            name: settings
            for name, settings in self.config.channels.items()
            if settings.get("enabled") and settings.get("notification_chats")
        }
        if not channels:
            return
        report = self.service.report(self.config.actor("local"))
        if not report["latest"]:
            return
        for channel, settings in channels.items():
            for chat_id in settings["notification_chats"]:
                identity = self.config.identity(channel)
                target = {"channel": channel, "app_id": identity, "chat_id": chat_id, "scheduled": True}
                # Preserve the original Feishu delivery identity during upgrade.
                key_parts = [identity, chat_id, "report", report["date"], report["latest"]["run_id"]]
                key = digest(key_parts if channel == "feishu" else [channel, *key_parts])
                with IntegrationStore(self.config.root) as store, store.db:
                    store.queue_delivery(key, target, {"kind": "text", "text": report_text(report)})

    def delivery_once(self):
        try:
            with ProcessLock(self.config.root, "app-outbox"), IntegrationStore(self.config.root) as store:
                row = store.next_item("app_outbox")
                if not row:
                    return False
                target = json.loads(row["target_json"])
                try:
                    channel = target.get("channel")
                    settings = self.config.channels.get(channel, {})
                    if not settings.get("enabled") or target.get("app_id") != self.config.identity(channel):
                        raise PermissionError("delivery belongs to another application")
                    if target.get("scheduled"):
                        if target["chat_id"] not in settings.get("notification_chats", []):
                            raise PermissionError("notification target removed")
                    else:
                        self.config.actor(channel, target["user_id"])
                        self.config.check_chat(channel, target["chat_id"], target.get("chat_type"))

                    def checkpoint(receipts):
                        with store.db:
                            store.db.execute(
                                "UPDATE app_outbox SET receipt_json=? WHERE delivery_id=?", (json.dumps(receipts), row["delivery_id"])
                            )

                    self.registry.get(channel).deliver(
                        target, json.loads(row["payload_json"]), row["delivery_id"], json.loads(row["receipt_json"] or "{}"), checkpoint
                    )
                    with store.db:
                        store.db.execute("UPDATE app_outbox SET status='sent' WHERE delivery_id=?", (row["delivery_id"],))
                except PermissionError as exc:
                    with store.db:
                        store.db.execute(
                            "UPDATE app_outbox SET status='rejected',error_type=? WHERE delivery_id=?",
                            (type(exc).__name__, row["delivery_id"]),
                        )
                except Exception as exc:
                    store.fail_item("app_outbox", row["delivery_id"], type(exc).__name__, retry_after=retry_delay(exc))
                return True
        except BlockingIOError:
            return False

    def serve(self, stop: threading.Event):
        # Jobs can take hours. Ingress/status and delivery keep their own loops.
        def loop(function):
            while not stop.is_set():
                try:
                    function()
                except Exception as exc:
                    import logging

                    logging.getLogger(__name__).error("integration worker stage failed: %s", type(exc).__name__)
                stop.wait(self.config.poll_seconds)

        def outbound():
            self.queue_reports()
            self.delivery_once()

        threads = [threading.Thread(target=loop, args=(fn,), daemon=True) for fn in (self.job_once, self.ingress_once, outbound)]
        for thread in threads:
            thread.start()
        return threads
