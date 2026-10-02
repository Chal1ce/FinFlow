"""Discord Gateway ingress and REST delivery; no slash-command registration required."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from urllib.parse import quote

import requests

from integrations.channels.common import (
    ChannelAPIError,
    FileChannel,
    addressed_text,
    download_url,
    log_failure,
    normalized_event,
    pdf_event,
    remember,
)


def normalize(config, message):
    if message.get("author", {}).get("bot") or message.get("webhook_id"):
        return None
    group = bool(message.get("guild_id"))
    text = addressed_text(message.get("content", ""), r"<@!?" + re.escape(config.identity("discord")) + r">", required=group)
    if text is None:
        return None
    event = normalized_event(config, "discord", message["author"]["id"], message["channel_id"], message["id"], group=group)
    files = message.get("attachments", [])
    if files:
        if len(files) != 1:
            return {**event, "kind": "notice", "text": "请每条消息只附上一份 PDF。"}
        return pdf_event(config, event, file_id=files[0]["id"], filename=files[0].get("filename", ""), size=files[0].get("size", 0))
    return {**event, "text": text}


class Client(FileChannel):
    channel = "discord"
    base = "https://discord.com/api/v10"

    def request(self, method, endpoint, **kwargs):
        headers = {"Authorization": "Bot " + self.token, "User-Agent": "DiscordBot (https://github.com/Chal1ce/FinFlow, 0.1.0)"}
        response = requests.request(
            method, self.base + endpoint, headers=headers, timeout=(10, 120), allow_redirects=False, **kwargs
        )
        try:
            if response.status_code == 429:
                raise ChannelAPIError("Discord API rate limited", response.json().get("retry_after", 0))
            if response.status_code not in {200, 201}:
                raise ChannelAPIError("Discord API request failed")
            return response.json()
        finally:
            response.close()

    @staticmethod
    def options(key):
        # Discord checks enforced nonces for recently sent messages; receipts remain primary.
        nonce = str(int(hashlib.sha256(key.encode()).hexdigest()[:15], 16))
        return {"allowed_mentions": {"parse": []}, "nonce": nonce, "enforce_nonce": True}

    def send_text(self, target, text, key):
        result = self.request(
            "POST", f"/channels/{quote(target['chat_id'], safe='')}/messages", json={"content": text, **self.options(key)}
        )
        return result["id"]

    def send_file(self, target, path, key):
        with path.open("rb") as handle:
            result = self.request(
                "POST",
                f"/channels/{quote(target['chat_id'], safe='')}/messages",
                data={"payload_json": json.dumps(self.options(key))},
                files={"files[0]": (path.name, handle)},
            )
        return result["id"]

    def download_event(self, event):
        self.ensure_identity()
        message = self.request("GET", f"/channels/{quote(event['chat_id'], safe='')}/messages/{quote(event['message_id'], safe='')}")
        if str(message.get("author", {}).get("id")) != event["user_id"]:
            raise PermissionError("attachment message author changed")
        attachment = next((f for f in message.get("attachments", []) if str(f["id"]) == event["file_id"]), None)
        if not attachment or not attachment["filename"].lower().endswith(".pdf"):
            raise ValueError("PDF attachment no longer exists")
        if attachment.get("size", 0) > self.config.max_bytes:
            raise ValueError("attachment exceeds size limit")
        return download_url(self.config, attachment["url"], {"cdn.discordapp.com", "media.discordapp.net"})

    def verify_identity(self):
        me = self.request("GET", "/users/@me")
        if str(me["id"]) != self.config.identity(self.channel) or not me.get("bot"):
            raise PermissionError("Discord token does not match configured bot_id")

    def listen(self, stop):
        import discord

        self.ensure_identity()
        intents = discord.Intents.default()
        intents.message_content = True
        gateway = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())

        @gateway.event
        async def on_message(message):
            raw = {
                "id": str(message.id),
                "channel_id": str(message.channel.id),
                "content": message.content,
                "author": {"id": str(message.author.id), "bot": message.author.bot},
                "webhook_id": message.webhook_id,
                "guild_id": str(message.guild.id) if message.guild else None,
                "attachments": [{"id": str(a.id), "filename": a.filename, "size": a.size} for a in message.attachments],
            }
            try:
                event = normalize(self.config, raw)
                await asyncio.to_thread(remember, self.config, event)
            except (PermissionError, ValueError, KeyError, TypeError):
                pass
            except Exception as exc:
                # Discord does not provide durable message replay across a cold restart.
                log_failure(self.channel, exc)

        async def run():
            async with gateway:

                async def watch_stop():
                    while not stop.is_set():
                        await asyncio.sleep(1)
                    await gateway.close()

                watcher = asyncio.create_task(watch_stop())
                try:
                    await gateway.start(self.token)
                finally:
                    watcher.cancel()

        asyncio.run(run())
