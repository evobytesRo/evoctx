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
    "list_grants", "register_project",
]

# A grant may list "*" instead of enumerating tools, so it never goes stale as
# tools are added. It is now the shipped default, and 0.1.4 is where that
# changed.
#
# 0.1.3's default enumerated every tool, on the reasoning that "*" breaks a
# 0.1.2 machine: 0.1.2 raises GrantError on any unrecognised name including
# "*", inside grants() -> active_grant() -> PolicyEngine.check(), so every tool
# call there fails. True — but it does not distinguish the options, which is
# what a default has to do. An enumerated default listing `register_project`
# breaks 0.1.2 identically, for the identical reason, and a default that
# omitted the tool to stay 0.1.2-readable would deny it on every fresh 0.1.4
# store instead. Only 0.1.3+ drops unknown names gracefully. The 0.1.2 exposure
# is the same whichever way this goes, so it decides nothing.
#
# What does decide: a fresh store has no deliberately-narrow grant to respect,
# so "*" costs nothing there and means the file never needs revisiting as tools
# are added. Existing files are left alone and upgraded on request, by
# `evoctx grant upgrade` — which preserves a narrow grant by refusing to touch
# one at all (see `predates_current_tools`), NOT by "only adding tool names".
# That distinction is the round-1 defect of this release: adding every missing
# name to a read-only grant hands over the whole store while leaving scope and
# expiry untouched to look conservative.
TOOLS_WILDCARD = "*"

# The complete tool set of each earlier release, newest first.
#
# `grant upgrade` exists for one situation: a grant that meant "everything"
# when it was written, and no longer does because a release added a tool. That
# is only recognisable against what "everything" *was*. Without this list the
# only available rule is "every tool this build ships that you did not name",
# which cannot tell a stale everything-grant from a deliberately narrow one —
# and would answer a read-only payments grant by adding `write_note` to it.
#
# So an enumerated grant is auto-upgraded only when its tools are exactly one
# of these sets. Anything else is the user's own composition: neither edited
# nor reported. `doctor` passes it, because a narrowing someone made on
# purpose is not a fault, and reporting it would make the health check
# unsatisfiable without destroying the grant.
#
# **Append the outgoing set here whenever ALL_TOOLS grows.** A release that
# forgets does NOT degrade to the manual channel — it fails silently and in the
# worst direction: the stale grant matches no prior set, so it is never
# considered at all. `doctor` then passes a grant that does not cover the
# build's tools, `grant upgrade` reports nothing missing while blaming the user
# for a narrowing they never made, and the denial sends them after a wider
# grant that does not exist. That is precisely the lockout this release
# removes, reintroduced by an omission.
#
# `test_prior_tool_sets_covers_the_previous_release` is what actually enforces
# the append; this comment cannot. Growing ALL_TOOLS without adding the
# outgoing set fails in CI instead of on someone's machine.
PRIOR_TOOL_SETS = [
    # 0.1.0 - 0.1.3
    ["list_projects", "get_project", "search_context", "get_by_id",
     "write_note", "start_session", "end_session", "recent_sessions",
     "list_grants"],
]

