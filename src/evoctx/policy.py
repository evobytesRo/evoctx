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

from .audit import AuditWriter
from .db import Database
from .grants import Grant, GrantStore
from .redact import Redactor


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

    def _out(self, grant: Grant, response):
        """Everything leaving the store passes through here."""
        return self.redactor(grant).redact(response)

    # ── gate ──────────────────────────────────────────────────────────────────

    def check(self, client: str, tool: str) -> Grant:
        grant, _expires, reason = self.grants.active_grant()
        if grant is None:
            raise PolicyDenied(
                f"Denied: {reason}. Ask the user to run: evoctx grant activate everything"
            )
        if not grant.matches_client(client):
            raise PolicyDenied(
                f"Denied: active grant '{grant.name}' covers client "
                f"'{grant.client}', not '{client}'. Ask the user to activate "
                f"a matching grant: evoctx grant activate <name>"
            )
        if not grant.allows_tool(tool):
            raise PolicyDenied(
                f"Denied: grant '{grant.name}' does not allow {tool}(). "
                f"Allowed: {', '.join(grant.tools)}. Ask the user to activate "
                f"a wider grant: evoctx grant activate <name>"
            )
        return grant

    # ── scoped reads ──────────────────────────────────────────────────────────

    def _note_visible(self, grant: Grant, note: dict) -> bool:
        if not grant.allows_project(note.get("project")):
            return False
        return not any(t in grant.exclude_tags for t in note.get("tags", []))

    def list_projects(self, grant: Grant) -> list[str]:
        return self._out(grant, [p for p in self.db.list_projects() if grant.allows_project(p)])

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

    def _fetch_filtered(self, fetch_page, is_visible, limit: int) -> list:
        """Page through fetch_page(limit, offset) until `limit` visible items
        are collected or the store is exhausted — replaces a fixed-multiplier
        over-fetch, which silently under-fills results once exclusion rate is
        high enough (e.g. most notes tagged #personal under a grant that
        excludes it)."""
        collected: list = []
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

    # ── audited tool entry points (what the server calls) ────────────────────
    # Lifecycle per call: grant check → scope → store → redact → audit → out.
    # The audited query field is post-redaction — a search string can itself
    # contain secrets. Denied calls are logged too, without their arguments.

    def _guard(self, client: str, tool: str) -> Grant:
        try:
            return self.check(client, tool)
        except PolicyDenied:
            if self.audit:
                grant, _, _ = self.grants.active_grant()
                self.audit.log(client, tool, "denied",
                               grant_name=grant.name if grant else None)
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

    def t_list_projects(self, client: str) -> list[str]:
        grant = self._guard(client, "list_projects")
        result = self.list_projects(grant)
        self._log(client, "list_projects", "read", grant, None, result, result)
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
                     session_id: str | None) -> str:
        grant = self._guard(client, "write_note")
        self.check_write_project(grant, project)
        (title, content), hits = self._redact_inbound(grant, title, content)
        note_id = self.db.add_note(title, content, project=project, tags=tags,
                                   session_id=session_id, author_client=client)
        # title only — the content is already in the store, don't duplicate it
        self._log(client, "write_note", "write", grant,
                  {"title": title, "project": project}, [note_id], note_id,
                  inbound_hits=hits)
        return note_id

    def t_start_session(self, client: str, project: str | None, intent: str | None) -> dict:
        grant = self._guard(client, "start_session")
        self.check_write_project(grant, project)
        (intent,), hits = self._redact_inbound(grant, intent) if intent else ((intent,), 0)
        session_id = self.db.start_session(project=project, intent=intent, client=client)
        result = self.db.get_session(session_id)
        self._log(client, "start_session", "write", grant,
                  {"project": project, "intent": intent}, [session_id], result,
                  inbound_hits=hits)
        return self._out(grant, result)

    def t_end_session(self, client: str, session_id: str, summary: str) -> dict:
        grant = self._guard(client, "end_session")
        (summary,), hits = self._redact_inbound(grant, summary)
        self.db.end_session(session_id, summary)
        result = self.db.get_session(session_id)
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
            grant, _, _ = self.grants.active_grant()
            self.audit.log(client, "list_grants", "read",
                           grant_name=grant.name if grant else None,
                           result_count=1,
                           response_bytes=len(json.dumps(result, default=str)))
        return result

    # ── introspection ─────────────────────────────────────────────────────────

    def describe(self, client: str) -> dict:
        grant, expires_at, reason = self.grants.active_grant()
        if grant is None:
            return {"active_grant": None, "client": client, "reason": reason,
                    "hint": "evoctx grant activate <name>"}
        return {
            "active_grant": grant.name,
            "client": client,
            "client_glob": grant.client,
            "client_matches": grant.matches_client(client),
            "allowed_tools": grant.tools,
            "scope": {"projects": grant.projects, "exclude_tags": grant.exclude_tags},
            # count only — the patterns themselves describe what's being hidden
            "redactions_active": len(grant.redactions),
            "expires_at": expires_at.isoformat(),
        }
