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
from .db import Database, RootNotADirectory, RootOwnedByAnotherProject
from .grants import ALL_TOOLS, Grant, GrantStore, predates_current_tools
from .redact import Redactor
from .resolve import NONE, current_directory, resolve_workspace

T = TypeVar("T")


def _tail(text: str) -> str:
    """A hint, trimmed, for use at the END of a message and nowhere else.

    It routinely ends in the command the user is meant to copy — `evoctx init
    --name myrepo` — so anything spliced after it becomes part of the argument
    by eye. Terminating it was the first attempt and made `myrepo.` look like
    the name. Putting it last is the fix that has no such edge."""
    return (text or "").strip()


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
            # Two different problems, two different fixes. A grant can lack a
            # tool because a release added it after the grant was written, or
            # because the user left it out on purpose — and only the first is
            # something `grant upgrade` will act on. `predates_current_tools`
            # is what distinguishes them; the inference "the grant does not
            # name it, so the grant predates it" is exactly the one this
            # release refuses to make, because it turns a deliberate
            # restriction into an out-of-date file. Telling the first user to
            # "activate a wider grant" sends them after a grant that does not
            # exist; telling the second to run `grant upgrade` sends them to a
            # command that will not touch their grant.
            outdated = tool in ALL_TOOLS and predates_current_tools(grant.tools)
            fix = ("run: evoctx grant upgrade — this grant predates "
                   f"{tool}() and lists only the tools that existed when it "
                   "was written"
                   if outdated else
                   "activate a wider grant: evoctx grant activate <name>")
            raise PolicyDenied(
                f"Denied: grant '{grant.name}' does not allow {tool}(). "
                f"Allowed: {', '.join(grant.effective_tools())}. Ask the user to "
                f"{fix}"
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
        # An unregistered directory is fixable from inside the conversation
        # now, so the hint has to say so — it is the channel the model actually
        # relays, and a hint naming only the CLI sends the user out of the
        # session to do what the assistant could have done.
        #
        # Only at rung 3, which `suggested_name` identifies: a refused
        # `CONTEXT_PROJECT` is a config typo, and registering there would bind
        # this directory to a name that came from a misconfigured env var,
        # while "no readable cwd" is a workspace `register_project` genuinely
        # cannot bind. Both keep the CLI text.
        #
        # Only when the grant allows the tool. Offering a call the caller will
        # be denied is worse than offering the CLI, which always works.
        if (block["project"] is None and block.get("suggested_name")
                and grant.allows_tool("register_project")):
            name = block["suggested_name"]
            block["hint"] = (
                f"This directory is not registered to any project, and no name "
                f"was guessed. Ask the user whether to register it; if they "
                f"agree, read the workspace and call register_project("
                f"name='{name}', overview=...). They can also do it themselves "
                f"by running in that directory: evoctx init --name {name}"
            )
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

    def check_write_project(self, grant: Grant, project: str | None,
                            thing: str = "note") -> None:
        if not grant.allows_project(project):
            raise PolicyDenied(
                f"Denied: grant '{grant.name}' does not cover project "
                f"'{project}' (scope: {grant.projects}). The {thing} was NOT saved."
            )

    def resolve_write_project(self, grant: Grant, project: str | None,
                              is_global: bool, tool: str) -> str | None:
        """The project a write lands under — refusing rather than guessing.

        Cases, in order:

        - **Global AND a named project is refused.** They are mutually
          exclusive: "this spans every project" and "this belongs to that one"
          are contradictory claims. Returning None for a global write without
          checking would *silently drop* the name the caller gave and publish
          the note into every project's scoped read — a widening nobody asked
          for. `db.add_note` raises `ConflictingNoteScope` on the same
          combination, but it never sees it once the project has been nulled
          here, so this check cannot be delegated downward.
        - **A name that does not exist is refused.** Until resolution existed
          this would have been a trap: the caller had no way to learn the right
          name, so refusing would have converted stranded notes into failed
          writes. Now `list_projects()` answers it, so an invented name is a
          mistake with a fix, and the refusal names the workspace project and
          the names this grant can see.
        - **A null project takes the resolved workspace**, when one resolved.
          Otherwise `recent_sessions(project=…)` keeps missing sessions that
          plainly belong to a project, and notes stay unattributable.
        - **A null project with nothing resolved is refused.** Leaving it NULL
          is what conflates "the user asked for a general note" with "nobody
          set a project", which is the defect this release exists to remove.

        `tool` shapes the advice, because the two callers do not have the same
        parameters: `start_session` has no `is_global`, and telling a session
        to "mark the note global" is both incoherent and unactionable — the
        assistant obeying it gets a schema error.

        Returns the project to write under. Raises `PolicyDenied` rather than
        returning None-and-hoping, because a refused write must be loud —
        silently filing a note nowhere is the data loss this guards.
        """
        # Blank is unset, not a name. `db.add_note` tests truthiness while this
        # tested `is not None`, so a client serialising an unset optional as ""
        # got a denial quoting `project=''` — which reads as a bug report about
        # itself — where before phase 4 it wrote a global note. Normalising here
        # keeps the two guards agreeing and stops '   ' being echoed as a name.
        if project is not None and not project.strip():
            project = None
        global_is_an_option = tool == "write_note"
        thing = "note" if global_is_an_option else "session"
        or_global = (" or set is_global if it deliberately spans every project"
                     if global_is_an_option else "")

        if is_global:
            if project is not None:
                raise PolicyDenied(
                    f"Denied: is_global and project='{project}' are mutually "
                    f"exclusive — global means the note belongs to no single "
                    f"project and appears under every one. Nothing was saved. "
                    f"Send the project, or send is_global, not both."
                )
            return None

        # Scoped to what this grant may see. `db.list_projects()` would
        # enumerate the whole store to a caller narrowed away from most of it,
        # which is the existence leak `workspace()` and `get_project` are both
        # careful to avoid — and it would be a NEW leak, since before this the
        # out-of-scope case only ever echoed the name the caller already had.
        if project is not None:
            if not self.db.project_exists(project):
                known = ", ".join(self.list_projects(grant)) or "none yet"
                block = self.workspace(grant)
                here = block.get("project")
                # "register this workspace" only when it is not registered —
                # otherwise it is dead advice contradicted by the sentence
                # right after it, which names the project this workspace IS.
                register = "" if here else ", register this workspace"
                where = _tail(f"This workspace is '{here}'." if here
                              else block.get("hint", ""))
                raise PolicyDenied(
                    f"Denied: no project named '{project}'. Known projects: "
                    f"{known}. No {thing} was created — use an existing name"
                    f"{register}{or_global}."
                    + (f" {where}" if where else "")
                )
            return project

        block = self.workspace(grant)
        resolved = block.get("project")
        if resolved:
            return resolved
        # The hint carries WHY nothing resolved, and the two reasons need
        # different fixes: an unregistered directory wants registering, while a
        # directory whose project is outside this grant wants a wider grant.
        # `workspace()` has already written the right one — and deliberately
        # without naming an out-of-scope project, since out of scope reads as
        # nonexistent. Re-deriving it here would either duplicate that logic or
        # leak the name it exists to withhold.
        hint = _tail(block.get("hint") or "")
        raise PolicyDenied(
            f"Denied: no project was given and this workspace resolves to "
            f"none, so there is nowhere to file this {thing}. Nothing was "
            f"saved. Name a project from list_projects(){or_global}. There is "
            f"no project-less {thing} any more: a record filed under nothing "
            f"is one nobody finds again, which is what this release removed."
            + (f" {hint}" if hint else "")
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

        Only claiming global scope requires holding it. Writing a note with
        no project at all used to be the other way past this; since 0.1.4 step
        4 `resolve_write_project` refuses that too, so a scoped grant has no
        remaining route to content every other grant can read.

        Scope of this check, so nobody reads it as more than it is: it refuses
        the *claim* of global scope, which is what pulls a note into every
        project's scoped read. Notes written before 0.1.4 with no project are
        untouched and still visible to every grant on an unscoped
        `search_context()` or `get_by_id()` — that is the existing grant model,
        and nothing here narrows it retroactively.
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

    def t_register_project(self, client: str, name: str, overview: str,
                           conventions: str = "", open_questions: str = "",
                           confirmed_by_user: bool = False) -> dict:
        """Create-or-link a project and bind the server's own directory to it.

        **The path is not an argument and deliberately cannot be.** The
        assistant supplies the description; the server supplies the location.
        A path parameter would put the one fact the caller cannot verify back
        under the caller's control, which is the whole failure this release
        exists to remove.

        Authorship is the calling client, unconditionally. `confirmed_by_user`
        records that a human approved the text and never changes who wrote it:
        if approval flipped the label to human-authored, one rubber-stamped
        "looks good" would erase the provenance permanently. It is also an
        unverifiable self-report — like every other client claim here, this is
        policy for cooperative clients, not a sandbox — so it is stored as the
        weaker fact it actually is.
        """
        grant = self._guard(client, "register_project")
        name = name.strip()
        if not name:
            # Nothing downstream refuses this: `fnmatch("", "*")` is True, so
            # the scope check passes, and no layer validates the name. An empty
            # project would take the directory binding and make every correct
            # registration afterwards fail with "already registered to ''" —
            # unfixable from here, since deleting is human-only by design.
            return {"error": "A project name is required. Use the "
                             "`suggested_name` from list_projects(), or ask "
                             "the user what to call it."}
        self.check_write_project(grant, name)
        cwd = current_directory()
        if cwd is None:
            return {"error": "This server has no readable working directory, "
                             "so there is nothing to register. Ask the user to "
                             "run: evoctx init --name <name>"}
        # masked before storage, like every other write that takes free text
        (overview, conventions, open_questions), inbound = self._redact_inbound(
            grant, overview, conventions, open_questions)
        try:
            root = self.db.register_project(
                name, cwd, overview=overview, conventions=conventions,
                open_questions=open_questions, author=client,
                reviewed=confirmed_by_user,
            )
        except (RootOwnedByAnotherProject, RootNotADirectory, ValueError) as e:
            # expected refusals, not failures: they carry the reason a human
            # needs, and an exception would reach the client as a stack trace
            return {"error": str(e)}
        # the scoped read, not the raw row: linking an existing project is the
        # main case, and returning it unfiltered would hand back an overview
        # this grant redacts and notes its exclude_tags hide
        project = self.get_project(grant, name)
        self._log(client, "register_project", "write", grant, {"name": name},
                  [name], project, inbound_hits=inbound)
        return {**(project or {"name": name}), "root": root}

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
        self.check_write_global(grant, is_global)
        # resolution BEFORE the scope check, so a defaulted project is scoped
        # like any other: `allows_project(None)` is always True, so checking
        # first would wave through a null that then resolves to a project this
        # grant does not cover. A scope-denied default must fail loudly rather
        # than fall back to NULL, which would file the note nowhere to dodge
        # an error.
        project = self.resolve_write_project(grant, project, is_global,
                                             tool="write_note")
        self.check_write_project(grant, project)
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
        project = self.resolve_write_project(grant, project, is_global=False,
                                             tool="start_session")
        self.check_write_project(grant, project, thing="session")
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
