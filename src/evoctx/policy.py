"""Policy engine — the single choke point every MCP tool call passes through.

Order per request (matches the design doc's lifecycle):
grant check → scope enforcement → store query → (redaction: phase 5) →
(audit: phase 6) → response.

Scope rules:
- search results, project listings, and sessions are filtered to the grant's
  project globs; notes carrying an excluded tag are filtered out.
- a scope-blocked get_by_id returns None, indistinguishable from a missing
  id — a distinguishable "denied" would leak that the note exists.
- writes to an out-of-scope project fail loudly instead: silently dropping
  a write would be data loss.
"""
import json
from typing import Callable, TypeVar

from .audit import AuditWriter
from .db import Database
from .grants import Grant, GrantStore
from .redact import Redactor
from .resolve import NONE, resolve_workspace

T = TypeVar("T")


class PolicyDenied(Exception):
    """Tool call not covered by the active grant. Message is user-actionable."""


class PolicyEngine:
    def __init__(self, db: Database, grant_store: GrantStore, audit: AuditWriter | None = None):
        self.db = db
        self.grants = grant_store
        self.audit = audit
        self._redactor: Redactor | None = None
        self._redactor_grant: str | None = None

    def redactor(self, grant: Grant) -> Redactor:
        """One Redactor per active grant, kept for the process lifetime so
        tokenization stays stable; rebuilt if the active grant changes."""
        if self._redactor is None or self._redactor_grant != grant.name:
            self._redactor = Redactor(grant.redactions)
            self._redactor_grant = grant.name
        return self._redactor

    def _out(self, grant: Grant, response: T) -> T:
        """Everything leaving the store passes through here. Redaction is
        structure-preserving, so the response type is unchanged."""
        return self.redactor(grant).redact(response)

    # ── gate ──────────────────────────────────────────────────────────────────

    def check(self, client: str, tool: str) -> Grant:
        active, reason = self.grants.active_grant()
        if active is None:
            raise PolicyDenied(
                f"Denied: {reason}. Ask the user to run: evoctx grant activate everything"
            )
        grant = active.grant
        if not grant.matches_client(client):
            raise PolicyDenied(
                f"Denied: active grant '{grant.name}' covers client "
                f"'{grant.client}', not '{client}'. Ask the user to activate "
                f"a matching grant: evoctx grant activate <name>"
            )
        if not grant.allows_tool(tool):
            raise PolicyDenied(
                f"Denied: grant '{grant.name}' does not allow {tool}(). "
                f"Allowed: {', '.join(grant.effective_tools())}. Ask the user to "
                f"activate a wider grant: evoctx grant activate <name>"
            )
        return grant

    # ── scoped reads ──────────────────────────────────────────────────────────

    def _note_visible(self, grant: Grant, note: dict) -> bool:
        if not grant.allows_project(note.get("project")):
            return False
        return not any(t in grant.exclude_tags for t in note.get("tags", []))

    def list_projects(self, grant: Grant) -> list[str]:
        return self._out(grant, [p for p in self.db.list_projects() if grant.allows_project(p)])

    def workspace(self, grant: Grant, cwd=None) -> dict:
        """The resolved workspace, filtered to what this grant may know about.

        Resolution runs against the whole store — a directory belongs to the
        project it belongs to, and narrowing the *input* would make a scoped
        grant resolve to some wider ancestor's project, which is worse than
        resolving to nothing. The narrowing happens here, on the way out.

        An out-of-scope resolution is reported as unresolved rather than
        refused by name, following `get_project`'s rule: out of scope reads the
        same as nonexistent, so a narrow grant cannot enumerate the store by
        walking directories. The hint still says what happened, because a
        silently empty answer would send the caller looking for a bug.

        `cwd` defaults to this process, which is what the server means. It is
        a parameter so the scoping rules below can be tested against a made-up
        directory instead of by chdir'ing the test runner.
        """
        block = resolve_workspace(self.db, cwd=cwd)
        block["nearby"] = [
            n for n in block["nearby"] if grant.allows_project(n["project"])
        ]
        project = block.get("project")
        if project is not None and not grant.allows_project(project):
            block["project"] = None
            block["confidence"] = NONE
            block.pop("matched_root", None)
            block.pop("note", None)
            block["hint"] = (
                f"This directory resolves to a project outside grant "
                f"'{grant.name}' (scope: {grant.projects}), so it is reported "
                f"as unresolved. Ask the user to activate a wider grant."
            )
        return block

    def get_project(self, grant: Grant, name: str) -> dict | None:
        if not grant.allows_project(name):
            return None  # same as nonexistent — don't leak existence
        proj = self.db.get_project(name)
        if proj:
            proj["recent_notes"] = [
                n for n in proj["recent_notes"]
                if not any(t in grant.exclude_tags for t in n.get("tags", []))
            ]
        return self._out(grant, proj)

    # Hard ceiling on how much of the store a single scoped fetch will page
    # through looking for `limit` visible results. Real stores are small
    # (personal, single-user); this exists to bound worst case, not to be hit.
    _MAX_SCAN = 2000

    def _fetch_filtered(self, fetch_page: Callable[[int, int], list[dict]],
                        is_visible: Callable[[dict], bool], limit: int) -> list[dict]:
        """Page through fetch_page(limit, offset) until `limit` visible items
        are collected or the store is exhausted — replaces a fixed-multiplier
        over-fetch, which silently under-fills results once exclusion rate is
        high enough (e.g. most notes tagged #personal under a grant that
        excludes it)."""
        collected: list[dict] = []
        offset = 0
        page_size = max(limit, 20)
        while len(collected) < limit and offset < self._MAX_SCAN:
            page = fetch_page(page_size, offset)
            if not page:
                break
            collected.extend(item for item in page if is_visible(item))
            offset += page_size
        return collected[:limit]

    def search(self, grant: Grant, query: str, project: str | None, limit: int) -> list[dict]:
        if project is not None and not grant.allows_project(project):
            return []
        result = self._fetch_filtered(
            lambda n, o: self.db.search(query, project=project, limit=n, offset=o),
            lambda note: self._note_visible(grant, note),
            limit,
        )
        return self._out(grant, result)

    def get_note(self, grant: Grant, note_id: str) -> dict | None:
        note = self.db.get_note(note_id)
        if note is None or not self._note_visible(grant, note):
            return None
        return self._out(grant, note)

    def recent_sessions(self, grant: Grant, project: str | None, limit: int) -> list[dict]:
        if project is not None and not grant.allows_project(project):
            return []
        result = self._fetch_filtered(
            lambda n, o: self.db.recent_sessions(project=project, limit=n, offset=o),
            lambda s: grant.allows_project(s.get("project")),
            limit,
        )
        return self._out(grant, result)

    # ── scoped writes ─────────────────────────────────────────────────────────

    def check_write_project(self, grant: Grant, project: str | None) -> None:
        if not grant.allows_project(project):
            raise PolicyDenied(
                f"Denied: grant '{grant.name}' does not cover project "
                f"'{project}' (scope: {grant.projects}). The note was NOT saved."
            )

    def check_write_global(self, grant: Grant, is_global: bool) -> None:
        """A scoped grant may not write a note that spans every project.

        `check_write_project` cannot catch this: a global note carries
        `project=None`, and `allows_project(None)` returns True by design, so
        that project-less notes stay visible to scoped grants. That is the
        right rule for reads — but it means a grant scoped to one project
        could write content that lands in *every* project's scoped read
        (`get_project`, `search_context(project=…)`), which is precisely what
        the scope exists to prevent.

        Writing project-less notes stays allowed; only claiming global scope
        requires holding global scope.

        What this does NOT close, so nobody reads it as more than it is: a
        scoped grant can still write a note with no project and no global
        flag, and `allows_project(None)` means every other grant sees it on an
        unscoped `search_context()` or `get_by_id()`. Content can still cross
        scopes that way — that is the existing grant model, unchanged. This
        refuses only the *claim* of global scope, which is what pulls a note
        into every project's scoped read.
        """
        if is_global and not grant.allows_all_projects():
            raise PolicyDenied(
                f"Denied: grant '{grant.name}' is scoped to {grant.projects}, and a "
                f"global note appears under every project. Write it without "
                f"is_global, or ask the user to activate a wider grant. "
                f"The note was NOT saved."
            )

    # ── audited tool entry points (what the server calls) ────────────────────
    # Lifecycle per call: grant check → scope → store → redact → audit → out.
    # The audited query field is post-redaction — a search string can itself
    # contain secrets. Denied calls are logged too, without their arguments.

    def _guard(self, client: str, tool: str) -> Grant:
        try:
            return self.check(client, tool)
        except PolicyDenied:
            if self.audit:
                active, _ = self.grants.active_grant()
                self.audit.log(client, tool, "denied",
                               grant_name=active.grant.name if active else None)
            raise

    def _log(self, client: str, tool: str, action: str, grant: Grant,
             query: dict | None, record_ids: list[str], result,
             inbound_hits: int = 0) -> None:
        if not self.audit:
            return
        redactor = self.redactor(grant)
        if query is not None:
            query = redactor.redact(query)
        self.audit.log(
            client, tool, action, grant_name=grant.name,
            query=query, record_ids=record_ids,
            result_count=len(result) if isinstance(result, list) else (1 if result else 0),
            response_bytes=len(json.dumps(result, default=str)),
            # for writes this is inbound (content masked before storage);
            # for reads it's outbound (response masked before returning)
            redactions_applied=redactor.hits + inbound_hits,
        )

    def _redact_inbound(self, grant: Grant, *texts: str) -> tuple[list[str], int]:
        """Mask secrets in content BEFORE it's stored, using the active
        grant's own custom patterns. Built-in secret patterns are enforced
        one layer down in db.py, unconditionally — they apply to CLI writes
        too, which never pass through here at all. This method only adds
        grant-specific patterns on top for MCP-tool writes."""
        if not grant.redactions:
            return list(texts), 0
        inbound = Redactor(grant.redactions)
        cleaned = [inbound.redact_text(t) if t else t for t in texts]
        return cleaned, inbound.hits

    def _reread_session(self, session_id: str) -> dict:
        """Re-read a session that was just written on this same connection.

        Both callers declare `-> dict` and the MCP layer hands the result
        straight to the client, so a None here has nowhere sensible to go.
        Short of the row being deleted mid-call it cannot happen — raise
        rather than let a None travel outward as if it were a session.
        """
        session = self.db.get_session(session_id)
        if session is None:
            raise RuntimeError(
                f"session '{session_id}' vanished immediately after being written"
            )
        return session

    def t_list_projects(self, client: str) -> dict:
        """Projects plus the resolved workspace, as one envelope.

        The shape change from `list[str]` is deliberate and is the cheaper of
        the two options: adding a tool would deny it to every existing user,
        whose grant enumerates the tools that existed when it was written,
        whereas a response shape is not something grants gate. The cost is
        that the old shape is baked into installed instruction snippets — see
        the tool docstring for what a caller reading a stale one sees.

        The audit line records the project names and sizes the *envelope*.
        `result_count` collapses to 1 because the result is now one dict, and
        that is fine — the names are in `record_ids`, so the count is still
        recoverable, whereas response bytes measured on the inner list would
        simply be wrong about what this client was handed.
        """
        grant = self._guard(client, "list_projects")
        projects = self.list_projects(grant)
        result = {"projects": projects, "workspace": self.workspace(grant)}
        self._log(client, "list_projects", "read", grant, None, projects, result)
        return result

    def t_get_project(self, client: str, name: str) -> dict | None:
        grant = self._guard(client, "get_project")
        result = self.get_project(grant, name)
        ids = [n["id"] for n in result["recent_notes"]] if result else []
        self._log(client, "get_project", "read", grant, {"name": name}, [name, *ids] if result else [], result)
        return result

    def t_search_context(self, client: str, query: str, project: str | None, limit: int) -> list[dict]:
        grant = self._guard(client, "search_context")
        result = self.search(grant, query, project, limit)
        self._log(client, "search_context", "read", grant,
                  {"query": query, "project": project, "limit": limit},
                  [n["id"] for n in result], result)
        return result

    def t_get_by_id(self, client: str, note_id: str) -> dict | None:
        grant = self._guard(client, "get_by_id")
        result = self.get_note(grant, note_id)
        self._log(client, "get_by_id", "read", grant, {"id": note_id},
                  [note_id] if result else [], result)
        return result

    def t_write_note(self, client: str, title: str, content: str,
                     project: str | None, tags: list[str] | None,
                     session_id: str | None, is_global: bool = False) -> str:
        grant = self._guard(client, "write_note")
        self.check_write_project(grant, project)
        self.check_write_global(grant, is_global)
        (title, content), hits = self._redact_inbound(grant, title, content)
        note_id = self.db.add_note(title, content, project=project, tags=tags,
                                   session_id=session_id, author_client=client,
                                   is_global=is_global)
        # title only — the content is already in the store, don't duplicate it.
        # is_global is recorded because "deliberately cross-project" and "no
        # project chosen" are the distinction this release exists to make, and
        # the audit log is the durable record of who did what.
        self._log(client, "write_note", "write", grant,
                  {"title": title, "project": project, "is_global": is_global},
                  [note_id], note_id, inbound_hits=hits)
        return note_id

    def t_start_session(self, client: str, project: str | None, intent: str | None) -> dict:
        grant = self._guard(client, "start_session")
        self.check_write_project(grant, project)
        (intent,), hits = self._redact_inbound(grant, intent) if intent else ((intent,), 0)
        session_id = self.db.start_session(project=project, intent=intent, client=client)
        result = self._reread_session(session_id)
        self._log(client, "start_session", "write", grant,
                  {"project": project, "intent": intent}, [session_id], result,
                  inbound_hits=hits)
        return self._out(grant, result)

    def t_end_session(self, client: str, session_id: str, summary: str) -> dict:
        grant = self._guard(client, "end_session")
        (summary,), hits = self._redact_inbound(grant, summary)
        self.db.end_session(session_id, summary)
        result = self._reread_session(session_id)
        self._log(client, "end_session", "write", grant, None, [session_id], result,
                  inbound_hits=hits)
        return self._out(grant, result)

    def t_recent_sessions(self, client: str, project: str | None, limit: int) -> list[dict]:
        grant = self._guard(client, "recent_sessions")
        result = self.recent_sessions(grant, project, limit)
        self._log(client, "recent_sessions", "read", grant,
                  {"project": project, "limit": limit},
                  [s["id"] for s in result], result)
        return result

    def t_list_grants(self, client: str) -> dict:
        result = self.describe(client)
        if self.audit:
            active, _ = self.grants.active_grant()
            self.audit.log(client, "list_grants", "read",
                           grant_name=active.grant.name if active else None,
                           result_count=1,
                           response_bytes=len(json.dumps(result, default=str)))
        return result

    # ── introspection ─────────────────────────────────────────────────────────

    def describe(self, client: str) -> dict:
        active, reason = self.grants.active_grant()
        if active is None:
            return {"active_grant": None, "client": client, "reason": reason,
                    "hint": "evoctx grant activate <name>"}
        grant = active.grant
        return {
            "active_grant": grant.name,
            "client": client,
            "client_glob": grant.client,
            "client_matches": grant.matches_client(client),
            "allowed_tools": grant.effective_tools(),
            "scope": {"projects": grant.projects, "exclude_tags": grant.exclude_tags},
            # count only — the patterns themselves describe what's being hidden
            "redactions_active": len(grant.redactions),
            "expires_at": active.expires_at.isoformat(),
        }
