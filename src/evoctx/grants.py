"""Grant configuration — who may do what with the context store.

Grants are declared in ``grants.yaml`` next to the store (hand-edited, no UI);
which one is active lives separately in ``active_grant.json``, written by
``evoctx grant activate``. Exactly one grant is active at a time. Both files
are re-read whenever their mtime changes, so an activation is picked up by a
running server without restart.

This is policy for cooperative clients, not a security sandbox: client
identity is self-reported and anyone with file access can edit the store
directly.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path

import yaml

ALL_TOOLS = [
    "list_projects", "get_project", "search_context", "get_by_id",
    "write_note", "start_session", "end_session", "recent_sessions",
    "list_grants",
]

DEFAULT_GRANTS_YAML = """\
# evoctx grants — what a connected AI client may do with your store.
# Activate one with:  evoctx grant activate <name>
# Fields:
#   client    glob matched against the client's self-reported name
#   tools     which MCP tools the client may call
#   scope     projects allowlist (glob ok) + tags whose notes are invisible
#   redactions  regex rewrites applied to every response before it leaves
#   expires   mandatory: ISO timestamp, or +Nd/+Nh/+Nm relative to activation
grants:
  - name: everything
    client: "*"
    tools: [list_projects, get_project, search_context, get_by_id,
            write_note, start_session, end_session, recent_sessions,
            list_grants]
    scope:
      projects: ["*"]
      exclude_tags: []
    redactions: []
    expires: "+30d"

  # Example of a scoped, read-mostly grant:
  # - name: payments-only
  #   client: "cursor*"
  #   tools: [list_projects, get_project, search_context, get_by_id, list_grants]
  #   scope:
  #     projects: ["payments"]
  #     exclude_tags: ["#personal", "#finance"]
  #   redactions:
  #     - pattern: "api_key=[A-Za-z0-9]+"
  #       replace: "[REDACTED_KEY]"
  #   expires: "+8h"
"""

_RELATIVE_EXPIRES = re.compile(r"^\+(\d+)([dhm])$")


class GrantError(ValueError):
    """grants.yaml is invalid."""


@dataclass
class Grant:
    name: str
    client: str = "*"
    tools: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=lambda: ["*"])
    exclude_tags: list[str] = field(default_factory=list)
    redactions: list[dict] = field(default_factory=list)
    expires: str = ""

    def matches_client(self, client_name: str) -> bool:
        return fnmatch(client_name, self.client)

    def allows_tool(self, tool: str) -> bool:
        return tool in self.tools

    def allows_project(self, project: str | None) -> bool:
        if project is None:
            return True
        return any(fnmatch(project, pat) for pat in self.projects)

    def expires_at(self, activated_at: datetime) -> datetime:
        m = _RELATIVE_EXPIRES.match(self.expires)
        if m:
            amount, unit = int(m.group(1)), m.group(2)
            delta = {"d": timedelta(days=amount),
                     "h": timedelta(hours=amount),
                     "m": timedelta(minutes=amount)}[unit]
            return activated_at + delta
        return datetime.fromisoformat(self.expires.replace("Z", "+00:00"))


def _parse_grant(raw: dict) -> Grant:
    if not raw.get("name"):
        raise GrantError("grant missing 'name'")
    if not raw.get("expires"):
        raise GrantError(f"grant '{raw['name']}': 'expires' is mandatory "
                         "(ISO timestamp or +Nd/+Nh/+Nm)")
    expires = str(raw["expires"])
    if not _RELATIVE_EXPIRES.match(expires):
        try:
            datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            raise GrantError(f"grant '{raw['name']}': bad expires '{expires}'")
    tools = raw.get("tools") or []
    unknown = [t for t in tools if t not in ALL_TOOLS]
    if unknown:
        raise GrantError(f"grant '{raw['name']}': unknown tools {unknown}")
    scope = raw.get("scope") or {}
    return Grant(
        name=raw["name"],
        client=raw.get("client", "*"),
        tools=tools,
        projects=scope.get("projects") or ["*"],
        exclude_tags=scope.get("exclude_tags") or [],
        redactions=raw.get("redactions") or [],
        expires=expires,
    )


class GrantStore:
    """Loads grants.yaml + active_grant.json, hot-reloading on mtime change."""

    def __init__(self, store_dir: Path):
        self.grants_path = store_dir / "grants.yaml"
        self.active_path = store_dir / "active_grant.json"
        self._grants: dict[str, Grant] = {}
        self._grants_mtime: float = -1.0
        self._active: dict | None = None
        self._active_mtime: float = -1.0
        self.ensure_default_files()

    def ensure_default_files(self) -> None:
        if not self.grants_path.exists():
            self.grants_path.write_text(DEFAULT_GRANTS_YAML, encoding="utf-8")
        if not self.active_path.exists():
            self.activate("everything")

    def activate(self, name: str) -> Grant:
        grant = self.grants().get(name)
        if grant is None:
            raise GrantError(
                f"no grant named '{name}' in {self.grants_path} "
                f"(have: {', '.join(self.grants()) or 'none'})"
            )
        self.active_path.write_text(json.dumps({
            "name": name,
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")
        return grant

    def deactivate(self) -> None:
        self.active_path.write_text(json.dumps({
            "name": None,
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")

    def grants(self) -> dict[str, Grant]:
        mtime = self.grants_path.stat().st_mtime if self.grants_path.exists() else -1.0
        if mtime != self._grants_mtime:
            raw = yaml.safe_load(self.grants_path.read_text(encoding="utf-8")) or {}
            self._grants = {}
            for entry in raw.get("grants") or []:
                grant = _parse_grant(entry)
                self._grants[grant.name] = grant
            self._grants_mtime = mtime
        return self._grants

    def _active_state(self) -> dict:
        mtime = self.active_path.stat().st_mtime if self.active_path.exists() else -1.0
        if mtime != self._active_mtime:
            self._active = json.loads(self.active_path.read_text(encoding="utf-8"))
            self._active_mtime = mtime
        return self._active or {}

    def active_grant(self) -> tuple[Grant | None, datetime | None, str]:
        """Returns (grant, expires_at, reason-if-none)."""
        state = self._active_state()
        name = state.get("name")
        if not name:
            return None, None, "no grant is active"
        grant = self.grants().get(name)
        if grant is None:
            return None, None, f"active grant '{name}' no longer exists in grants.yaml"
        activated_at = datetime.fromisoformat(state["activated_at"])
        expires_at = grant.expires_at(activated_at)
        if datetime.now(timezone.utc) >= expires_at:
            return None, None, (
                f"grant '{name}' expired {expires_at.isoformat()}"
            )
        return grant, expires_at, ""
