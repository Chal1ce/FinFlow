"""Non-secret application settings; credentials remain in the environment."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from workflow.flywheel_config import FlywheelConfig

PROJECT = Path(__file__).resolve().parents[1]
CHANNELS = ("feishu", "telegram", "discord", "slack")
DEPENDENCIES = {"feishu": "lark_oapi", "discord": "discord", "slack": "slack_sdk", "mcp": "mcp"}


@dataclass(frozen=True)
class Actor:
    id: str
    channel: str
    roles: frozenset[str]

    def require(self, role: str) -> None:
        if role not in self.roles:
            raise PermissionError(f"requires {role} role")

    def to_dict(self) -> dict:
        return {"id": self.id, "channel": self.channel, "roles": sorted(self.roles)}

    @classmethod
    def from_dict(cls, value: dict) -> "Actor":
        return cls(value["id"], value["channel"], frozenset(value["roles"]))


class IntegrationConfig:
    def __init__(self, path=None, *, flywheel=None, data_root=None):
        self.path = Path(path or PROJECT / "config" / "integrations.json").resolve()
        self.settings = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(self.settings, dict) or self.settings.get("schema_version") != "app-integrations-v1":
            raise ValueError("unsupported integration configuration version")
        self.flywheel = flywheel or FlywheelConfig(PROJECT / "config" / "flywheel.json", data_root=data_root)
        self.root = self.flywheel.root.resolve()
        self.timezone = ZoneInfo(self.settings.get("timezone", "Asia/Shanghai"))
        self.max_bytes = int(self.settings.get("max_attachment_mb", 32)) * 1024 * 1024
        self.max_jobs = int(self.settings.get("max_pending_jobs", 100))
        self.poll_seconds = float(self.settings.get("poll_seconds", 2))
        self.review_ttl = int(self.settings.get("review_ttl_seconds", 3600))
        if not 1024 <= self.max_bytes <= 512 * 1024 * 1024 or not 1 <= self.max_jobs <= 10000:
            raise ValueError("invalid attachment or queue limit")
        if not math.isfinite(self.poll_seconds) or self.poll_seconds < 0.1 or not 60 <= self.review_ttl <= 86400:
            raise ValueError("invalid polling interval or review expiry")
        self.feishu = self.settings.get("feishu", {})
        self.mcp = self.settings.get("mcp", {})
        if not isinstance(self.feishu, dict) or not isinstance(self.mcp, dict):
            raise ValueError("channel settings must be objects")
        if not isinstance(self.mcp.get("allow_mutations", False), bool):
            raise ValueError("allow_mutations must be boolean")
        roots = self.mcp.get("import_roots", [])
        if not isinstance(roots, list) or not all(isinstance(root, str) and root.strip() for root in roots):
            raise ValueError("import_roots must contain non-empty strings")
        if not all(Path(root).expanduser().is_absolute() for root in roots):
            raise ValueError("MCP import_roots must be absolute paths")
        self.app_id = os.getenv("FINFLOW_FEISHU_APP_ID", "")
        self.app_secret = os.getenv("FINFLOW_FEISHU_APP_SECRET", "")
        self.channels = {name: self.settings.get(name, {}) for name in CHANNELS}
        self.tokens = {name: os.getenv(f"FINFLOW_{name.upper()}_BOT_TOKEN", "") for name in CHANNELS if name != "feishu"}
        self.slack_app_token = os.getenv("FINFLOW_SLACK_APP_TOKEN", "")
        for name, settings in self.channels.items():
            if not isinstance(settings, dict) or not isinstance(settings.get("enabled", False), bool):
                raise ValueError(f"invalid {name} channel configuration")
            users = settings.get("users", {})
            if not isinstance(users, dict) or not all(
                isinstance(uid, str)
                and uid
                and isinstance(roles, list)
                and all(isinstance(role, str) and role in {"viewer", "operator", "reviewer"} for role in roles)
                for uid, roles in users.items()
            ):
                raise ValueError(f"invalid {name} user roles")
            for key in ("allowed_group_chats", "notification_chats"):
                values = settings.get(key, [])
                if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
                    raise ValueError(f"{name}.{key} must contain string IDs")
            for key in ("bot_id", "team_id", "bot_user_id", "app_id"):
                if key in settings and not isinstance(settings[key], str):
                    raise ValueError(f"{name}.{key} must be a string")

    def identity(self, channel: str) -> str:
        if channel == "feishu":
            return self.app_id
        settings = self.channels[channel]
        if channel == "slack":
            team, user = settings.get("team_id"), settings.get("bot_user_id")
            return f"{team}:{user}" if team and user else ""
        return settings.get("bot_id", "")

    def check_chat(self, channel, chat_id, chat_type):
        if chat_type not in {"p2p", "group"}:
            raise PermissionError("unsupported conversation type")
        if chat_type == "group" and chat_id not in self.channels[channel].get("allowed_group_chats", []):
            raise PermissionError("conversation is not allowed")

    def actor(self, channel: str, user_id: str = "") -> Actor:
        if channel == "local":
            return Actor("local:operator", channel, frozenset({"viewer", "operator", "reviewer"}))
        if channel == "mcp":
            roles = {"viewer", "operator"} if self.mcp.get("allow_mutations", False) else {"viewer"}
            return Actor("mcp:local", channel, frozenset(roles))
        if channel not in CHANNELS or not self.channels[channel].get("enabled", False) or not self.identity(channel):
            raise PermissionError("channel is disabled")
        roles = self.channels[channel].get("users", {}).get(user_id, [])
        if not roles:
            raise PermissionError("user is not allowed")
        return Actor(f"{channel}:{self.identity(channel)}:{user_id}", channel, frozenset({"viewer", *roles}))

    def current_actor(self, actor: Actor) -> Actor:
        if actor.channel in CHANNELS:
            prefix = f"{actor.channel}:{self.identity(actor.channel)}:"
            if not actor.id.startswith(prefix):
                raise PermissionError("application identity changed")
            return self.actor(actor.channel, actor.id[len(prefix) :])
        return self.actor(actor.channel)

    def preflight(self, *, channel="mcp") -> dict:
        import importlib.util

        errors = []
        if channel == "feishu":
            if not self.feishu.get("enabled", False):
                errors.append("set feishu.enabled=true in the integration configuration")
            if not self.app_id or not self.app_secret:
                errors.append("set FINFLOW_FEISHU_APP_ID and FINFLOW_FEISHU_APP_SECRET")
            if not self.feishu.get("users"):
                errors.append("configure at least one Feishu open_id and its roles")
        elif channel in CHANNELS:
            settings = self.channels[channel]
            if not settings.get("enabled"):
                errors.append(f"set {channel}.enabled=true")
            if not self.identity(channel):
                errors.append(f"configure {channel} bot identity (Slack requires team_id and bot_user_id)")
            if not self.tokens[channel]:
                errors.append(f"set FINFLOW_{channel.upper()}_BOT_TOKEN")
            if channel == "slack" and not self.slack_app_token:
                errors.append("set FINFLOW_SLACK_APP_TOKEN")
            if channel == "slack" and not settings.get("app_id"):
                errors.append("configure slack.app_id to verify incoming event application identity")
            if not settings.get("users"):
                errors.append(f"configure {channel} user IDs and roles")
        elif channel != "mcp":
            raise ValueError("unsupported channel")
        module = DEPENDENCIES.get(channel)
        if module and importlib.util.find_spec(module) is None:
            errors.append(f"install the {channel} optional dependencies")
        return {"status": "failed" if errors else "success", "channel": channel, "errors": errors, "pipeline": self.flywheel.preflight()}
