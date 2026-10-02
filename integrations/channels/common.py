"""Small transport contract and shared, bounded attachment delivery."""

from __future__ import annotations

import logging
import math
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, urljoin

import requests

from core.flywheel_files import safe_path, sha256
from integrations.store import IntegrationStore
from storage.state_store import utc_now
from workflow.flywheel_config import digest


class ChannelAPIError(RuntimeError):
    """Safe provider failure; never include token-bearing URLs or raw responses."""

    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def retry_delay(exc):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    try:
        delay = float(getattr(exc, "retry_after", 0) or headers.get("Retry-After") or headers.get("retry-after") or 0)
        return min(86400, max(0, delay)) if math.isfinite(delay) else 0
    except (ValueError, TypeError):
        return 0


def remember(config, event):
    if event:
        with IntegrationStore(config.root) as store:
            store.queue_event(event["event_key"], event)


def normalized_event(config, channel, user_id, chat_id, message_id, *, group=False, thread_id=None):
    user_id, chat_id, message_id = str(user_id), str(chat_id), str(message_id)
    config.actor(channel, user_id)
    config.check_chat(channel, chat_id, "group" if group else "p2p")
    event = {
        "channel": channel,
        "kind": "text",
        "app_id": config.identity(channel),
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "chat_type": "group" if group else "p2p",
        "received_at": utc_now(),
        "event_key": digest([channel, config.identity(channel), chat_id, message_id]),
    }
    if thread_id is not None:
        event["thread_id"] = str(thread_id)
    return event


def pdf_event(config, event, *, file_id, filename, size=0):
    if not filename.lower().endswith(".pdf") or not file_id:
        return None
    config.actor(event["channel"], event["user_id"]).require("operator")
    if size and int(size) > config.max_bytes:
        # A normal text event produces a clear response without downloading.
        return {**event, "kind": "notice", "text": "PDF 超过本机附件大小上限。"}
    return {**event, "kind": "file", "file_id": str(file_id), "filename": Path(filename).name[:255]}


def log_failure(channel, exc):
    logging.getLogger(__name__).warning("%s receiver failed: %s", channel, type(exc).__name__)


def copy_response(response, root, limit):
    temporary = None
    try:
        if response.status_code != 200 or "json" in response.headers.get("Content-Type", "").lower():
            raise ChannelAPIError("attachment download failed")
        size = response.headers.get("Content-Length")
        if size and int(size) > limit:
            raise ValueError("attachment exceeds size limit")
        directory = root / "integrations/downloads"
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".pdf", delete=False) as output:
            temporary = Path(output.name)
            total = 0
            for chunk in response.iter_content(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise ValueError("attachment exceeds size limit")
                output.write(chunk)
        return temporary
    except BaseException:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise
    finally:
        response.close()


def download_url(config, url, allowed_hosts, *, headers=None):
    for _ in range(4):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed_hosts
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise ValueError("attachment URL is outside the platform file service")
        response = requests.get(url, headers=headers, stream=True, timeout=(10, 120), allow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("Location"):
            url = urljoin(url, response.headers["Location"])
            response.close()
            continue  # validate the next host before forwarding any authorization header
        return copy_response(response, config.root, config.max_bytes)
    raise ChannelAPIError("attachment has too many redirects")


class FileChannel:
    """Adapters implement send_text/send_file/download_event/listen.

    Full evidence is delivered as files; the final text contains a person-bound
    review command. Receipts are checkpointed after every provider acknowledgement.
    """

    channel = ""
    text_limit = 1800

    def __init__(self, config):
        self.config = config
        self.settings = config.channels[self.channel]
        self.token = config.tokens[self.channel]
        self._verified = False
        self._identity_lock = threading.Lock()

    def ensure_identity(self):
        with self._identity_lock:
            if not self._verified:
                self.verify_identity()
                self._verified = True

    def verify_identity(self):
        raise NotImplementedError

    def evidence_path(self, relative, expected_hash):
        path = safe_path(self.config.root, relative)
        if path.stat().st_size > self.config.max_bytes or sha256(path) != expected_hash:
            raise ValueError("outbound evidence changed or exceeds size limit")
        return path

    def deliver(self, target, payload, delivery_id, receipts, checkpoint):
        self.ensure_identity()

        def save(stage, value):
            receipts[stage] = value
            checkpoint(receipts)

        if payload["kind"] == "candidate":
            candidate, data = payload["candidate"], payload["candidate"]["data"]
            for stage, path_key, hash_key in (("image", "image_path", "image_sha256"), ("evidence", "path", "sha256")):
                if data.get(path_key) and stage not in receipts:
                    path = self.evidence_path(data[path_key], data[hash_key])
                    save(stage, self.send_file(target, path, delivery_id + ":" + stage))
            text = (
                f"FinFlow 候选 {candidate['candidate_id']}\n方法：{candidate['method']}\n状态：{candidate['status']}\n"
                f"描述/正文（节选）：\n{data.get('text', '')[:1500]}\n"
                f"OCR/原文（节选）：\n{data.get('evidence_text', '')[:1500]}\n"
                "完整正文与证据见文件。核对原图、数字、单位和来源后再决定。"
            )
            if payload.get("ticket"):
                prefix = "/" if self.channel == "telegram" else "!"
                text += f"\n复核命令：\n{prefix}review {payload['ticket']} accepted|rejected|needs_review 原因"
                text += "\n群内还需 @ 当前机器人。授权只属于获取此候选的人和当前会话，默认一小时有效。"
        else:
            text = payload["text"]
        for number, offset in enumerate(range(0, max(1, len(text)), self.text_limit)):
            stage = f"text-{number}"
            if stage not in receipts:
                save(stage, self.send_text(target, text[offset : offset + self.text_limit] or "FinFlow", delivery_id + ":" + stage))
        return receipts


def addressed_text(text, mention_pattern, *, required=False):
    match = re.search(mention_pattern, text)
    if required and not match:
        return None
    return re.sub(mention_pattern, "", text).strip()[:10000]
