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
from .db import (
    ENOUGH_HITS,
    SEARCH_SCAN_LIMIT,
    AmbiguousPrefix,
    Database,
    RootNotADirectory,
    RootOwnedByAnotherProject,
)
from .grants import (
    ALL_TOOLS,
    Grant,
    GrantStore,
    match_client_key,
    predates_current_tools,
)
from .holds import HOLD_DEFAULT_HOURS, validate_hold_hours
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


def _open_sessions_warning(others: list[dict], total: int) -> str:
    """The line an assistant reads after closing its own session.

    `others` must ALREADY have been through the redactor. Truncating first
    and redacting after leaks: a grant rule matching text that straddles
    character 60 masks the stored intent and not the clipped copy, so the
    structured field says `[CLIENT]` while this sentence still carries the
    name. Redact, then clip.

    `total` is passed rather than derived from `len(others)`, which is a
    capped, filtered list — reporting its length as the number of open
    sessions states a false fact with the confidence of a count.

    Phrased as an observation, not an instruction. Another window's session
    being open is the normal case on a machine running two of them, and the
    caller has no way to know whether that session was abandoned or is being
    worked in right now — so it must not read as "close this too". Closing
    someone else's live session strands it with a summary of work it did not
    do, which is worse than leaving it open, which is merely untidy.
    """
    # The client is named because it is the one field that helps the reader
    # decide whose session this is, which is the whole question the warning
    # raises and deliberately does not answer for them.
    shown = "; ".join(
        f"{s['id'][:8]} [{s.get('project') or 'no project'}] "
        f"({s.get('client') or 'unknown'}) "
        f"{(s.get('intent') or '-')[:60]}"
        for s in others[:3]
    )
    more = f" (+{total - 3} more)" if total > 3 else ""
    plural = "session is" if total == 1 else "sessions are"
    return (
        f"Closed. Note that {total} other {plural} still open: {shown}{more}. "
        f"That may be another window working right now — do not close it on a "
        f"guess. If it is yours and was interrupted, read its notes first, then "
        f"end_session(summary, session_id=…) with the full id from "
        f"other_open_sessions."
    )


class PolicyDenied(Exception):
    """Tool call not covered by the active grant. Message is user-actionable."""


