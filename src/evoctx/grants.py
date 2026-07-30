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
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import NamedTuple

import yaml

ALL_TOOLS = [
    "list_projects", "get_project", "search_context", "get_by_id",
    "write_note", "start_session", "end_session", "recent_sessions",
    "list_grants",
]

# A grant may list "*" instead of enumerating tools, so it never goes stale as
# tools are added. NOT the shipped default: 0.1.2 raises GrantError on any
# unrecognised name including "*", and that raise happens inside
# grants() -> active_grant() -> PolicyEngine.check(), so a 0.1.3-written
# grants.yaml reaching a 0.1.2 machine — via rollback, or a cloud-synced home
# directory — would fail EVERY tool call there. Defaulting to "*" would create
# exactly the lockout this release exists to remove, pointed backwards.
# Flip the default once 0.1.2 is no longer in circulation.
TOOLS_WILDCARD = "*"

DEFAULT_GRANTS_YAML = """\
# evoctx grants — what a connected AI client may do with your store.
# Activate one with:  evoctx grant activate <name>
# Fields:
#   client    glob matched against the client's self-reported name
#   tools     which MCP tools the client may call. "*" means every tool,
#             including ones added by future releases — switch to it if you
#             don't want to revisit this list when evoctx gains a tool.
#             (Left enumerated by default so this file stays readable by
#             evoctx 0.1.2, which rejects "*". Safe to change once every
#             machine sharing this store is on 0.1.3+.)
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
        return TOOLS_WILDCARD in self.tools or tool in self.tools

    def effective_tools(self) -> list[str]:
        """The tool names this grant actually permits, with "*" expanded.

        Config keeps the wildcard so the grant never goes stale; responses
        expand it so list_grants() can still answer "may I call X?" concretely
        instead of handing back an unhelpful ["*"].
        """
        if TOOLS_WILDCARD in self.tools:
            return list(ALL_TOOLS)
        return list(self.tools)

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
    unknown = [t for t in tools if t != TOOLS_WILDCARD and t not in ALL_TOOLS]
    # A wildcard grant already permits everything, so an unrecognised name
    # alongside it changes nothing — saying "ignoring them" there would be
    # false. Only the enumerated case actually loses a permission.
    if unknown and TOOLS_WILDCARD not in tools:
        # Forward compatibility. A grants.yaml written by a NEWER release names
        # tools this build has never heard of. Raising here would be wildly
        # disproportionate: _parse_grant runs inside grants() ->
        # active_grant() -> PolicyEngine.check(), so one unrecognised name
        # would fail EVERY tool call, not just that one — locking the user out
        # of a store that is merely newer than the code.
        #
        # Drop the unknown names rather than carrying them: this build cannot
        # serve those tools, so advertising them via effective_tools() (which
        # list_grants() returns) would be a lie. grants.yaml on disk is never
        # rewritten, so upgrading the package restores them automatically.
        print(
            f"evoctx: grant '{raw['name']}' names tools unknown to this build "
            f"({', '.join(unknown)}); ignoring them. The store was probably "
            f"written by a newer release — upgrade with: pip install -U evoctx",
            file=sys.stderr,
        )
        tools = [t for t in tools if t not in unknown]
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


class ActiveGrant(NamedTuple):
    """A grant that is usable *right now*, together with its resolved expiry.

    One object rather than two parallel Optionals, so "there is a grant" and
    "there is an expiry" cannot be checked and dereferenced independently.
    They used to be separate slots in a 3-tuple: every path that returned a
    grant also returned an expiry, but nothing said so, and
    `evoctx grant activate` on an already-expired grant dereferenced the
    None — an AttributeError instead of an explanation.

    Note the field is the resolved timestamp, whereas ``Grant.expires_at`` of
    the same name is the function that resolves it against an activation time.
    """
    grant: Grant
    expires_at: datetime


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

    def active_grant(self) -> tuple[ActiveGrant | None, str]:
        """Returns (active, reason-if-none). `active` is None whenever there
        is nothing usable — unset, deleted from grants.yaml, or expired — and
        `reason` says which."""
        state = self._active_state()
        name = state.get("name")
        if not name:
            return None, "no grant is active"
        grant = self.grants().get(name)
        if grant is None:
            return None, f"active grant '{name}' no longer exists in grants.yaml"
        activated_at = datetime.fromisoformat(state["activated_at"])
        expires_at = grant.expires_at(activated_at)
        if datetime.now(timezone.utc) >= expires_at:
            return None, f"grant '{name}' expired {expires_at.isoformat()}"
        return ActiveGrant(grant, expires_at), ""
