"""Background workers and deterministic chat commands."""

from __future__ import annotations

import json
import shlex
import threading

from integrations.channels.feishu import FeishuClient
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
        )
        if latest.get("errors"):
            text += "\n阶段异常：" + json.dumps(latest["errors"], ensure_ascii=False)[:3000]
    else:
        text += "\n该日期暂无已写入摘要的运行。"
    return text


class CommandRouter:
    def __init__(self, service: FinFlowService, client=None):
        self.service = service
        self.config = service.config
        self.client = client or FeishuClient(self.config)

    def handle(self, event):
        if event["app_id"] != self.config.app_id:
            raise PermissionError("application identity changed")
        actor = self.config.actor("feishu", event["user_id"])
        if event.get("chat_type") == "group" and event["chat_id"] not in self.config.feishu.get("allowed_group_chats", []):
            raise PermissionError("chat is no longer allowed")
        target = {
            "channel": "feishu",
            "app_id": self.config.app_id,
            "chat_id": event["chat_id"],
            "user_id": event["user_id"],
            "chat_type": event.get("chat_type", ""),
        }
        request_id = event["event_key"]
        if event["kind"] == "file":
            actor.require("operator")
            temporary = self.client.download(event["message_id"], event["file_id"])
            try:
                result = self.service.import_pdf(actor, temporary, request_id, origin=event, reply=target, trusted_attachment=True)
            finally:
                temporary.unlink(missing_ok=True)
        elif event["kind"] == "review":
            result = self.service.submit_review(
                actor, event["ticket"], event["decision"], "通过飞书复核卡片确认", event["chat_id"], reply=target
            )
        else:
            args = shlex.split(event.get("text", ""))
            if not args:
                return {"kind": "text", "text": HELP}
            command, params = args[0].lower(), args[1:]
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
    def __init__(self, service, *, router=None, client=None):
        self.service = service
        self.config = service.config
        self.client = client or FeishuClient(self.config)
        self.router = router or CommandRouter(service, self.client)

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
                target = {
                    "channel": "feishu",
                    "app_id": event["app_id"],
                    "chat_id": event["chat_id"],
                    "user_id": event["user_id"],
                    "chat_type": event.get("chat_type", ""),
                }
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
                    store.fail_item("app_event", row["event_key"], type(exc).__name__)
                    return True
                with store.db:
                    store.queue_delivery("event:" + row["event_key"], target, payload)
                    store.db.execute("UPDATE app_event SET status='handled' WHERE event_key=?", (row["event_key"],))
                return True
        except BlockingIOError:
            return False

    def queue_reports(self):
        if not self.config.feishu.get("enabled") or not self.config.feishu.get("notification_chats"):
            return
        report = self.service.report(self.config.actor("local"))
        if not report["latest"]:
            return
        for chat_id in self.config.feishu["notification_chats"]:
            target = {"channel": "feishu", "app_id": self.config.app_id, "chat_id": chat_id, "scheduled": True}
            key = digest([self.config.app_id, chat_id, "report", report["date"], report["latest"]["run_id"]])
            with IntegrationStore(self.config.root) as store, store.db:
                store.queue_delivery(key, target, {"kind": "text", "text": report_text(report)})

    def delivery_once(self):
        if not self.config.feishu.get("enabled"):
            return False
        try:
            with ProcessLock(self.config.root, "app-outbox"), IntegrationStore(self.config.root) as store:
                row = store.next_item("app_outbox")
                if not row:
                    return False
                target = json.loads(row["target_json"])
                try:
                    if target.get("channel") != "feishu" or target.get("app_id") != self.config.app_id:
                        raise PermissionError("delivery belongs to another application")
                    if target.get("scheduled"):
                        if target["chat_id"] not in self.config.feishu.get("notification_chats", []):
                            raise PermissionError("notification target removed")
                    else:
                        self.config.actor("feishu", target["user_id"])
                        if target.get("chat_type") == "group" and target["chat_id"] not in self.config.feishu.get(
                            "allowed_group_chats", []
                        ):
                            raise PermissionError("group removed")

                    def checkpoint(receipts):
                        with store.db:
                            store.db.execute(
                                "UPDATE app_outbox SET receipt_json=? WHERE delivery_id=?", (json.dumps(receipts), row["delivery_id"])
                            )

                    self.client.deliver(
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
                    store.fail_item("app_outbox", row["delivery_id"], type(exc).__name__)
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
