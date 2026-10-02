"""Slack Socket Mode events, authenticated downloads and external file uploads."""

from __future__ import annotations

import re
import uuid

from integrations.channels.common import FileChannel, addressed_text, download_url, normalized_event, pdf_event, remember


def normalize(config, payload):
    settings = config.channels["slack"]
    if payload.get("team_id") != settings.get("team_id") or payload.get("api_app_id") != settings.get("app_id"):
        return None
    event = payload.get("event", {})
    # Use message events consistently: a parallel app_mention event may omit the
    # files and would otherwise win deduplication before the full file-share event.
    if event.get("type") != "message" or event.get("bot_id") or event.get("subtype") not in {None, "file_share"}:
        return None
    user, chat = event.get("user"), event.get("channel")
    if not user or not chat or user == settings.get("bot_user_id"):
        return None
    group = event.get("channel_type") != "im" and not chat.startswith("D")
    text = addressed_text(event.get("text", ""), r"<@" + re.escape(settings["bot_user_id"]) + r">", required=group)
    if text is None:
        return None
    result = normalized_event(config, "slack", user, chat, event["ts"], group=group, thread_id=event.get("thread_ts"))
    files = event.get("files", [])
    if files:
        if len(files) != 1:
            return {**result, "kind": "notice", "text": "请每条消息只附上一份 PDF。"}
        return pdf_event(config, result, file_id=files[0]["id"], filename=files[0].get("name", ""), size=files[0].get("size", 0))
    return {**result, "text": text}


class Client(FileChannel):
    channel = "slack"

    def web(self):
        from slack_sdk import WebClient

        return WebClient(token=self.token, timeout=120, retry_handlers=[])

    def send_text(self, target, text, key):
        response = self.web().chat_postMessage(
            channel=target["chat_id"],
            text=text,
            thread_ts=target.get("thread_id"),
            mrkdwn=False,
            parse="none",
            unfurl_links=False,
            unfurl_media=False,
            client_msg_id=str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
        )
        return response["ts"]

    def send_file(self, target, path, key):
        response = self.web().files_upload_v2(
            channel=target["chat_id"], thread_ts=target.get("thread_id"), file=str(path), filename=path.name, title=path.name
        )
        return response["files"][0]["id"]

    def download_event(self, event):
        self.ensure_identity()
        file = self.web().files_info(file=event["file_id"])["file"]
        if file.get("file_access") == "check_file_info" or not file.get("url_private_download"):
            raise ValueError("Slack file is not available for download")
        if file.get("size", 0) > self.config.max_bytes or not file.get("name", "").lower().endswith(".pdf"):
            raise ValueError("attachment is not an allowed PDF")
        return download_url(
            self.config, file["url_private_download"], {"files.slack.com"}, headers={"Authorization": "Bearer " + self.token}
        )

    def verify_identity(self):
        identity = self.web().auth_test()
        if (
            identity["team_id"] != self.settings["team_id"]
            or identity["user_id"] != self.settings["bot_user_id"]
            or not identity.get("bot_id")
        ):
            raise PermissionError("Slack token does not match configured team_id/bot_user_id")

    def listen(self, stop):
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse

        self.ensure_identity()
        web = self.web()
        socket = SocketModeClient(app_token=self.config.slack_app_token, web_client=web)

        def receive(client, request):
            if request.type == "events_api":
                try:
                    event = normalize(self.config, request.payload)
                except (PermissionError, ValueError, KeyError, TypeError):
                    event = None
                # Do not acknowledge a valid message until the inbox write commits.
                remember(self.config, event)
            client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))

        socket.socket_mode_request_listeners.append(receive)
        try:
            socket.connect()
            while not stop.wait(1):
                pass
        finally:
            socket.close()
