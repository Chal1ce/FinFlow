"""Telegram Bot API long polling, with a cursor committed after inbox persistence."""

from __future__ import annotations

import json
import re

import requests

from integrations.channels.common import ChannelAPIError, FileChannel, copy_response, log_failure, normalized_event, pdf_event, retry_delay
from integrations.store import IntegrationStore


def normalize(config, update, *, username):
    message = update.get("message")
    if not message or message.get("from", {}).get("is_bot") or message.get("sender_chat"):
        return None
    user = message.get("from", {}).get("id")
    chat = message.get("chat", {})
    if not user or chat.get("type") not in {"private", "group", "supergroup"}:
        return None
    text = message.get("text") or message.get("caption") or ""
    group = chat["type"] != "private"
    mention = r"@" + re.escape(username) + r"\b"
    if group and not re.search(mention, text, re.I):
        return None
    command = text.split(maxsplit=1)[0] if text.strip() else ""
    if command.startswith("/") and "@" in command and command.rsplit("@", 1)[1].lower() != username.lower():
        return None
    event = normalized_event(
        config, "telegram", user, chat["id"], message["message_id"], group=group, thread_id=message.get("message_thread_id")
    )
    if document := message.get("document"):
        if document.get("file_size", 0) > 20 * 1024 * 1024:
            return {**event, "kind": "notice", "text": "Telegram 云端 Bot API 仅支持下载不超过 20 MB 的附件，请使用本机 PDF 导入。"}
        return pdf_event(
            config, event, file_id=document.get("file_id"), filename=document.get("file_name", ""), size=document.get("file_size", 0)
        )
    return {**event, "text": re.sub(mention, "", text, flags=re.I).strip()[:10000]}


class Client(FileChannel):
    channel = "telegram"

    def request(self, method, *, data=None, files=None, timeout=40):
        # Never log URLs: Telegram embeds the bot token in the path.
        response = requests.post(
            f"https://api.telegram.org/bot{self.token}/{method}", data=data, files=files, timeout=(10, timeout), allow_redirects=False
        )
        try:
            result = response.json()
            if response.status_code != 200 or not result.get("ok"):
                raise ChannelAPIError("Telegram API request failed", (result.get("parameters") or {}).get("retry_after", 0))
            return result["result"]
        finally:
            response.close()

    @staticmethod
    def destination(target):
        result = {"chat_id": target["chat_id"]}
        if target.get("thread_id"):
            result["message_thread_id"] = target["thread_id"]
        return result

    def send_text(self, target, text, key):
        return str(
            self.request(
                "sendMessage", data={**self.destination(target), "text": text, "link_preview_options": json.dumps({"is_disabled": True})}
            )["message_id"]
        )

    def send_file(self, target, path, key):
        # Documents preserve original image bytes rather than Telegram photo compression.
        with path.open("rb") as handle:
            return str(
                self.request("sendDocument", data=self.destination(target), files={"document": (path.name, handle)}, timeout=120)[
                    "message_id"
                ]
            )

    def download_event(self, event):
        self.ensure_identity()
        file = self.request("getFile", data={"file_id": event["file_id"]})
        if file.get("file_size", 0) > self.config.max_bytes:
            raise ValueError("attachment exceeds size limit")
        path = file.get("file_path", "")
        if not re.fullmatch(r"[A-Za-z0-9_/.-]+", path) or ".." in path.split("/") or path.startswith("/"):
            raise ValueError("invalid Telegram file path")
        response = requests.get(
            f"https://api.telegram.org/file/bot{self.token}/{path}", stream=True, timeout=(10, 120), allow_redirects=False
        )
        return copy_response(response, self.config.root, self.config.max_bytes)

    def verify_identity(self):
        me = self.request("getMe")
        if str(me["id"]) != self.config.identity(self.channel) or not me.get("is_bot"):
            raise PermissionError("Telegram token does not match configured bot_id")
        self.username = me["username"]

    def listen(self, stop):
        self.ensure_identity()
        if self.request("getWebhookInfo").get("url"):
            raise ValueError("Telegram webhook is active; remove it explicitly before using polling")
        key = "telegram:" + self.config.identity(self.channel)
        while not stop.is_set():
            try:
                with IntegrationStore(self.config.root) as store:
                    row = store.db.execute("SELECT value FROM app_cursor WHERE cursor_key=?", (key,)).fetchone()
                offset = int(row[0]) if row else 0
                updates = self.request("getUpdates", data={"offset": offset, "timeout": 25, "allowed_updates": '["message"]'})
                for update in updates:
                    if stop.is_set():
                        return
                    try:
                        event = normalize(self.config, update, username=self.username)
                    except (PermissionError, ValueError, KeyError, TypeError):
                        event = None
                    with IntegrationStore(self.config.root) as store, store.db:
                        if event:
                            # The event and offset share one commit before acknowledging by polling again.
                            store.db.execute(
                                "INSERT OR IGNORE INTO app_event(event_key,payload_json) VALUES(?,?)",
                                (event["event_key"], json.dumps(event, ensure_ascii=False)),
                            )
                        store.db.execute(
                            "INSERT INTO app_cursor VALUES(?,?) ON CONFLICT(cursor_key) DO UPDATE SET value=excluded.value",
                            (key, str(update["update_id"] + 1)),
                        )
            except Exception as exc:
                log_failure(self.channel, exc)
                stop.wait(max(10, retry_delay(exc)))
