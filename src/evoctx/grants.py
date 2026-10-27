"""Grant configuration — who may do what with the context store.

Grants are declared in ``grants.yaml`` next to the store (hand-edited, no UI);
which one is active lives separately in ``active_grant.json``, written by
``evoctx grant activate``. Both files are re-read whenever their mtime
changes, so an activation is picked up by a running server without restart.

One grant is active machine-wide, and **since 0.1.4 a client may have its own**
— ``active_grant.json`` carries a ``clients`` map from a client-name glob to a
grant. That exists because a client-scoped grant used to scope the wrong thing:
activating a grant with ``client: "cursor*"`` did not narrow cursor, it *denied
every other client*, so the natural setup — read-only for one assistant, full
write for another — was unreachable. Resolution now happens per client, with
the machine-wide scalar as the fallback for clients that have no entry.

This is policy for cooperative clients, not a security sandbox: client
identity is self-reported and anyone with file access can edit the store
directly.
"""
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import NamedTuple

import yaml

ALL_TOOLS = [
    "list_projects", "get_project", "search_context", "get_by_id",
    "write_note", "start_session", "end_session", "recent_sessions",
    "list_grants", "register_project", "hold_session",
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
    # 0.1.4 - 0.1.5
    ["list_projects", "get_project", "search_context", "get_by_id",
     "write_note", "start_session", "end_session", "recent_sessions",
     "list_grants", "register_project"],
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


# fnmatch's full metacharacter set. Scoring a key by "length minus the stars"
# is the obvious rule and the wrong one: it counts the brackets of a character
# class as literal text, so `claude-code*` (11 literal chars, scored 11) would
# beat the strictly tighter `claude-cod[ae]` (12 literal chars, scored 14 only
# if `[`, `]` are miscounted). Excluding every metacharacter scores intent.
_KEY_WILDCARDS = "*?[]"

# `]` is not in this set: on its own it is an ordinary character to fnmatch,
# and a key containing one but no `[` matches literally. Only an opening
# bracket can begin a class.
_KEY_GLOB_CHARS = "*?["


def client_key_is_concrete(key: str) -> bool:
    """Does this map key name exactly one client, rather than a set of them?

    The distinction decides how much `evoctx grant activate --client` can
    promise. A concrete key is the client's own name, so it wins rule 1 of
    `match_client_key` against every glob and nothing can shadow it. A glob
    key competes with other globs, and comparing two globs in the abstract is
    automata work this file deliberately does not do.
    """
    return not any(c in _KEY_GLOB_CHARS for c in key)


def client_key_specificity(key: str) -> int:
    """How specific a key is: the number of characters that must match
    literally. Higher wins. See `_KEY_WILDCARDS` for why it is not `len()`."""
    return sum(1 for c in key if c not in _KEY_WILDCARDS)


def match_client_key(keys, client: str) -> tuple[str | None, list[str]]:
    """Which key in the `clients` map governs `client`.

    Returns `(key, tied)`. `tied` is non-empty only when the answer is
    genuinely ambiguous, and then `key` is None: the caller must deny rather
    than pick, the same way `db.py`'s `AmbiguousPrefix` refuses a note id
    prefix that matches two notes instead of resolving to whichever came
    first.

    The rule, and why it is applied here rather than at activation:

    1. an exact key wins — and **two exact keys tie exactly as two globs do**
    2. else, among the keys that match, the most specific one
    3. a tie at either tier is ambiguous — deny and name both

    `claude*`, `claude-c*` and `*-code` all match `claude-code`, and JSON
    preserves insertion order, so "most specific" would otherwise silently
    mean "whichever the file happens to list first". Ambiguity is a property
    of (client, key set) and only becomes decidable once there is a concrete
    client to match: refusing at activation would have to compare globs in the
    abstract, where `cursor*` and `vscode*` tie on specificity and can never
    both match anybody.

    **Both tiers casefold.** Client names are self-reported strings and nobody
    should have to guess whether their assistant calls itself `Cursor` or
    `cursor`. Left unpinned the two tiers disagree with each other *and across
    platforms*: `fnmatch` casefolds via `os.path.normcase` on Windows but not
    on Linux, while a dict lookup never does — so a store synced between two
    machines would resolve differently on each. `fnmatchcase` on casefolded
    strings is the same answer everywhere.

    Tier 1 having its own tie check is not symmetry for its own sake. Once
    matching casefolds, `Cursor` and `cursor` are the *same key* written
    twice, and `activate` refuses to create the second — but this file is
    hand-editable and gets merged and synced, so both can exist without ever
    passing through that guard. Returning the first one walked past would
    then resolve a grant by JSON byte order, and the order is not the user's
    intent: the entry that loses may be the narrow one they wrote most
    recently. A widening decided by file order is the failure this whole
    lookup is shaped to avoid.
    """
    folded = client.casefold()
    exact = sorted(k for k in keys if k.casefold() == folded)
    if len(exact) > 1:
        return None, exact
    if exact:
        return exact[0], []
    matching = [k for k in keys if fnmatchcase(folded, k.casefold())]
    if not matching:
        return None, []
    best = max(client_key_specificity(k) for k in matching)
    top = sorted(k for k in matching if client_key_specificity(k) == best)
    if len(top) > 1:
        return None, top
    return top[0], []


def _tie_is_between_exact_keys(tied, client: str) -> bool:
    """Did this ambiguity come from rule 1 rather than rule 2?

    The two tiers tie for opposite reasons and only one of them has a
    remedy. A rule-2 tie is several globs of equal specificity: adding an
    exact key outranks all of them. A rule-1 tie is the exact key, present
    more than once under different casings — so there is nothing left to
    outrank it with, and `activate` refuses every spelling by naming the
    other one.
    """
    folded = client.casefold()
    return all(k.casefold() == folded for k in tied)


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
        """Case-insensitive on every platform, like the `clients` map keys.

        This is the third `fnmatch` over a client name and it runs on every
        call. Casefolding the map alone would have moved the divergence rather
        than removed it: a key `Cursor` could resolve case-insensitively to a
        grant whose own `client: "cursor*"` then passed here on Windows —
        where plain `fnmatch` normcases — and **denied on Linux**, from one
        synced store.
        """
        return fnmatchcase(client_name.casefold(), self.client.casefold())

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
        """Case-**sensitive**, on every platform — the opposite choice from
        `matches_client`, for the opposite reason.

        Both were on plain `fnmatch`, which normcases on Windows and not on
        Linux, so one synced store answered differently on each machine. A
        client name is a self-reported string nobody should have to guess the
        casing of, so that one folds. A project name is a store key: it comes
        from `list_projects()`, it is what a note is filed under, and the
        store treats `Payments` and `payments` as two projects. Folding here
        would make a scope glob match a project the grant does not name — on
        Windows that was already happening.
        """
        if project is None:
            return True
        return any(fnmatchcase(project, pat) for pat in self.projects)

    def covers_all_clients(self) -> bool:
        """Does this grant's own `client` glob accept every client?

        Its own method for the same reason as `allows_all_projects`: the
        caller that needs it — the per-client activation warning — should not
        be the one place that knows which glob spellings mean "everything".
        """
        return self.client in ("*", "**", "?*")

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
            absolute = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            raise GrantError(f"grant '{raw['name']}': bad expires '{expires}'")
        if absolute.tzinfo is None:
            # `2027-01-01T00:00:00` parses, and then every comparison against
            # `datetime.now(timezone.utc)` raises `TypeError: can't compare
            # offset-naive and offset-aware`. Validation passed the value and
            # the crash surfaced far away — out of tool calls, and since this
            # release out of `doctor` and `grant list` too, the commands
            # someone runs to find out what is wrong. Refused here, where the
            # message can name the grant and the fix. At the MCP boundary it
            # is still an unhandled `GrantError` rather than a `PolicyDenied`
            # — it fails closed, and every malformed-grants.yaml case has
            # behaved that way since 0.1.0.
            raise GrantError(
                f"grant '{raw['name']}': expires '{expires}' has no timezone. "
                f"Add one — '{expires}Z' for UTC, or '{expires}+02:00' — or use "
                f"a relative value like +30d."
            )
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

    ``client_key`` is the ``clients`` map key this resolved through, or None
    for the machine-wide default. It is carried rather than recomputed because
    the recovery command a denial prints has to name the **key**, not the
    client: an entry filed under `cursor*` that denies client `cursor` would
    otherwise send the user to `deactivate --client cursor`, which matches no
    key and silently does nothing.
    """
    grant: Grant
    expires_at: datetime
    client_key: str | None = None


class GrantStore:
    """Loads grants.yaml + active_grant.json, hot-reloading on mtime change."""

    def __init__(self, store_dir: Path):
        self.grants_path = store_dir / "grants.yaml"
        self.active_path = store_dir / "active_grant.json"
        self._grants: dict[str, Grant] = {}
        self._grants_mtime: float = -1.0
        self._active: dict | None = None
        # `None` means "reload next read". It must NOT be a value `stat`
        # can also produce: this was -1.0, which is exactly what the read
        # computes for a file that does not exist — so after any write in
        # this process, deleting the file read as "cache is current" and a
        # later merge wrote stale entries back over the store.
        self._active_mtime: float | None = None
        # why the last read produced nothing, "" when it produced a real
        # (possibly empty) document — the difference between "no grant is
        # active" and "this file is unreadable", which the write path needs
        # and the read path deliberately flattens
        self._active_unreadable: str = ""
        self.ensure_default_files()

    def ensure_default_files(self) -> None:
        """Write the defaults a fresh store needs, and never raise here.

        This runs in `GrantStore.__init__`, which `cli.py` calls at module
        level — so anything that escapes fails *every* command, including
        `grant deactivate` and `doctor`, the two you reach for when something
        is wrong. A store whose `grants.yaml` exists but has no grant named
        `everything` did exactly that: no `active_grant.json`, so this called
        `activate("everything")`, which raised `GrantError` out of the import.

        Falling back to another grant keeps the store usable, and **the
        fallback is the narrowest one**, announced on stderr. Alphabetical
        order is not an access policy: with `admin` and `read-only` in the
        file it silently activated `admin`, machine-wide, from a read-only
        command like `doctor`. Deleting `active_grant.json` is a plausible
        "revoke everything", and that made it a self-grant.

        Nothing is activated at all when the file cannot be parsed. That is
        still a working store — every call is denied until someone activates
        a grant, which `doctor` reports and every denial names — and it is
        the only honest answer when the set of grants is unknown.
        """
        if not self.grants_path.exists():
            self.grants_path.write_text(DEFAULT_GRANTS_YAML, encoding="utf-8")
        if self.active_path.exists():
            return
        try:
            available = self.grants()
        except Exception:
            # NOT just GrantError: `grants()` runs `yaml.safe_load` and then
            # `.get` on the result, so a mis-indented file raises
            # `yaml.ParserError` and a top-level list raises `AttributeError`.
            # Both escaped from here — which is `GrantStore.__init__`, which
            # `cli.py` runs at import — and killed every command, including
            # `grant deactivate` and `doctor`, the two you reach for when
            # `grants.yaml` is what is wrong.
            return
        if "everything" in available:
            self.activate("everything")
            return
        if not available:
            return
        narrowest = min(available.values(),
                        key=lambda g: (len(g.effective_tools()), g.name))
        print(f"evoctx: no grant named 'everything' in {self.grants_path}; "
              f"activated '{narrowest.name}', the narrowest one. Change it with: "
              f"evoctx grant activate <name>", file=sys.stderr)
        self.activate(narrowest.name)

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

    def _write_active(self, state: dict) -> None:
        """Every write to active_grant.json goes through here, so the mtime
        cache is invalidated and the write stays atomic in one place.

        **Atomic, via a temp file and `os.replace`**, the way `db.py` writes
        its exports. `activate` is now a read-modify-write over the whole
        per-client configuration rather than a two-key overwrite, so an
        interrupted write no longer costs one line of JSON — it truncates the
        file that gates every tool call, and `_active_state` parses it with an
        unguarded `json.loads`.

        The mtime reset matters for the same reason: `activate` now *reads*
        before writing, and filesystem mtime granularity is coarse enough
        (milliseconds on Windows, worse elsewhere) that a read and a write
        inside one tick would leave the cache looking current — so an
        activation could be ignored by the very process that performed it.
        """
        tmp = self.active_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.active_path)
        self._active_mtime = None

    def activate(self, name: str, client_key: str | None = None) -> Grant:
        """Make `name` the active grant — machine-wide, or for one client.

        Without `client_key` this sets the machine-wide default, exactly as it
        always has, **except that it now merge-preserves the `clients` map**
        instead of writing a fresh two-key file over it. A rewrite would make
        every routine `grant activate` a silent wipe of the per-client setup.

        With `client_key` it writes one entry in that map. The key is a glob
        matched against client names, and it carries its own `activated_at`,
        so each client's grant expires on its own clock.

        Two refusals here, both cheaper than the denial they prevent:

        - **a key that differs from an existing one only by case.** Matching
          is case-insensitive, so `Cursor` and `cursor` are the same key;
          writing the second would leave a shadow entry that nothing can ever
          resolve to and no listing distinguishes.
        - **a grant whose own `client:` glob cannot cover this key** — but
          only when the key is a concrete client name. Both sides are globs
          otherwise, and general glob-vs-glob intersection is automata work
          nobody should write for this: grant `client: "claude-code"` with
          `--client "claude-*"` fails a naive `fnmatch` even though the key
          does cover that client. A real mismatch there is caught per call by
          `PolicyEngine.check`.
        """
        grant = self.grants().get(name)
        if grant is None:
            raise GrantError(
                f"no grant named '{name}' in {self.grants_path} "
                f"(have: {', '.join(self.grants()) or 'none'})"
            )
        self._refuse_if_unreadable()
        now = datetime.now(timezone.utc).isoformat()
        state = dict(self._active_state())
        # `client_entries`, not `state["clients"]`: the raw value is
        # hand-editable and an older build rewrites this file, so it can be a
        # list or a string. Reading it directly raised a ValueError out of
        # `dict()` — from `grant activate`, the command someone runs *because*
        # something is wrong. Anything unreadable is normalised away below
        # rather than preserved, since nothing can resolve through it anyway.
        clients = dict(self.client_entries())

        if client_key is None:
            state["name"] = name
            state["activated_at"] = now
            state["clients"] = clients
            if not clients:
                del state["clients"]
        else:
            # Stored stripped. A padded key is stored verbatim and then
            # matches nothing — `" cursor"` never equals a client name and
            # never globs one, so the entry is dead on arrival while every
            # check passes and the command reports success. Same shape as the
            # shadowing defect: the user believes they narrowed a client and
            # did not.
            client_key = client_key.strip()
            if not client_key:
                # Only ever matches a client whose self-reported name is
                # empty, and the server substitutes "unknown" for that — so
                # the entry is unreachable. It is also unremovable by its own
                # key through any CLI that treats "" as "no key given", which
                # is how `deactivate ""` reached the kill switch.
                raise GrantError(
                    "--client needs a client name or glob; an empty key can "
                    "never match a client. Omit --client to set the "
                    "machine-wide grant."
                )
            if client_key_is_concrete(client_key) and not grant.matches_client(client_key):
                raise GrantError(
                    f"grant '{name}' covers client '{grant.client}', which does "
                    f"not match '{client_key}' — every call from that client "
                    f"would be denied. Pick a grant whose 'client' covers it, or "
                    f"widen this one in {self.grants_path}."
                )
            clash = next((k for k in clients
                          if k.casefold() == client_key.casefold() and k != client_key), None)
            if clash is not None:
                raise GrantError(
                    f"a per-client grant already exists under key '{clash}', which "
                    f"differs from '{client_key}' only by case. Client matching is "
                    f"case-insensitive, so these are the same key — update that one "
                    f"instead, with the grant you want: evoctx grant activate <name> "
                    f"--client \"{clash}\". (Naming a grant here would complete to a "
                    f"command replacing whatever '{clash}' currently grants.)"
                )
            clients[client_key] = {"name": name, "activated_at": now}
            state["clients"] = clients
            # The scalar keys stay present whatever happens here. 0.1.3 reads
            # `state["activated_at"]` by subscript, so a file without it makes
            # an older build raise KeyError on every call — and this store is
            # the kind people sync between machines. Present, not necessarily
            # non-null: `deactivate` legitimately writes a null name, and
            # 0.1.3 reads that fine through `state.get("name")`.
            state.setdefault("name", None)
            state.setdefault("activated_at", now)

        self._write_active(state)
        return grant

    def deactivate(self, client_key: str | None = None) -> None:
        """Bare: the kill switch. With a key: remove one per-client entry.

        **Bare `deactivate` clears the scalar and the map, and must.** It is
        the switch the user pulls when something is wrong, and it says so:
        *"every MCP call is now denied"*. Merge-preserving the map here — the
        obvious symmetry with `activate` — would leave every client that has
        an entry fully active after the switch was pulled: silent privilege
        retention, introduced by the fix for the wipe problem.

        `client_key` matches a key **exactly**, never as a glob against the
        keys: globbing would make `deactivate --client "*"` delete every entry
        while looking like it named one. Without this, "no entry at all" is
        unreachable once an entry has been written — and that state is the
        only one that falls back to the machine-wide default.

        **Exactly means byte-identical**, not case-insensitively — the one
        place in this feature where case is not folded, and deliberately.
        Everywhere else folding is a convenience: nobody should have to guess
        whether their client calls itself `Cursor` or `cursor`. Here it is a
        delete. `activate` refuses to create two keys differing only by case,
        but a hand-edited file can hold both, and a fold then removes
        whichever came *first in the file* rather than the one named — and if
        the survivor is the wider of the pair, the removal is a widening. Even
        with a single candidate, folding turns a case typo into the silent
        deletion of a real access rule. So the spelling has to match, and a
        near miss says what the keys actually are.
        """
        now = datetime.now(timezone.utc).isoformat()
        if client_key is None:
            # No guard: the kill switch never reads the file, and it is the
            # documented way out of an unreadable one.
            self._write_active({"name": None, "activated_at": now})
            return
        self._refuse_if_unreadable()
        state = dict(self._active_state())
        clients = dict(self.client_entries())
        if client_key not in clients:
            near = [k for k in sorted(clients) if k.casefold() == client_key.casefold()]
            raise GrantError(
                f"no per-client grant under key '{client_key}' "
                f"(have: {', '.join(sorted(clients)) or 'none'})."
                + (f" Removal takes the key exactly as written, including case — "
                   f"did you mean {', '.join(repr(k) for k in near)}?" if near else
                   " This takes the map key, not a client name — see: evoctx grant list")
            )
        match = client_key
        del clients[match]
        state["clients"] = clients
        state.setdefault("name", None)
        state.setdefault("activated_at", now)
        self._write_active(state)

    def active_state_snapshot(self) -> dict:
        """A detached copy of active_grant.json, for undoing a write.

        Exists because every check `evoctx grant activate --client` performs
        asks what the store *resolves to*, which is only answerable once the
        entry is in it — so the command writes first and validates after. A
        non-zero exit that left the write applied inverts the contract the
        command documents ("exit 0 means usable"), and on one path it had
        already handed every tool to clients the failure message did not
        name.
        """
        return json.loads(json.dumps(self._active_state()))

    def restore_active(self, snapshot: dict) -> None:
        """Put back what `active_state_snapshot` captured.

        Normalised on the way out, because a snapshot of an unreadable file is
        `{}` — a state `_active_state` deliberately tolerates — and writing
        that back produces the one file shape this release promises never to
        write: no `activated_at`, which an evoctx older than 0.1.4 subscripts
        directly and fails every call on. This is the only write that does not
        go through `activate`'s own guard for that.
        """
        if not snapshot and self._active_unreadable:
            # The snapshot of a file that could not be parsed is `{}`, and
            # writing that back replaces whatever the user was about to
            # repair with a deny-all file — while the caller prints "Nothing
            # was changed." Leave it alone; `activate` refuses on this state
            # anyway, so the only way here is a rollback that has nothing to
            # roll back to.
            return
        state = dict(snapshot)
        state.setdefault("name", None)
        state.setdefault("activated_at", datetime.now(timezone.utc).isoformat())
        self._write_active(state)

    def client_entries(self) -> dict[str, dict]:
        """The `clients` map as written, for listings and health checks.

        Defensive about shape because this file is hand-editable and is the
        one artefact an older build rewrites: anything that is not a dict of
        dicts reads as no entries rather than raising through
        `active_grant`, which every tool call passes through.
        """
        raw = self._active_state().get("clients")
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)}

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
        """The parsed active_grant.json, or `{}` when it cannot be read.

        `{}` resolves to "no grant is active", which denies every call — the
        safe direction, and recoverable by `evoctx grant activate`. The
        alternative is what this used to do: raise `JSONDecodeError` out of
        `PolicyEngine.check`, so a truncated or hand-mangled file failed every
        tool call with a traceback instead of an explanation, and failed the
        `activate` that would have repaired it. Reported on stderr rather than
        swallowed, the way an unknown tool name in `grants.yaml` is.
        """
        mtime = self.active_path.stat().st_mtime if self.active_path.exists() else -1.0
        if mtime != self._active_mtime:
            unreadable = ""
            if not self.active_path.exists():
                # Absent is not corrupt. It is the fresh-store state, which
                # `ensure_default_files` fills in — treating it as unreadable
                # made the write guard refuse on a store that had never been
                # written to.
                state = {}
                self._active, self._active_unreadable = state, ""
                self._active_mtime = mtime
                return state
            try:
                state = json.loads(self.active_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
                unreadable = str(e)
                state = {}
            if not isinstance(state, dict):
                unreadable = "the file does not contain a JSON object"
                state = {}
            if unreadable:
                print(f"evoctx: cannot read {self.active_path} ({unreadable}); "
                      f"treating it as no active grant until it is fixed",
                      file=sys.stderr)
            self._active = state
            self._active_unreadable = unreadable
            self._active_mtime = mtime
        return self._active or {}

    def _refuse_if_unreadable(self) -> None:
        """Guard every write that merges into active_grant.json.

        `_active_state` answers `{}` for a file it could not parse, which is
        right for reads — no grant resolves, every call is denied, safe. It is
        catastrophic for writes: `activate` merges onto `{}`, sees no
        `clients`, and writes a clean two-key file. One stray byte in that
        file and the next routine `evoctx grant activate` silently deleted
        every per-client narrowing, exit 0, with a success line. That is the
        same wipe the CHANGELOG warns about for pre-0.1.4 builds, except 0.1.4
        doing it to itself from a plausible hand edit.

        A file that cannot be read is not a file whose contents are `{}`. So
        the write refuses, and says which of the two recoveries applies —
        repair the JSON to keep the entries, or `deactivate` to reset. Bare
        `deactivate` deliberately does not call this: it is the kill switch,
        it never reads the file, and it has to stay reachable when everything
        else about the store is broken.
        """
        if self._active_state() or not self._active_unreadable:
            return
        raise GrantError(
            f"{self.active_path} cannot be read ({self._active_unreadable}), so "
            f"writing to it would discard whatever it holds — including any "
            f"per-client grants. Fix the JSON by hand to keep them, or run "
            f"`evoctx grant deactivate` to reset the file and start over."
        )

    def recovery_command(self, client: str | None = None) -> str:
        """The command that fixes a denial **for this client**.

        One definition, and the reasons `active_grant` returns deliberately
        do NOT contain it: this is emitted through four channels a user may
        read side by side — a `PolicyDenied` message, `list_grants()`'s
        `hint`, `evoctx grant list`, `doctor` — and a reason carrying its own
        copy produced lines like *"...activate everything — run: evoctx grant
        activate everything"* where a consumer added another.

        Naming the key rather than the client is the whole point: an entry
        filed under `cursor*` that denies client `cursor` must not produce
        `--client cursor`, which matches no key and silently does nothing.

        Names a grant only where **re-activating that grant is the fix** —
        an expiry, where it resets the clock. Everywhere else it emits a
        `<name>` placeholder rather than guess, and the reason beside it
        carries what the user needs in order to fill one in.

        **It never emits a command that widens**, and neither may any caller
        that prints remediation: a command that removes a narrow entry hands
        its client back to the machine-wide grant, which is usually the wider
        one, so an assistant told to relay it verbatim would be relaying a
        request for the access it was just refused. Three review rounds each
        found that same command surviving in a surface that built its own
        text, which is why this function is the only producer and the others
        call it.
        """
        scope = ""
        if client is not None:
            entries = self.client_entries()
            key, tied = match_client_key(entries, client)
            if tied:
                if _tie_is_between_exact_keys(tied, client):
                    # **No command can fix this one.** Adding an exact key is
                    # the answer to a glob tie; here the exact key already
                    # exists twice, differing only by case, and `activate`
                    # refuses every spelling by pointing at the other — a
                    # closed loop with no exit, which is worse than a
                    # placeholder because it looks actionable. Removal would
                    # terminate, and is exactly what must not be printed: one
                    # of the two entries is the narrower, and deleting that
                    # one promotes the client.
                    #
                    # So point at the state and let the reason carry the fix.
                    # Which rule to keep is a decision only the user can make,
                    # and the file is the only place it can be made.
                    return "evoctx grant list"
                # **Resolve a glob tie by adding, never by removing.**
                # Suggesting a removal looked safe because the key was a
                # placeholder, and it was not: the reason lists exactly two
                # fillings and one of them deletes the narrower entry, so the
                # printed repair for "this client is denied" promoted it to
                # the wider grant. Reproduced end to end.
                #
                # An exact key wins rule 1 over every glob, so naming this
                # client concretely settles which grant applies without
                # touching either rule the user wrote — and the grant stays a
                # placeholder, so access can only widen if they choose it. A
                # three-way glob tie needs no special case: one exact key
                # outranks all of them at once.
                return f'evoctx grant activate <name> --client "{client}"'
            if key is not None:
                scope = f' --client "{key}"'
                name = entries[key].get("name")
            else:
                name = self._active_state().get("name")

            active, _ = self.active_grant(client)
            if active is not None and not active.grant.matches_client(client):
                # The grant is not the fix, whichever tier it came from: it
                # carries a `client` glob excluding the very client being
                # denied, so re-activating it rewrites the same exclusion.
                # Naming it produced a runnable command that provably changed
                # nothing — worse than a placeholder, because nothing marks it
                # as needing a decision. Removing the entry is not the fix
                # either; that is the widening above.
                return f"evoctx grant activate <name>{scope}"
        else:
            name = self._active_state().get("name")

        # **A grant is named only when re-activating THAT grant is the fix.**
        # One rule, both tiers, and the reason it is a rule rather than a
        # branch: five review rounds found the same defect five times, always
        # as some path that filled in a plausible name. Re-activating the
        # recorded grant cannot widen — it is the access the user already
        # chose, with its clock reset. Any *other* name is a guess, and the
        # convenient guess is always the store's broadest grant, so guessing
        # here means printing a widening as a repair. Where there is nothing
        # to re-activate there is nothing to name.
        if isinstance(name, str) and name in self.grants():
            return f"evoctx grant activate {name}{scope}"
        return f"evoctx grant activate <name>{scope}"

    def widen_command(self, client: str) -> str:
        """The command for a client whose grant *resolves* but refuses this —
        a tool it does not list, a project outside its scope.

        Distinct from `recovery_command` because the two answer different
        questions and only one of them can name a grant. There, the recorded
        grant is often exactly what should be re-activated; here it is by
        definition the grant that just said no, so the name has to be the
        user's choice. What this contributes is the **scope**: without
        `--client "<key>"` a narrowed client is told to run a machine-wide
        `grant activate`, which updates the scalar it does not resolve
        through and leaves it denied — on the branch that fires for every
        write the narrowed client attempts, which is the whole point of
        per-client grants.
        """
        key, tied = match_client_key(self.client_entries(), client)
        # Same rules as `recovery_command`, and the reason they are stated
        # twice is that this branch has now twice kept a defect after that
        # one lost it — first the removal, then the unrunnable exact-tie
        # command. Both times the property test found it, and only after it
        # stopped treating placeholders as out of scope.
        if _tie_is_between_exact_keys(tied, client) and tied:
            return "evoctx grant list"
        scope = f' --client "{client if tied else key}"' if (tied or key) else ""
        return f"evoctx grant activate <name>{scope}"

    # `_default_grant_name` used to live here and fell back to `everything`
    # whenever the recorded name was null, missing or deleted — which is
    # exactly what `deactivate` writes. So pulling the kill switch on a
    # narrow machine-wide grant made both `grant list` and `doctor` answer
    # "run: evoctx grant activate everything": the broadest grant in the
    # store, recommended by name, as the repair for a deliberate lockdown.
    # The rule above replaces it: name the recorded grant or name nothing.

    def active_grant(self, client: str | None = None) -> tuple[ActiveGrant | None, str]:
        """Returns (active, reason-if-none). `active` is None whenever there
        is nothing usable — unset, deleted from grants.yaml, or expired — and
        `reason` says which. The reason is **factual only**; the command that
        fixes it comes from `recovery_command`, so a caller that wants both
        composes them and no caller ends up printing the command twice.

        **Pass the client whenever you know it.** `client=None` answers for
        the machine-wide default and is for display only. The rule is not a
        list of call sites — an enumeration goes stale, and one already
        shipped incomplete — it is: if the caller knows which client it is
        acting for, it says so. `describe()` in particular has the client and
        must pass it, or `list_grants()` answers *"may I call X?"* from the
        machine default while that client's actual calls resolve against a
        narrower entry: a wrong answer from the one tool whose whole job is
        that answer.

        **An expired per-client entry denies that client. It never falls back
        to the scalar.** This is the one place fallback is actively dangerous:
        a `read-only` grant for cursor with an 8-hour expiry would, on falling
        back, silently promote cursor to the machine-wide `everything` at the
        moment its narrow grant lapsed. Narrowing fails closed. Fallback
        applies only where a client has *no* entry at all.
        """
        state = self._active_state()
        entries = self.client_entries()
        key: str | None = None
        if client is not None and entries:
            key, tied = match_client_key(entries, client)
            if tied:
                # The grant behind each key, not just the keys. The user has
                # to pick one to remove and the removal is not recoverable —
                # naming only the keys leaves them a coin flip between the
                # narrow entry and the wide one.
                pairs = ", ".join(
                    f"{k!r} -> {entries[k].get('name') or '(none)'}" for k in tied)
                # The grants are named, not just the keys, because whichever
                # way this is resolved the user is choosing between them —
                # and for the rule-1 case they are choosing by hand, so they
                # need to see what each one grants before deleting a line.
                # (An earlier comment here said "the user has to pick one to
                # remove", which stopped being true for the glob tie.)
                how = (f"delete one of them from {self.active_path} — they are the "
                       f"same key written twice, so no command can pick between them"
                       if _tie_is_between_exact_keys(tied, client) else
                       "give this client an entry of its own, which outranks both")
                return None, (
                    f"client '{client}' is covered by {len(tied)} per-client "
                    f"grants that are equally specific ({pairs}), so which one "
                    f"applies is undefined — {how}"
                )

        if key is not None:
            source = f"the grant for client key '{key}'"
            name = entries[key].get("name")
            activated_raw = entries[key].get("activated_at")
        else:
            source = "the active grant"
            name = state.get("name")
            activated_raw = state.get("activated_at")

        if not name:
            return None, (f"no grant is active for client key '{key}'" if key
                          else "no grant is active")
        if not isinstance(name, str):
            # The parse guard above proves this file is a JSON object; it
            # proves nothing about the values inside it. A merge or a hand
            # edit leaving `"name": ["everything"]` made `grants().get(name)`
            # raise `TypeError: unhashable type` — out of every tool call, and
            # out of `doctor` and `grant list`, the two commands someone runs
            # to find out what is wrong.
            return None, (f"{source} has a grant name that is not text, in "
                          f"{self.active_path}")
        grant = self.grants().get(name)
        if grant is None:
            return None, f"{source}, '{name}', no longer exists in grants.yaml"
        # Hand-editable file, and the per-client entries are new, so a missing
        # or malformed timestamp is a live possibility rather than a
        # theoretical one. It used to be a bare subscript, which fails every
        # tool call with a KeyError traceback instead of an explanation.
        try:
            # str() rather than a type guard: a missing key and a key holding
            # a number are the same problem to the caller, and both end as a
            # ValueError here instead of a TypeError escaping to a tool call.
            activated_at = datetime.fromisoformat(str(activated_raw))
        except (TypeError, ValueError):
            return None, (f"{source}, '{name}', has no readable activation time "
                          f"in {self.active_path}")
        expires_at = grant.expires_at(activated_at)
        if datetime.now(timezone.utc) >= expires_at:
            return None, (f"grant '{name}' expired {expires_at.isoformat()}"
                          f"{f' for client key {key!r}' if key else ''}")
        return ActiveGrant(grant, expires_at, key), ""

    def entry_usable(self, key: str) -> tuple[bool, str]:
        """Is the entry under `key` usable right now, judged on its **own**
        activation time?

        Separate from `active_grant` because the caller that needs it is not
        acting *for* a client — `evoctx grant activate --client` knows a key,
        which may itself be a glob, so it cannot ask "what does client X
        resolve to". Reusing the client path there would silently check the
        machine-wide scalar instead, and report success for an activation
        that denies every call: exactly the regression the post-activation
        check exists to prevent, one path over.

        "Usable" includes **the grant being able to cover this key at all**,
        not only its expiry. A grant carries its own `client` glob, and
        pointing key `claude*` at a grant scoped `client: "cursor*"` produces
        an entry that resolves fine and then denies every call — the entry
        wins for claude-code, and the grant refuses it. Judged here rather
        than left to `PolicyEngine.check` because every surface that asks
        "is this healthy" asks this function: without it the CLI exits 0,
        `grant list` prints an expiry, and `doctor` passes, on a client that
        can never make a call.

        Only for a **concrete** key. For a glob key both sides are patterns
        and the intersection is undecidable here — see `activate`'s guard,
        which declines the same comparison for the same reason.
        """
        entry = self.client_entries().get(key)
        if entry is None:
            return False, f"no per-client grant under key '{key}'"
        name = entry.get("name")
        if name and not isinstance(name, str):
            return False, "the recorded grant name is not text"
        grant = self.grants().get(name) if name else None
        if grant is None:
            return False, (f"'{name}' is not a grant in grants.yaml" if name
                           else "no grant name recorded")
        if client_key_is_concrete(key) and not grant.matches_client(key):
            return False, (f"grant '{name}' covers client '{grant.client}', "
                           f"not '{key}' — every call would be denied")
        try:
            activated_at = datetime.fromisoformat(str(entry.get("activated_at")))
        except (TypeError, ValueError):
            return False, "no readable activation time"
        expires_at = grant.expires_at(activated_at)
        if datetime.now(timezone.utc) >= expires_at:
            return False, f"grant '{name}' expired {expires_at.isoformat()}"
        return True, expires_at.isoformat()

    def shadowing_keys(
        self, new_key: str
    ) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str]]]:
        """Existing expired keys that could take precedence over `new_key`,
        as `(blocking, warning)`.

        Writing a usable entry does not make the client usable, because an
        existing key can outrank it. Two tiers can shadow and only one is
        cheaply decidable, so the contract is scoped honestly rather than
        claimed closed:

        - **`new_key` concrete → nothing shadows it.** An exact key wins rule
          1 for that client, and it is the one just written. Both lists empty,
          and the caller's exit code means what it says.
        - **`new_key` a glob → best effort.** Rule-1 shadowing is decidable:
          an existing *wildcard-free* key that this glob covers beats it for
          that one client, so every such key is **blocking — usable or not**.
          Skipping the usable ones was the inversion: an expired shadow only
          denies a client, while a **usable** one leaves them on whatever
          access that entry already grants. So `activate readonly --client
          "cursor*"` over an existing `cursor -> everything` reported nothing,
          exited 0, and left client `cursor` with every tool — the one
          outcome where the user believes they narrowed something and did
          not. Rule-2 shadowing is not decidable: an existing glob can
          outscore the new one —
          expired `cursor-*` (7) beats a new `cur*` (3) for client `cursor-1`
          — and deciding that in general is glob-vs-glob intersection, the
          comparison this module declines everywhere for the same reason. So
          over-approximate: **warn** on any expired glob key scoring at least
          as high as the new one, accept the false positives (`vscode*` warns
          about `cursor*` harmlessly), and document the residual instead of
          pretending there is none.

        **The residual, stated plainly:** two *usable* glob keys that tie on
        specificity make every client they both match ambiguous, and that is
        not reported here either — same undecidable comparison. `doctor`
        catches it where it can be decided, against the concrete names of the
        clients actually installed.
        """
        if client_key_is_concrete(new_key):
            return [], []
        blocking, warning = [], []
        folded = new_key.casefold()
        score = client_key_specificity(new_key)
        entries = self.client_entries()
        for key in sorted(entries):
            if key == new_key:
                continue
            usable, detail = self.entry_usable(key)
            if client_key_is_concrete(key):
                if fnmatchcase(key.casefold(), folded):
                    # The reason travels with the key, and so does whether it
                    # is a fault. A *usable* shadow is the documented
                    # precedence rule working — an override beside a family
                    # glob — so the caller warns. An *unusable* one leaves its
                    # client denied with the new entry unable to reach them,
                    # which is a broken store and the caller fails on it.
                    # Folding both into one list made the second exit 0.
                    blocking.append((key, detail if not usable else
                                     f"still resolves to grant "
                                     f"'{entries[key].get('name')}'", usable))
            else:
                spec = client_key_specificity(key)
                if spec == score:
                    # A tie, and this is the one rule-2 case that needs no
                    # glob intersection to be worth saying: *if* any client
                    # matches both, resolution refuses and that client is
                    # denied outright — whether this key is usable or not.
                    # Filtering on `not usable` here repeated one tier down
                    # the exact inversion this function was fixed for: it
                    # warned about the expired key, which merely denies, and
                    # said nothing about the usable one, which denies just as
                    # hard and looks healthy in every listing.
                    warning.append((key, f"ties with '{new_key}' on specificity, "
                                         f"so any client matching both is denied"))
                elif not usable and spec > score:
                    # More specific and cannot serve: it wins for the clients
                    # it covers and leaves them denied. More specific and
                    # usable is left alone — that is an override beside a
                    # family default, the precedence rule this ships.
                    warning.append((key, detail))
        return blocking, warning

    def denied_clients(self, names) -> list[tuple[str, str]]:
        """Of these concrete client names, the ones that cannot make a call.

        Resolution run for real, which is what turns the undecidable
        questions into lookups: whether two globs can both match *something*
        is expensive, whether they both match `claude-code` is a dict walk.
        So this catches an ambiguous pair, an entry pointed at a grant whose
        own `client` glob excludes the client, and an expiry, without any of
        them needing its own detector.

        One definition because two callers need the same answer — `doctor`,
        and `grant activate --client`, whose exit code claims the client can
        use what was just written.
        """
        out = []
        for name in sorted(set(names)):
            active, reason = self.active_grant(name)
            if active is None:
                out.append((name, reason))
            elif not active.grant.matches_client(name):
                out.append((name, f"grant '{active.grant.name}' covers "
                                  f"'{active.grant.client}', not '{name}'"))
        return out
