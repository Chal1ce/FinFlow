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
        if not isinstance(self.feishu.get("enabled", False), bool) or not isinstance(self.mcp.get("allow_mutations", False), bool):
            raise ValueError("enabled and allow_mutations must be booleans")
        users = self.feishu.get("users", {})
        if not isinstance(users, dict):
            raise ValueError("Feishu users must map open_id to roles")
        for user_id, roles in users.items():
            if not user_id or not isinstance(roles, list) or not all(role in {"viewer", "operator", "reviewer"} for role in roles):
                raise ValueError("invalid Feishu user roles")
        for value in (
            self.feishu.get("allowed_group_chats", []),
            self.feishu.get("notification_chats", []),
            self.mcp.get("import_roots", []),
        ):
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                raise ValueError("chat lists and import_roots must contain non-empty strings")
        if not all(Path(root).expanduser().is_absolute() for root in self.mcp.get("import_roots", [])):
            raise ValueError("MCP import_roots must be absolute paths")
        self.app_id = os.getenv("FINFLOW_FEISHU_APP_ID", "")
        self.app_secret = os.getenv("FINFLOW_FEISHU_APP_SECRET", "")

    def actor(self, channel: str, user_id: str = "") -> Actor:
        if channel == "local":
            return Actor("local:operator", channel, frozenset({"viewer", "operator", "reviewer"}))
        if channel == "mcp":
            roles = {"viewer", "operator"} if self.mcp.get("allow_mutations", False) else {"viewer"}
            return Actor("mcp:local", channel, frozenset(roles))
        if channel != "feishu" or not self.feishu.get("enabled", False) or not self.app_id:
            raise PermissionError("channel is disabled")
        roles = self.feishu.get("users", {}).get(user_id, [])
        if not roles:
            raise PermissionError("user is not allowed")
        return Actor(f"feishu:{self.app_id}:{user_id}", channel, frozenset({"viewer", *roles}))

    def current_actor(self, actor: Actor) -> Actor:
        if actor.channel == "feishu":
            prefix = f"feishu:{self.app_id}:"
            if not actor.id.startswith(prefix):
                raise PermissionError("application identity changed")
            return self.actor("feishu", actor.id[len(prefix) :])
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
            if importlib.util.find_spec("lark_oapi") is None:
                errors.append("install the feishu optional dependencies")
        elif importlib.util.find_spec("mcp") is None:
            errors.append("install the mcp optional dependencies")
        return {"status": "failed" if errors else "success", "channel": channel, "errors": errors, "pipeline": self.flywheel.preflight()}
