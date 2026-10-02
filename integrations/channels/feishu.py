"""Feishu event normalization, bounded file transfer and durable delivery.

Uses Feishu's public REST API and official WebSocket SDK. No upstream agent
implementation is copied or required.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import requests

from core.flywheel_files import safe_path, sha256
from integrations.store import IntegrationStore
from storage.state_store import utc_now
from workflow.flywheel_config import digest


class FeishuAPIError(RuntimeError):
    """Provider failures are reported by type, never by token-bearing response bodies."""


class FeishuClient:
    base = "https://open.feishu.cn/open-apis"

    def __init__(self, config, *, session=None):
        self.config = config
        self.session = session or requests.Session()
        self.token = ""
        self.expires_at = 0
        self.lock = threading.Lock()

    def _token(self):
        with self.lock:
            if self.token and time.time() < self.expires_at:
                return self.token
            if not self.config.app_id or not self.config.app_secret:
                raise ValueError("Feishu credentials are missing")
            response = self.session.post(
                self.base + "/auth/v3/tenant_access_token/internal",
                json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
                timeout=(10, 30),
                allow_redirects=False,
            )
            try:
                data = response.json()
                if response.status_code != 200 or data.get("code") != 0 or not data.get("tenant_access_token"):
                    raise FeishuAPIError("Feishu authentication failed")
                self.token = data["tenant_access_token"]
                self.expires_at = time.time() + max(0, int(data.get("expire", 0)) - 60)
                return self.token
            finally:
                response.close()

    def request(self, method, endpoint, **kwargs):
        response = self.session.request(
            method,
            self.base + endpoint,
            headers={"Authorization": "Bearer " + self._token()},
            timeout=(10, 120),
            allow_redirects=False,
            **kwargs,
        )
        if kwargs.get("stream"):
            if response.status_code != 200 or "json" in response.headers.get("Content-Type", "").lower():
                # Resource endpoints can return HTTP 200 with a JSON token error.
                # Refresh authentication on retry instead of treating it as a PDF.
                self.expires_at = 0
                response.close()
                raise FeishuAPIError("Feishu attachment download failed")
            return response
        try:
            data = response.json()
            if response.status_code != 200 or data.get("code") != 0:
                if data.get("code") in {99991663, 99991664, 99991668}:
                    self.expires_at = 0
                raise FeishuAPIError("Feishu API request failed")
            return data.get("data") or data
        finally:
            response.close()

    def bot_id(self):
        return self.request("GET", "/bot/v3/info")["bot"]["open_id"]

    def message(self, chat_id, kind, content, delivery_key):
        return self.request(
            "POST",
            "/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": kind,
                "content": json.dumps(content, ensure_ascii=False),
                "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, delivery_key)),
            },
        )

    def download(self, message_id, file_id):
        directory = self.config.root / "integrations" / "downloads"
        directory.mkdir(parents=True, exist_ok=True)
        response = self.request(
            "GET", f"/im/v1/messages/{quote(message_id, safe='')}/resources/{quote(file_id, safe='')}", params={"type": "file"}, stream=True
        )
        temporary = None
        try:
            length = response.headers.get("Content-Length")
            if length and int(length) > self.config.max_bytes:
                raise ValueError("attachment exceeds configured size limit")
            with tempfile.NamedTemporaryFile(dir=directory, suffix=".pdf", delete=False) as output:
                temporary = Path(output.name)
                total = 0
                for chunk in response.iter_content(1024 * 1024):
                    total += len(chunk)
                    if total > self.config.max_bytes:
                        raise ValueError("attachment exceeds configured size limit")
                    output.write(chunk)
            return temporary
        except BaseException:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise
        finally:
            response.close()

    def upload(self, relative, expected_hash, *, image=False):
        path = safe_path(self.config.root, relative)
        if sha256(path) != expected_hash or path.stat().st_size > self.config.max_bytes:
            raise ValueError("outbound evidence changed or exceeds size limit")
        with path.open("rb") as handle:
            if image:
                data = self.request("POST", "/im/v1/images", data={"image_type": "message"}, files={"image": handle})
                return data["image_key"]
            data = self.request("POST", "/im/v1/files", data={"file_type": "stream", "file_name": path.name}, files={"file": handle})
            return data["file_key"]

    def deliver(self, target, payload, delivery_id, receipts, checkpoint):
        chat_id = target["chat_id"]

        def send(stage, kind, content):
            if stage not in receipts:
                result = self.message(chat_id, kind, content, delivery_id + ":" + stage)
                receipts[stage] = result.get("message_id", "sent")
                checkpoint(receipts)

        if payload["kind"] == "candidate":
            candidate = payload["candidate"]
            data = candidate["data"]
            if data.get("image_path") and "image" not in receipts:
                key = self.upload(data["image_path"], data["image_sha256"], image=True)
                send("image", "image", {"image_key": key})
            if "evidence" not in receipts:
                key = self.upload(data["path"], data["sha256"])
                send("evidence", "file", {"file_key": key})
            elements = [
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": f"候选：{candidate['candidate_id']}\n方法：{candidate['method']}\n状态：{candidate['status']}\n"
                        f"描述/正文：\n{data.get('text', '')[:3500]}\n\nOCR/原文证据：\n{data.get('evidence_text', '')[:3500]}\n\n"
                        "完整正文与证据见上方 JSON 附件；显示内容可能截断。请核对原图、数字、单位和来源后再决定。",
                    },
                }
            ]
            if payload.get("ticket"):
                elements.append(
                    {
                        "tag": "div",
                        "text": {
                            "tag": "plain_text",
                            "content": "需要填写复核原因时，可发送：\n/review "
                            + payload["ticket"]
                            + " accepted|rejected|needs_review 原因",
                        },
                    }
                )
                buttons = []
                for label, decision in (("接受", "accepted"), ("拒绝", "rejected"), ("待复核", "needs_review")):
                    buttons.append(
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": label},
                            "type": "default",
                            "value": {"ticket": payload["ticket"], "decision": decision},
                            "confirm": {
                                "title": {"tag": "plain_text", "content": "确认复核决定"},
                                "text": {"tag": "plain_text", "content": "此操作会记录你的身份与决定，并继续检查来源训练准入。"},
                            },
                        }
                    )
                elements.append({"tag": "action", "actions": buttons})
            send(
                "card",
                "interactive",
                {
                    "config": {"wide_screen_mode": True},
                    "header": {"title": {"tag": "plain_text", "content": "FinFlow 候选复核"}},
                    "elements": elements,
                },
            )
        else:
            text = payload["text"]
            for i, start in enumerate(range(0, max(1, len(text)), 2000)):
                send(str(i), "text", {"text": text[start : start + 2000] or "FinFlow"})
        return receipts

    def download_event(self, event):
        return self.download(event["message_id"], event["file_id"])

    def listen(self, stop):
        listen(self.config, stop, client=self)


def normalize_message(config, event: dict, *, bot_id="") -> dict | None:
    """Accept only authenticated SDK events from configured human senders."""
    header, body = event.get("header", {}), event.get("event", {})
    if header.get("app_id") != config.app_id:
        return None
    sender, message = body.get("sender", {}), body.get("message", {})
    if sender.get("sender_type") != "user":
        return None
    user = sender.get("sender_id", {}).get("open_id", "")
    try:
        config.actor("feishu", user)
    except PermissionError:
        return None
    chat, message_id = message.get("chat_id", ""), message.get("message_id", "")
    if not chat or not message_id:
        return None
    if message.get("chat_type") == "group":
        if chat not in config.feishu.get("allowed_group_chats", []):
            return None
        if not bot_id or not any(m.get("id", {}).get("open_id") == bot_id for m in message.get("mentions", [])):
            return None
    elif message.get("chat_type") != "p2p":
        return None
    content = json.loads(message.get("content") or "{}")
    kind = message.get("message_type")
    if kind not in {"text", "file"}:
        return None
    result = {
        "channel": "feishu",
        "kind": kind,
        "user_id": user,
        "chat_id": chat,
        "chat_type": message["chat_type"],
        "message_id": message_id,
        "app_id": config.app_id,
        "event_key": digest([config.app_id, message_id]),
        "received_at": utc_now(),
    }
    if kind == "text":
        text = content.get("text", "")
        for mention in message.get("mentions", []):
            if mention.get("id", {}).get("open_id") == bot_id and mention.get("key"):
                text = text.replace(mention["key"], "")
        result["text"] = text.strip()[:10000]
    else:
        config.actor("feishu", user).require("operator")
        filename = content.get("file_name", "")
        if not filename.lower().endswith(".pdf") or not content.get("file_key"):
            return None
        result.update({"filename": Path(filename).name[:255], "file_id": content["file_key"]})
    return result


def normalize_card(config, event: dict) -> dict | None:
    header, body = event.get("header", {}), event.get("event", {})
    if header.get("app_id") != config.app_id:
        return None
    user = body.get("operator", {}).get("open_id", "")
    actor = config.actor("feishu", user)
    actor.require("reviewer")
    context, value = body.get("context", {}), body.get("action", {}).get("value", {})
    chat = context.get("open_chat_id", "")
    if not chat or not isinstance(value, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", str(value.get("ticket", ""))):
        return None
    decision = value.get("decision")
    if decision not in {"accepted", "rejected", "needs_review"}:
        return None
    with IntegrationStore(config.root) as store:
        ticket = store.db.execute("SELECT * FROM app_review WHERE ticket_hash=?", (digest(value["ticket"]),)).fetchone()
    if not ticket or ticket["actor_id"] != actor.id or ticket["chat_id"] != chat or ticket["expires_at"] < time.time():
        return None
    if ticket["chat_type"] == "group" and chat not in config.feishu.get("allowed_group_chats", []):
        return None
    return {
        "channel": "feishu",
        "kind": "review",
        "app_id": config.app_id,
        "user_id": user,
        "chat_id": chat,
        "chat_type": ticket["chat_type"],
        "message_id": context.get("open_message_id", ""),
        "ticket": value["ticket"],
        "decision": decision,
        "event_key": digest([config.app_id, user, chat, value["ticket"], decision]),
    }


def listen(config, stop, *, client=None):
    """SDK owns the socket loop. Callback work ends after a durable inbox write."""
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    client = client or FeishuClient(config)
    bot_id = client.bot_id()

    def receive(event):
        try:
            normalized = normalize_message(config, json.loads(lark.JSON.marshal(event)), bot_id=bot_id)
            if normalized:
                with IntegrationStore(config.root) as store:
                    store.queue_event(normalized["event_key"], normalized)
        except (PermissionError, ValueError, KeyError, TypeError, AttributeError):
            return  # malformed or disallowed messages never dispatch commands

    def card(event):
        try:
            normalized = normalize_card(config, json.loads(lark.JSON.marshal(event)))
            if not normalized:
                raise ValueError("unsupported callback")
            with IntegrationStore(config.root) as store:
                store.queue_event(normalized["event_key"], normalized)
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "已收到，处理结果将发到当前会话"}})
        except (PermissionError, ValueError, KeyError, TypeError, AttributeError):
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "无法接受此复核，请重新获取候选"}})

    handler = (
        lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(receive).register_p2_card_action_trigger(card).build()
    )
    socket = lark.ws.Client(config.app_id, config.app_secret, event_handler=handler, log_level=lark.LogLevel.WARNING)
    try:
        socket.start()
    finally:
        stop.set()


Client = FeishuClient