DEFAULT_GRANTS_YAML = """\
# evoctx grants — what a connected AI client may do with your store.
# Activate one with:  evoctx grant activate <name>
# Fields:
#   client    glob matched against the client's self-reported name
#   tools     which MCP tools the client may call. "*" means every tool,
#             including ones added by future releases, which is why it is the
#             default — an enumerated list silently denies each new tool until
#             you come back and add it. Enumerate when you mean to restrict,
#             but know what that costs: a list you wrote yourself is NEVER
#             auto-upgraded, deliberately, because evoctx cannot tell a
#             restriction you meant from one a release left behind. Add new
#             tools by hand, or use "*". `evoctx grant upgrade` only refreshes
#             a grant that listed every tool of an earlier release. (Note:
#             evoctx 0.1.2 rejects "*" and would fail every call reading this
#             file. So does any list naming a tool 0.1.2 never had, so this is
#             not a reason to enumerate.)
#   scope     projects allowlist (glob ok) + tags whose notes are invisible
#   redactions  regex rewrites applied to every response before it leaves
#   expires   mandatory: ISO timestamp, or +Nd/+Nh/+Nm relative to activation
grants:
  - name: everything
    client: "*"
    tools: ["*"]
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


_NAME_KEY = re.compile(r"^\s*-\s+name:\s*[\"']?([^\"'\s#]+)", re.MULTILINE)
_TOOLS_KEY = re.compile(r"^\s*tools:\s*", re.MULTILINE)


def predates_current_tools(tools: list[str]) -> bool:
    """Did this exact tool list mean "every tool" in some earlier release?

    The one rule behind three answers that have to agree: whether `upgrade`
    edits a grant, whether `doctor` reports it, and whether a denial tells the
    user their grant is out of date. If they disagree, `doctor` fails a config
    the upgrade refuses to fix, or a denial sends someone to run a command
    that will not touch their grant.

    False for a wildcard grant (nothing to be out of date about) and false for
    any list the user composed themselves — a grant missing `write_note`
    because someone wanted it read-only does not predate anything.
    """
    return (TOOLS_WILDCARD not in tools
            and set(tools) in [set(s) for s in PRIOR_TOOL_SETS])


def _insert_tools(text: str, close_at: int, names: list[str]) -> str:
    """`names` added just inside the `]` at `close_at`. One definition, because
    the candidate that gets validated and the text that gets written must be
    produced the same way or the validation proves nothing."""
    return text[:close_at] + ", " + ", ".join(names) + text[close_at:]


def _tools_by_grant(text: str) -> dict[str, set[str]]:
    """Grant name -> the tools it actually grants, parsed from a candidate
    document that is not on disk.

    The point is the "not on disk" part: it lets `upgrade_tools` check its own
    edit before writing, instead of writing and hoping the rollback path is
    reachable.

    Uses `yaml.safe_load` — the same loader `grants()` does — but not
    `_parse_grant`, so this answers "would this file load, and what would it
    grant", not "is every grant in it valid". That is the question the caller
    has: it is inserting names into one sequence, which cannot make a
    previously-valid grant invalid.

    Entries whose tools are not a plain list of strings are skipped rather
    than coerced. `tools: ["*", [oops]]` loads fine — the wildcard
    short-circuits `_parse_grant`'s unknown-name check, so the nested list
    survives — and `set()` on it raises TypeError from a function whose
    callers are guarding for parse errors.
    """
    data = yaml.safe_load(text) or {}
    out: dict[str, set[str]] = {}
    for raw in data.get("grants") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        tools = raw.get("tools") or []
        if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
            continue
        out[str(raw["name"])] = set(tools)
    return out


def _tools_sequence_span(text: str, grant_name: str) -> int | None:
    """Offset of the closing `]` of `grant_name`'s `tools:` flow sequence.

    Returns None when there is nothing safe to edit — the grant is not found,
    it has no `tools:` before the next grant begins, or its tools are a block
    sequence rather than a bracketed one. None means "tell the user", never
    "guess": this file gates every tool call, so a bad edit denies all of them.

    Brackets are matched outside string literals, so a tool name containing
    `]` cannot unbalance the scan — the same rule `install.py` applies to
    `.code-workspace` files, for the same reason.
    """
    starts = {m.group(1): m.start() for m in _NAME_KEY.finditer(text)}
    if grant_name not in starts:
        return None
    begin = starts[grant_name]
    # bounded by the next grant, so a later grant's tools: is never edited
    later = [s for s in starts.values() if s > begin]
    end = min(later) if later else len(text)

    key = _TOOLS_KEY.search(text, begin, end)
    if key is None:
        return None
    i = key.end()
    while i < end and text[i] in " \t":
        i += 1
    if i >= end or text[i] != "[":
        return None            # block sequence, or empty — not ours to rewrite

    depth, in_string, quote, escaped = 0, False, "", False
    while i < end:
        c = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == quote:
                in_string = False
        elif c in "\"'":
            in_string, quote = True, c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None                # unbalanced — treat as unreadable, not as editable


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

    def allows_all_projects(self) -> bool:
        """Does this grant cover every project, including ones not yet created?

        Its own method so the one caller that needs "covers everything" — the
        global-note write check — stops being the single place that inspects
        `.projects` literally. `"**"` and `"?*"` also match anything under
        fnmatch, so they count.
        """
        return any(pat in ("*", "**", "?*") for pat in self.projects)

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
    # `projects: []` reads as "no projects" and used to parse as full access,
    # because `or ["*"]` cannot tell an empty list from a missing key. That is
    # a scope failing open, and it now also decides whether a grant may write
    # global notes — so refuse it rather than guess which was meant.
    if "projects" in scope and not scope["projects"]:
        # `not`, not `== []`: a bare `projects:` in YAML parses to None, which
        # is the spelling a human is likelier to type and which failed open
        # the same way. A key that is present but empty is a scope somebody
        # meant to write; guessing "everything" from it is the wrong guess,
        # and it now also gates who may write global notes.
        raise GrantError(
            f"grant '{raw['name']}': 'projects' is present but empty, which is "
            f"ambiguous — write projects: [\"*\"] for every project, or name the "
            f"ones you mean. Remove the key entirely to accept the default."
        )
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

    def missing_tools(self) -> dict[str, list[str]]:
        """Per grant that *meant every tool*, the tools it no longer covers.

        Deliberately not "every tool this build ships that you did not name".
        That rule cannot distinguish a grant left behind by a release from one
        someone narrowed on purpose, so it answers a read-only payments grant
        by reporting `write_note` as missing — and then `doctor` fails a
        correct configuration, the denial text claims a grant "predates" a tool
        it excludes on purpose, and `upgrade` offers to add it. The narrowing
        is the user's decision and nothing here gets to second-guess it.

        A grant counts as stale only when its tools are exactly one of
        `PRIOR_TOOL_SETS` — a complete set from an earlier release, i.e.
        provably "everything, as of then". Empty for a `"*"` grant, which is
        what the wildcard is for.
        """
        out = {}
        for name, grant in self.grants().items():
            if not predates_current_tools(grant.tools):
                continue
            gap = [t for t in ALL_TOOLS if t not in grant.tools]
            if gap:
                out[name] = gap
        return out

    def upgrade_tools(self, dry_run: bool = False) -> dict:
        """Add newly-shipped tools to a grant that meant *every* tool when it
        was written.

        Exists because **adding an MCP tool denies it to every existing user**:
        a grant lists the tools that existed when it was written, and a name
        it does not contain is refused. A release that adds a tool therefore
        has to ship the way to grant it, or the tool is unreachable for
        everyone who already has a `grants.yaml`.

        **Which grants those are is `predates_current_tools`' answer, and the
        protection lives there — not in "we only add tool names".** That was
        the original reasoning and it is insufficient: adding every missing
        tool name to a read-only payments grant hands over the whole store
        just as surely as rewriting it to `"*"` would, while leaving scope and
        expiry untouched to make it look conservative. Only a grant whose
        tools are exactly a `PRIOR_TOOL_SETS` entry is edited; a list the user
        composed is left alone entirely — not edited, and not reported either,
        since a narrowing someone made on purpose is not a fault to flag.
        Scope, expiry and client are never touched by either path.

        **Edits the text, does not round-trip the YAML.** `grants.yaml` is
        documentation as much as configuration — the shipped file is more
        comment than data, and users hand-edit it. `yaml.safe_dump` would
        parse and re-emit it, dropping every comment and reordering keys, so
        the upgrade would cost the user their own annotations. Instead the
        missing names are inserted before the closing `]` of each grant's
        `tools:` sequence, leaving every other byte untouched.

        A grant whose `tools:` is a block sequence (`- list_projects` on its
        own line) has no `]` to insert before, and guessing at indentation to
        synthesise one is how a config file gets corrupted. Those are reported
        under `manual` with the names to add, rather than edited.
        """
        # `errors` is per grant, not one field: success is per candidate now,
        # so a single `error` alongside a populated `updated` reads as "nothing
        # was written" on the same screen as the list of what was written.
        report: dict = {"updated": {}, "manual": {}, "errors": {}, "backup": None}
        gaps = self.missing_tools()
        if not gaps:
            return report

        # Bytes, not text. `read_text`/`write_text` translate newlines, so a
        # LF file on Windows comes back CRLF — every line rewritten, and a
        # `.bak` that is not a copy of what was there. "Leaving every other
        # byte untouched" has to be literally true of a file people version.
        raw = self.grants_path.read_bytes()
        text = original = raw.decode("utf-8")

        candidates = []
        for name, gap in gaps.items():
            span = _tools_sequence_span(text, name)
            if span is None:
                report["manual"][name] = gap
            else:
                candidates.append((span, name, gap))

        # **Each candidate is validated ALONE, against the untouched original.**
        # Validating the combined edit and discarding everything on failure
        # means one grant with a legacy quirk — a legal trailing comma is
        # enough — vetoes every other grant in the file, so the upgrade path
        # this release exists to ship degrades to a no-op and tells the user to
        # hand-edit grants that needed nothing. Per-candidate, one bad grant
        # costs only itself.
        #
        # Validating BEFORE the file is touched, rather than writing and
        # rolling back: `grants()` runs yaml.safe_load, so a malformed sequence
        # raises yaml.YAMLError, not GrantError, and a rollback guarded on the
        # wrong exception leaves a corrupt grants.yaml on disk — which denies
        # every tool call, not just this one.
        accepted = []
        for close_at, name, gap in candidates:
            trial = _insert_tools(original, close_at, gap)
            try:
                landed = _tools_by_grant(trial)
            except (GrantError, yaml.YAMLError, TypeError) as e:
                # TypeError is a belt, not the fix: `_tools_by_grant` skipping
                # entries whose tools are not a list of strings is what stops
                # an unhashable `tools: ["*", [oops]]` elsewhere in the file
                # from raising. Kept because nothing above this catches one if
                # a future edit reintroduces the possibility, and the cost of
                # being wrong is a traceback out of an unrelated grant.
                report["errors"][name] = str(e)
                report["manual"][name] = gap
                continue
            # ...and the names must actually land somewhere that grants them. A
            # `]` inside a comment inside the sequence ends the bracket scan
            # there, so the insert can go into the comment: valid YAML, parses
            # clean, grants nothing, and every re-run appends another copy.
            if not set(gap).issubset(landed.get(name, set())):
                report["manual"][name] = gap
                continue
            accepted.append((close_at, name, gap))

        if not accepted:
            return report

        # Reverse document order, so an edit never shifts an offset located
        # before it.
        for close_at, name, gap in sorted(accepted, reverse=True):
            text = _insert_tools(text, close_at, gap)
            report["updated"][name] = gap

        # The composition, once. Each part is known good and the inserts are
        # disjoint, so this should never fire — but "should never" is not a
        # reason to write an unchecked file that gates every tool call.
        try:
            _tools_by_grant(text)
        except (GrantError, yaml.YAMLError, TypeError) as e:
            for name in report["updated"]:
                report["errors"][name] = str(e)
            report["manual"].update(report["updated"])
            report["updated"] = {}
            return report

        if dry_run:
            return report
        backup = self.grants_path.with_suffix(".yaml.bak")
        backup.write_bytes(raw)
        self.grants_path.write_bytes(text.encode("utf-8"))
        report["backup"] = backup
        self._grants_mtime = -1.0
        return report

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