def _names_session(arg: str, session_id: str) -> bool:
    """Does `arg` identify `session_id` the way the id resolver would?

    Case-insensitive because `_id_by_prefix` matches with LIKE, which folds
    ASCII case; a prefix counts only at the length the resolver requires.
    """
    a, full = arg.lower(), session_id.lower()
    return a == full or (len(a) >= PolicyEngine.MIN_SESSION_PREFIX
                         and full.startswith(a))


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
        # The client is passed, not omitted: a client with its own entry in
        # `active_grant.json` resolves to that grant, and only a client with
        # no entry falls back to the machine-wide default. The recovery
        # command likewise belongs to whichever of those answered — a fixed
        # `grant activate everything` here would send a per-client denial
        # after the scalar, which changes nothing for the denied client.
        active, reason = self.grants.active_grant(client)
        if active is None:
            raise PolicyDenied(
                f"Denied: {reason}. Ask the user to run: "
                f"{self.grants.recovery_command(client)}"
            )
        grant = active.grant
        if not grant.matches_client(client):
            # Reachable two ways now. The machine-wide grant may simply be
            # client-scoped elsewhere — or a hand-written `clients` entry may
            # point a key at a grant whose own `client:` glob does not cover
            # the client that matched the key. `activate --client` refuses
            # that when the key is a concrete name, but a glob key cannot be
            # checked up front, so it lands here and has to name the key.
            where = (f"the grant for client key '{active.client_key}'"
                     if active.client_key else "active grant")
            raise PolicyDenied(
                f"Denied: {where}, '{grant.name}', covers client "
                f"'{grant.client}', not '{client}'. Ask the user to run: "
                # The same source as every other channel. Hand-building one
                # here produced `activate <name> --client "claude*"` beside a
                # `hint` of `deactivate --client "claude*"` — two commands for
                # one state, and the one this branch invented rewrites the
                # entry that excludes the client with itself.
                f"{self.grants.recovery_command(client)}"
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
            # `grant upgrade` edits grants.yaml, so it is per-grant and needs
            # no client scope. Widening does: without `--client "<key>"` a
            # narrowed client is sent to a machine-wide activate that updates
            # the scalar it does not resolve through, leaving it denied. This
            # branch fires on every write the narrowed client attempts — the
            # scenario per-client grants exist for — and was the last one
            # still carrying the pre-feature string.
            fix = ("run: evoctx grant upgrade — this grant predates "
                   f"{tool}() and lists only the tools that existed when it "
                   "was written"
                   if outdated else
                   f"activate a wider grant: {self.grants.widen_command(client)}")
            raise PolicyDenied(
                f"Denied: grant '{grant.name}' does not allow {tool}(). "
                f"Allowed: {', '.join(grant.effective_tools())}. Ask the user to "
                f"{fix}"
            )
        return grant

    # ── scoped reads ──────────────────────────────────────────────────────────

    def _project_readable(self, grant: Grant, project: str | None,
                          is_global: bool = False) -> bool:
        """Whether a row's project puts it inside this grant's scope.

        `allows_project(None)` is True unconditionally, which made every
        project-less row readable by every scoped grant. That was a tolerable
        edge while such rows were only legacy notes from before 0.1.4 — and
        `check_write_global`'s docstring said so out loud. It stopped being an
        edge when `delete-project` began producing them in bulk: deleting one
        project made its notes and session intents readable by a grant scoped
        to a different one, which is precisely the widening that same check
        refuses on the write side.

        The distinction the release already draws is the one that fixes it. A
        **global** note says "this applies to every project", so it applies to
        the scoped grant's project too and stays visible. A **project-less**
        note says nothing about where it belongs, so it may only be read by a
        grant that covers everything. Silence is not consent to cross a scope
        boundary — the same reasoning as refusing a project-less write.
        """
        if project is not None:
            return grant.allows_project(project)
        if is_global:
            return True
        return grant.allows_all_projects()

    def _note_visible(self, grant: Grant, note: dict) -> bool:
        if not self._project_readable(grant, note.get("project"),
                                      bool(note.get("is_global"))):
            return False
        return not any(t in grant.exclude_tags for t in note.get("tags", []))

    def list_projects(self, grant: Grant) -> list[str]:
        return self._out(grant, [p for p in self.db.list_projects() if grant.allows_project(p)])

    def workspace(self, grant: Grant, client: str, cwd=None) -> dict:
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
                f"as unresolved. Ask the user to run: "
                # `list_projects()` is the first call of every session, so
                # this is the denial a model meets most often — and it was the
                # last one still ending in "activate a wider grant" with no
                # command to relay. A narrowed client told that runs a
                # machine-wide activate against the scalar it does not resolve
                # through, and stays exactly as denied.
                f"{self.grants.widen_command(client)}"
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

    # Hard ceiling on how much of the store one scoped read will look at.
    #
    # Two meanings, worth telling apart. For `_fetch_filtered`
    # (`recent_sessions`) and `_visible_open_sessions` it bounds how far a scan
    # will chase enough visible results, and it is not expected to be reached.
    # For `_widen_until_enough` it bounds each ordered pass that produces
    # `total`, and a broad query on a large store reaches it routinely
    # — which is what `total_is_lower_bound` exists to report. Real stores are
    # personal and small, so neither is a practical limit today.
    _MAX_SCAN = SEARCH_SCAN_LIMIT

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

    def search(self, grant: Grant, query: str, project: str | None, limit: int,
               offset: int = 0, page_token: str | None = None) -> dict:
        """One page of matches, plus how many there are in total.

        Returns an envelope rather than a bare list because a list cannot say
        "there are 40 of these and you are looking at 10" — and without that,
        an assistant has no way to tell a complete answer from a truncated one,
        and no reason to ask for the rest.

        **One store query per rung, not one per page.** The obvious
        implementation pages the store with a growing OFFSET until it runs
        out, and it is wrong in three ways at once, all measured:

        * `ORDER BY bm25(...)` cannot use an index, so SQLite scores and sorts
          every matching row on *every* page. Twenty pages is twenty sorts of
          the same set: measured at 1272 ms for a broad query matching every
          note on a 3000-note store, against 30 ms for the single pass below.
          Broad queries are exactly what this release's own guidance ("two or
          three bare words") produces, so that is the common case rather than
          the tail. What remains over a count-less page (5 ms at that size,
          1 ms at 378 notes) is the price of `total`, and at those absolute
          numbers it is one worth paying.
        * Twenty queries are twenty snapshots. One note written by another
          window between page 3 and page 4 shifts every later row by one, so a
          note lands in the response twice and `total` counts it twice. This
          store's whole premise is several assistant windows.
        * Exhaustion detected by "a page came back empty" is off by a page at
          the boundary, which made an exact count report itself as a floor.

        So: one ordered pass, capped, filtered, sliced. `_MAX_SCAN + 1` rows are
        asked for so that "there were exactly `_MAX_SCAN` matches" and "there
        were more than I looked at" stay distinguishable.

        The count is taken from the FILTERED rows and never from a store-level
        `COUNT`: a grant excluding `#personal` would otherwise report 40
        matches and hand back 6, which reads as a paging bug rather than as a
        grant doing its job.

        The widening ladder is walked HERE rather than left to
        `Database.choose_rung`, for the same reason. `choose_rung` counts what
        the store holds, which is the right answer for the CLI and the wrong
        one for a scoped client: a rung matching six notes in a project this
        grant cannot read has, for this caller, matched nothing — and stopping
        there returned `total: 0` while the next rung held a note the caller
        was perfectly entitled to. That needs no exotic setup, just a grant
        scoped to one project on a store that has several.
        """
        limit = max(0, limit)
        offset = max(0, offset)
        # Same keys as the success path, `page_token` included. Two shapes for
        # one response is how a caller learns to trust neither, and the tool
        # docstring names the fields without qualification.
        empty = {"results": [], "total": 0, "offset": offset, "limit": limit,
                 "has_more": False, "page_token": None}
        if project is not None and not grant.allows_project(project):
            return empty

        visible, capped, rung = self._widen_until_enough(
            grant, query, project, page_token)
        page = visible[offset:offset + limit]
        result = {
            "results": self._out(grant, page),
            "total": len(visible),
            "offset": offset,
            "limit": limit,
            # The rung this answer came from. Handed back so the NEXT page can
            # be drawn from the same one: each call re-walks the ladder, and a
            # note written by another window in between can push a narrower
            # rung over the threshold, which silently changes the result set
            # under a caller that is only trying to read page two.
            "page_token": rung,
            # Strictly "there is more you can reach by paging". It deliberately
            # does NOT cover matches past the scan ceiling: those cannot be
            # paged to, so saying so here would send a caller that obeys
            # "has_more means ask for the rest" round a loop that returns
            # nothing, for ever. `total_is_lower_bound` is what says the story
            # does not end here, and the tool docstring answers it with
            # "narrow the query", not "page".
            "has_more": limit > 0 and offset + len(page) < len(visible),
        }
        if capped:
            # Named so the assistant reads it as a floor rather than a fact.
            result["total_is_lower_bound"] = True
        return result

    def _widen_until_enough(
        self, grant: Grant, query: str, project: str | None,
        page_token: str | None = None,
    ) -> tuple[list[dict], bool, str]:
        """Walk the widening ladder, counting what this grant can actually see.

        Stops at the narrowest rung that leaves `ENOUGH_HITS` visible
        notes; otherwise keeps the rung that left the most, ties going to the
        narrower. Same rule as `Database.choose_rung`, one layer up, where the
        grant is known — that layer matters, because a rung matching six notes
        in a project this grant cannot read has, for this caller, matched
        nothing, and stopping there reported `total: 0` while the next rung
        held a note the caller was entitled to.

        Hitting the scan ceiling also stops the walk, but only when something
        visible came back with it. `capped` counts RAW rows, so a rung whose
        2001 matches are every one of them hidden would otherwise stop the
        ladder on the strength of rows this caller cannot see — the same
        pre-grant-count defect, reached through the other branch.

        `page_token` is a rung a previous call already settled on. It is
        accepted only when it is one this query's own ladder produces, so a
        token from a different query, or a hand-written one, cannot smuggle an
        arbitrary MATCH expression in; anything else is ignored and the ladder
        is walked normally.

        Each rung is fetched at most once, so one response is one snapshot per
        rung and the duplicate rows that came from paging the store rung by
        rung are not reachable from here.
        """
        ladder = self.db.widening_ladder(query)
        rungs = [page_token] if page_token in ladder else ladder
        best: list[dict] = []
        best_capped = False
        best_rung = rungs[0]
        for rung in rungs:
            rows = self.db.search(query, project=project,
                                  limit=self._MAX_SCAN + 1, offset=0, rung=rung)
            capped = len(rows) > self._MAX_SCAN
            visible = [n for n in rows[:self._MAX_SCAN]
                       if self._note_visible(grant, n)]
            if len(visible) >= ENOUGH_HITS or (capped and visible):
                return visible, capped, rung
            if len(visible) > len(best):
                best, best_capped, best_rung = visible, capped, rung
            elif capped and not best:
                # Nothing visible anywhere yet, and this rung's scan stopped
                # short — so "we looked at everything" is false even though
                # the answer is empty, and the caller has to be told that
                # rather than concluding the store holds nothing on the
                # subject. Guarded on `not best` so the flag always describes
                # the rung actually returned: a later capped rung must not
                # mark an earlier rung's exact, complete answer as a floor.
                best_capped = True
        return best, best_capped, best_rung

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
            # sessions carry no global flag — a project-less one belongs
            # nowhere, so only a grant covering everything may read it
            lambda s: self._project_readable(grant, s.get("project")),
            limit,
        )
        return self._out(grant, result)

    # ── scoped writes ─────────────────────────────────────────────────────────

    def check_write_project(self, grant: Grant, client: str, project: str | None,
                            thing: str = "note") -> None:
        # `client` is required, not defaulted, for the same reason `tool=` is
        # on `resolve_write_project`: the recovery command has to name the
        # per-client key, and a default here would be load-bearing for
        # whichever caller forgot to pass it. A scope denial used to carry no
        # command at all, so an assistant told to relay "the command from the
        # error" relayed one it knew — a machine-wide `grant activate`, which
        # updates the scalar a narrowed client does not resolve through.
        if not grant.allows_project(project):
            raise PolicyDenied(
                f"Denied: grant '{grant.name}' does not cover project "
                f"'{project}' (scope: {grant.projects}). The {thing} was NOT saved. "
                f"Ask the user to activate a wider grant: "
                f"{self.grants.widen_command(client)}"
            )

    def resolve_write_project(self, grant: Grant, client: str, project: str | None,
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
                block = self.workspace(grant, client)
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

        block = self.workspace(grant, client)
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

    def check_write_global(self, grant: Grant, client: str, is_global: bool) -> None:
        """A scoped grant may not write a note that spans every project.

        `check_write_project` cannot catch this: a global note carries
        `project=None`, and `allows_project(None)` returns True by design, so
        the project column alone cannot tell a scoped write from a global one.
        A grant scoped to one project could otherwise write content that lands
        in *every* project's scoped read (`get_project`,
        `search_context(project=…)`), which is precisely what the scope exists
        to prevent — and unlike a project-less note, a global one genuinely
        does reach scoped grants, by design (`_project_readable`).

        Only claiming global scope requires holding it. Writing a note with
        no project at all used to be the other way past this; since 0.1.4 step
        4 `resolve_write_project` refuses that too, so a scoped grant has no
        remaining route to content every other grant can read.

        Scope of this check, so nobody reads it as more than it is: it refuses
        the *claim* of global scope, which is what pulls a note into every
        project's scoped read. The other half — a note with no project at all —
        is handled on the read side by `_project_readable`, which since 0.1.4
        withholds project-less rows from any grant that does not cover every
        project. That narrowing applies retroactively to notes written before
        0.1.4; see CHANGELOG item 16 and SECURITY.md §5.
        """
        if is_global and not grant.allows_all_projects():
            raise PolicyDenied(
                f"Denied: grant '{grant.name}' is scoped to {grant.projects}, and a "
                f"global note appears under every project. Write it without "
                f"is_global, or ask the user to activate a wider grant: "
                f"{self.grants.widen_command(client)}. The note was NOT saved."
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
                # Not display: this records which grant refused. Resolved for
                # this client, or a per-client denial goes into the log under
                # the machine default's name — a record of the wrong grant.
                active, _ = self.grants.active_grant(client)
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

        Every caller declares `-> dict` and the MCP layer hands the result
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
        result = {"projects": projects, "workspace": self.workspace(grant, client)}
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
        self.check_write_project(grant, client, name)
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

    def t_search_context(self, client: str, query: str, project: str | None,
                         limit: int, offset: int = 0,
                         page_token: str | None = None) -> dict:
        grant = self._guard(client, "search_context")
        result = self.search(grant, query, project, limit, offset, page_token)
        # `record_ids` stays the ids actually handed over, so the audit line
        # still answers "what did this client read" — `total` is a count of
        # what matched, not of what was returned, and putting it here would
        # make the log claim more was read than was.
        # `page_token` is logged because it decides which set was served: two
        # calls with identical query/limit/offset can return disjoint results
        # if they were interpreted differently, and the record has to be able
        # to tell them apart.
        self._log(client, "search_context", "read", grant,
                  {"query": query, "project": project, "limit": limit,
                   "offset": offset, "page_token": result.get("page_token")},
                  [n["id"] for n in result["results"]], result["results"])
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
        self.check_write_global(grant, client, is_global)
        # resolution BEFORE the scope check, so a defaulted project is scoped
        # like any other: `allows_project(None)` is always True, so checking
        # first would wave through a null that then resolves to a project this
        # grant does not cover. A scope-denied default must fail loudly rather
        # than fall back to NULL, which would file the note nowhere to dodge
        # an error.
        project = self.resolve_write_project(grant, client, project, is_global,
                                             tool="write_note")
        self.check_write_project(grant, client, project)
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
        project = self.resolve_write_project(grant, client, project, is_global=False,
                                             tool="start_session")
        self.check_write_project(grant, client, project, thing="session")
        (intent,), hits = self._redact_inbound(grant, intent) if intent else ((intent,), 0)
        session_id = self.db.start_session(project=project, intent=intent, client=client)
        result = self._reread_session(session_id)
        self._log(client, "start_session", "write", grant,
                  {"project": project, "intent": intent}, [session_id], result,
                  inbound_hits=hits)
        return self._out(grant, result)

    def _resolve_session_target(self, client: str, grant: Grant, tool: str,
                                session_id: str,
                                too_short_tail: str) -> dict:
        """The session an id or prefix names, scope-checked — or an `error`
        dict, the refusal already audited as `denied`. A session dict never
        carries an `error` key, so `"error" in` tells the two apart.

        Shared by `end_session` and `hold_session`, which must refuse
        identically. Scoped before anything is touched: `recent_sessions`
        refuses to list an out-of-scope session, and `get_note` denies by
        returning nothing rather than explaining, so a caller cannot tell
        "out of scope" from "no such id".

        Both tools take an id, so a caller can name another project's session
        here, and this check is what stops it. It also fires when a session
        opened under a wider grant is still open after a narrower grant is
        activated.
        """
        # An id prefix resolves like everywhere else in this codebase. The
        # caller that matters here is an assistant reading an id out of an
        # earlier `end_session` warning or a `recent_sessions` listing, both of
        # which display a short prefix beside the full id; refusing the prefix
        # would make the displayed form a trap.
        #
        # A minimum length, though: closing is not undoable, and a hold keeps a
        # session's reminder quiet, and neither path has a confirmation step the
        # way `evoctx delete` has one. A unique one-character prefix would
        # otherwise act on a session, and a prefix that short is far likelier
        # to be a typo than an intention. Eight is what the tool docstrings
        # promise; promising it and not enforcing it is the kind of gap that
        # only shows up once.
        if (len(session_id) < self.MIN_SESSION_PREFIX
                and self.db.get_session(session_id) is None):
            self._log(client, tool, "denied", grant, None, [], None)
            return {"error": f"'{session_id}' is too short to identify a session. "
                                   f"Give at least {self.MIN_SESSION_PREFIX} characters of "
                                   f"the id, or the whole id.{too_short_tail}"}
        try:
            existing = (self.db.get_session(session_id)
                        or self.db.find_session_by_prefix(session_id))
        except AmbiguousPrefix as e:
            self._log(client, tool, "denied", grant, None, [], None)
            # The candidate ids are only shown when the caller may read every
            # one of them. `_id_by_prefix` resolves across the whole table, so
            # an ambiguity between a session this grant covers and one it does
            # not would otherwise name the second — and out of scope is
            # supposed to read as nonexistent everywhere else in this file.
            if all(self._project_readable(grant, s.get("project"))
                   for s in (self.db.get_session(i) or {} for i in e.ids)):
                return {"error": str(e)}
            return {"error": "No session with that id is open within this "
                                   "grant's scope."}
        if existing is None or not self._project_readable(
                grant, existing.get("project")):
            # Audited even though it is denied: `t_get_by_id` logs its blocked
            # reads for the same reason, and SECURITY.md calls the audit log
            # the accountability record. An attempt to act on and read a
            # session outside scope is exactly what it should hold. No ids in
            # the record — that would put in the log what the denial withholds
            # from the caller.
            # "denied", not "write" — `_guard` established that vocabulary for
            # a refused attempt, and `evoctx audit --action denied` is the
            # query a user runs to ask what a client tried and could not do.
            # Logging it as a write makes it indistinguishable at a glance
            # from a successful one in the one record SECURITY.md calls the
            # accountability record.
            self._log(client, tool, "denied", grant, None, [], None)
            # "this grant", not "this client". Closing a session another client
            # opened is the feature, not an accident — several windows share one
            # store and the whole point of taking an id is that you can close
            # the one you mean. What is checked here is project scope, and the
            # message has to name the check that actually ran.
            return {"error": "No session with that id is open within this "
                                   "grant's scope."}
        return existing

    def t_end_session(self, client: str, session_id: str, summary: str) -> dict:
        grant = self._guard(client, "end_session")
        existing = self._resolve_session_target(
            client, grant, "end_session", session_id,
            " Closing cannot be undone.")
        if "error" in existing:
            return existing
        session_id = existing["id"]
        (summary,), hits = self._redact_inbound(grant, summary)
        already_closed = not self.db.end_session(session_id, summary)
        result = self._reread_session(session_id)
        if already_closed:
            # Say so instead of returning a session that looks freshly closed.
            # `db.end_session` only updates rows with `ended_at IS NULL`, so a
            # second call leaves the first summary in place — and reporting
            # that untouched row as this call's result is how an assistant
            # concludes its summary was saved when another one was.
            result = dict(result)
            # A separate flag, not just the message, because the caller has to
            # act differently here than on any other error: this session is
            # definitively gone, so a connection holding it must let go of it.
            # Reading that out of prose would mean matching on a string.
            result["already_closed"] = True
            result["error"] = (
                f"Session {session_id[:8]} was already closed at "
                f"{result.get('ended_at')}; its existing summary was kept. "
                f"Nothing was written."
            )
        # Redact BEFORE the warning is built, not after. The warning quotes
        # another session's stored intent, clipped to 60 characters, and a
        # grant rule matching text that straddles that boundary would mask the
        # structured copy while this sentence still carried the value.
        result = self._out(grant, result)
        others, total = self._visible_open_sessions(grant, exclude_id=session_id)
        if others:
            others = self._out(grant, others)
            result = dict(result)
            result["other_open_sessions"] = others
            result["warning"] = _open_sessions_warning(others, total)
        # A close that changed nothing is a read. `_guard` established that a
        # refused attempt is "denied" precisely so the log distinguishes it at
        # a glance from a successful close; a no-op logged as a write is the
        # same lie in the other direction.
        self._log(client, "end_session", "read" if already_closed else "write",
                  grant, None, [session_id], result, inbound_hits=hits)
        return result

    def t_hold_session(self, client: str, session_id: str | None,
                       hours: float | None, own_session_id: str | None) -> dict:
        """Hold an open session so the Stop hook stays quiet while it is active.

        `own_session_id` is the session the calling connection started, or
        None. When there is one, it is the only session this call may hold:
        a hold keeps a reminder quiet, and on another window's session it
        would do that while nobody is working in it — renewed at each lapse,
        for ever. The argument is matched the way the resolver matches ids
        (case-insensitive, at least MIN_SESSION_PREFIX characters), and the
        full own id is what gets resolved, so the own-session path can never
        hit a prefix ambiguity. A caller with no session of its own can hold
        any session in scope by id; the server says what that costs.
        """
        grant = self._guard(client, "hold_session")
        try:
            hours = validate_hold_hours(HOLD_DEFAULT_HOURS if hours is None else hours)
        except ValueError as e:
            self._log(client, "hold_session", "denied", grant, None, [], None)
            return {"error": str(e)}
        # Not stripped: `end_session` takes the argument as given, and the two
        # must refuse identically. Blank means omitted.
        arg = session_id or ""
        if not arg.strip():
            arg = ""
        if not arg and own_session_id is None:
            self._log(client, "hold_session", "denied", grant, None, [], None)
            return {"error": "No session was started on this connection, and no "
                             "session_id was given. Call start_session() first. "
                             "If this conversation started a session before the "
                             "server restarted, hold that id: "
                             "hold_session(session_id=…)."}
        if own_session_id is not None and (not arg or _names_session(arg, own_session_id)):
            target = own_session_id
        elif own_session_id is not None and (
                len(arg) >= self.MIN_SESSION_PREFIX
                or self.db.get_session(arg) is not None):
            # A whole id shorter than the minimum is still an id: the resolver
            # accepts it, so it has to be refused here too.
            self._log(client, "hold_session", "denied", grant, None, [], None)
            return {"error": f"This connection's session is {own_session_id[:8]}; "
                             f"hold only your own. A hold keeps a session's "
                             f"reminder quiet, and on another window's session it "
                             f"would do that while nobody is working in it."}
        else:
            # too short is refused by the resolver, with its usual wording
            target = arg
        existing = self._resolve_session_target(
            client, grant, "hold_session", target, "")
        if "error" in existing:
            return existing
        sid: str = existing["id"]
        held = self.db.hold_session(sid, hours)
        result = self._reread_session(sid)
        if not held:
            # The only way the gated write changes nothing once the id has
            # resolved: the session is closed. Same flag `end_session` uses,
            # for the same reason — the caller has to let go of this session.
            result = dict(result)
            result["already_closed"] = True
            result["error"] = (f"Session {sid[:8]} was closed at "
                               f"{result.get('ended_at')}; nothing to hold.")
        result = self._out(grant, result)
        # A hold that wrote nothing is a read, as a no-op close is.
        self._log(client, "hold_session", "write" if held else "read", grant,
                  {"hours": hours}, [sid], result)
        return result

    #: Shortest id prefix `end_session` and `hold_session` will act on.
    #: Closing is not undoable, and a hold silences a reminder.
    MIN_SESSION_PREFIX = 8

    #: How many open sessions `other_open_sessions` carries. The prose warning
    #: names the first three of them and counts the remainder off the real
    #: total — see `_open_sessions_warning`, which owns that number.
    _WARN_SHOW = 5

    def _visible_open_sessions(self, grant: Grant,
                               exclude_id: str) -> tuple[list[dict], int]:
        """Other still-open sessions this grant may know about, and how many.

        Filtered through `_project_readable` like every other session read: a
        warning naming a session the caller cannot list, close, or read is a
        leak dressed as helpfulness.

        Filtering happens BEFORE the display cap, not after. Fetching five and
        then filtering is the pattern `_fetch_filtered` exists to eliminate —
        with five out-of-scope sessions newest, an in-scope one is silently
        invisible, and the caller is told about none of it. The count returned
        is the filtered total, so the warning states a number that is true
        rather than the length of the list it is naming sessions from. It is
        still bounded by `_MAX_SCAN`, which on a store with two thousand
        simultaneously open sessions would understate it; that is not a state
        anyone reaches, and the alternative is an unbounded read on a warning.
        """
        visible = [
            s for s in self.db.open_sessions(exclude_id=exclude_id,
                                             limit=self._MAX_SCAN)
            if self._project_readable(grant, s.get("project"))
        ]
        return visible[:self._WARN_SHOW], len(visible)

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
            # The name `describe` answered with, not the one resolution found:
            # when the resolved grant excludes this client, the response says
            # no access, and logging that grant's name would leave the record
            # and the response disagreeing about the same call.
            self.audit.log(client, "list_grants", "read",
                           grant_name=result.get("active_grant"),
                           result_count=1,
                           response_bytes=len(json.dumps(result, default=str)))
        return result

    # ── introspection ─────────────────────────────────────────────────────────

    def describe(self, client: str) -> dict:
        """What this client may actually do — resolved for *this* client.

        `list_grants()` exists to answer "may I call X?", so answering it from
        the machine-wide default while the caller's real calls resolve against
        a narrower per-client entry would be the one wrong answer this tool
        cannot afford. `client_key` is reported so the answer says which grant
        it came from.

        For the same reason it reports **no access** when the resolved grant's
        own `client` glob excludes this client. That grant denies every call,
        so listing its tools answers the question backwards — and the
        per-client map makes the case ordinary: a glob key can be pointed at a
        narrower grant, and no check up front can compare two globs.
        """
        active, reason = self.grants.active_grant(client)
        if active is not None and not active.grant.matches_client(client):
            reason = (f"grant '{active.grant.name}' covers client "
                      f"'{active.grant.client}', not '{client}'")
            active = None
        if active is None:
            # The hint has to be the same command the reason names. A generic
            # `grant activate <name>` here, beside a reason saying the entry
            # for key 'cursor*' expired, sends the user to update the
            # machine-wide scalar — which changes nothing for the client that
            # was denied, and looks like the fix failed.
            # `client_key` on this branch too: a caller reading it to work out
            # *which* entry answered needs it most on the denial it is calling
            # to diagnose, and a key present in one branch and absent in the
            # other is a KeyError waiting in exactly that path.
            key, tied = match_client_key(self.grants.client_entries(), client)
            answer = {"active_grant": None, "client": client, "client_key": key,
                      "reason": reason,
                      "hint": self.grants.recovery_command(client)}
            if tied:
                # `client_key` is null on a tie, which is exactly what "the
                # machine-wide default answered" looks like — the opposite
                # situation. Naming the tied keys is the only way a caller can
                # tell the two apart from the response alone.
                answer["ambiguous_keys"] = tied
            return answer
        grant = active.grant
        return {
            "active_grant": grant.name,
            "client": client,
            "client_key": active.client_key,
            "client_glob": grant.client,
            "client_matches": grant.matches_client(client),
            "allowed_tools": grant.effective_tools(),
            "scope": {"projects": grant.projects, "exclude_tags": grant.exclude_tags},
            # count only — the patterns themselves describe what's being hidden
            "redactions_active": len(grant.redactions),
            "expires_at": active.expires_at.isoformat(),
        }
