"""Assistant-side registration, and the grant upgrade it cannot ship without.

Adding an MCP tool denies it to every existing user: a grant enumerates the
tools that existed when it was written, so a name it does not contain is
refused. That is why `register_project` and `evoctx grant upgrade` are one
change and not two.
"""
import os
import time

import pytest
from conftest import run

from evoctx.grants import ALL_TOOLS, PRIOR_TOOL_SETS, TOOLS_WILDCARD, GrantStore
from evoctx.policy import PolicyDenied, PolicyEngine

OLD_GRANT = """\
grants:
  - name: everything
    client: "*"
    tools: [list_projects, get_project, search_context, get_by_id, write_note,
            start_session, end_session, recent_sessions, list_grants]
    scope: {projects: ["*"], exclude_tags: []}
    expires: "+30d"
"""

NARROW_GRANT = """\
grants:
  - name: payments-only
    client: "*"
    tools: [list_projects, get_project]
    scope:
      projects: ["payments"]
      exclude_tags: ["#personal"]
    expires: "+8h"
"""

# the 0.1.3 complete set, written as a block sequence — so it IS stale and the
# scanner is actually reached, rather than being skipped as user-narrowed
BLOCK_SEQUENCE_GRANT = """\
grants:
  - name: blocky
    client: "*"
    tools:
      - list_projects
      - get_project
      - search_context
      - get_by_id
      - write_note
      - start_session
      - end_session
      - recent_sessions
      - list_grants
    scope: {projects: ["*"], exclude_tags: []}
    expires: "+8h"
"""


def write_grants(tmp_path, body):
    path = tmp_path / "grants.yaml"
    path.write_text(body, encoding="utf-8")
    # mtime granularity is 1s on some filesystems, and `grants()` caches on it
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))


def store_with(tmp_path, body) -> GrantStore:
    """A store whose grants.yaml is `body`.

    Built in that order because `ensure_default_files()` activates
    `everything` when no active_grant.json exists, so a grants.yaml without
    that name cannot be the file a GrantStore is first constructed against.
    """
    store = GrantStore(tmp_path)          # writes the default pair
    write_grants(tmp_path, body)
    return store


# ── the lockout this phase exists for ─────────────────────────────────────────

def test_a_grant_written_before_the_tool_existed_denies_it(db, tmp_path):
    """Reproduce the constraint, so the fix has something to fix."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    write_grants(tmp_path, OLD_GRANT)
    engine.grants.activate("everything")

    with pytest.raises(PolicyDenied, match="does not allow register_project"):
        engine.check("claude-code", "register_project")


def test_the_denial_names_grant_upgrade_not_grant_activate(db, tmp_path):
    """Two different problems, two different fixes. "Activate a wider grant"
    sends the user looking for a grant that does not exist — the one they have
    simply predates the tool."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    write_grants(tmp_path, OLD_GRANT)
    engine.grants.activate("everything")

    with pytest.raises(PolicyDenied) as exc:
        engine.check("claude-code", "register_project")
    assert "evoctx grant upgrade" in str(exc.value)
    assert "grant activate" not in str(exc.value)


def test_an_unknown_tool_still_says_activate(db, tmp_path):
    """The upgrade advice must not swallow the other case: a tool this build
    does not ship is not something `grant upgrade` can add."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    write_grants(tmp_path, OLD_GRANT)
    engine.grants.activate("everything")

    with pytest.raises(PolicyDenied) as exc:
        engine.check("claude-code", "some_tool_from_the_future")
    assert "grant activate" in str(exc.value)
    assert "grant upgrade" not in str(exc.value)


def test_a_fresh_store_allows_the_new_tool_out_of_the_box(db, tmp_path):
    """The default is `"*"` from 0.1.4 on, so a new install does not have to
    run an upgrade before the tool it just installed works."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    assert engine.check("claude-code", "register_project").name == "everything"


# ── evoctx grant upgrade ──────────────────────────────────────────────────────

def test_upgrade_adds_only_the_missing_tools(tmp_path):
    write_grants(tmp_path, OLD_GRANT)
    store = GrantStore(tmp_path)

    report = store.upgrade_tools()
    assert report["updated"] == {"everything": ["register_project"]}
    assert store.grants()["everything"].allows_tool("register_project")


def test_upgrade_leaves_a_deliberately_narrow_grant_completely_alone(tmp_path):
    """The one that matters. A read-only payments grant is narrow on purpose,
    and "every tool this build ships that you did not name" cannot tell that
    from a grant a release left behind — so it would answer by adding
    `write_note` to it. The grant destroyed that way is the example this repo
    ships in its own default file.

    Asserting scope/expiry/client survived is NOT enough: they survive while
    the tool list is blown wide open. Assert the tools.
    """
    store = store_with(tmp_path, NARROW_GRANT)
    before = (tmp_path / "grants.yaml").read_text(encoding="utf-8")

    report = store.upgrade_tools()

    grant = store.grants()["payments-only"]
    assert grant.tools == ["list_projects", "get_project"]
    assert not grant.allows_tool("write_note")
    assert not grant.allows_tool("register_project")
    assert report["updated"] == {}
    assert report["manual"] == {}
    assert (tmp_path / "grants.yaml").read_text(encoding="utf-8") == before


def test_a_narrow_grant_is_not_reported_as_out_of_date(tmp_path):
    """Same rule from the other end: `doctor` must not fail a correct
    configuration, and the denial text must not claim a grant "predates" a
    tool it excludes on purpose."""
    store = store_with(tmp_path, NARROW_GRANT)
    assert store.missing_tools() == {}


def test_a_narrow_grants_denial_does_not_claim_it_is_out_of_date(db, tmp_path):
    engine = PolicyEngine(db, GrantStore(tmp_path))
    write_grants(tmp_path, NARROW_GRANT)
    engine.grants.activate("payments-only")

    with pytest.raises(PolicyDenied) as exc:
        engine.check("claude-code", "write_note")
    assert "grant upgrade" not in str(exc.value)
    assert "predates" not in str(exc.value)


def test_doctor_does_not_fail_a_deliberately_narrow_grant(tmp_path):
    """`doctor` exits non-zero on any FAIL, so reporting this would make the
    health check unsatisfiable without destroying the grant."""
    from evoctx.install import doctor

    store_with(tmp_path, NARROW_GRANT)
    checks = {c: (ok, detail) for c, ok, detail in doctor(tmp_path, tmp_path)}
    assert checks["grant tools"][0]


def test_upgrade_preserves_comments(tmp_path):
    """`grants.yaml` is documentation as much as configuration — the shipped
    file is more comment than data. A yaml round-trip would drop every one of
    them, so the upgrade edits text instead."""
    write_grants(tmp_path, "# keep me\n" + OLD_GRANT + "\n# and me\n")
    store = GrantStore(tmp_path)
    store.upgrade_tools()

    text = (tmp_path / "grants.yaml").read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "# and me" in text


def test_upgrade_backs_up_before_writing(tmp_path):
    write_grants(tmp_path, OLD_GRANT)
    store = GrantStore(tmp_path)
    report = store.upgrade_tools()

    assert report["backup"].exists()
    assert "register_project" not in report["backup"].read_text(encoding="utf-8")


def test_upgrade_is_a_no_op_on_a_wildcard_grant(tmp_path):
    store = GrantStore(tmp_path)          # ships the "*" default
    before = (tmp_path / "grants.yaml").read_text(encoding="utf-8")

    report = store.upgrade_tools()
    assert report["updated"] == {}
    assert (tmp_path / "grants.yaml").read_text(encoding="utf-8") == before


def test_upgrade_is_idempotent(tmp_path):
    write_grants(tmp_path, OLD_GRANT)
    store = GrantStore(tmp_path)
    store.upgrade_tools()
    after_first = (tmp_path / "grants.yaml").read_text(encoding="utf-8")

    assert store.upgrade_tools()["updated"] == {}
    assert (tmp_path / "grants.yaml").read_text(encoding="utf-8") == after_first


def test_upgrade_refuses_to_rewrite_a_block_sequence(tmp_path):
    """No `]` to insert before, and guessing at indentation to synthesise one
    is how a config file that gates every tool call gets corrupted. Report it
    with the names to add instead."""
    store = store_with(tmp_path, BLOCK_SEQUENCE_GRANT)
    before = (tmp_path / "grants.yaml").read_text(encoding="utf-8")

    report = store.upgrade_tools()
    assert report["updated"] == {}
    assert "register_project" in report["manual"]["blocky"]
    assert (tmp_path / "grants.yaml").read_text(encoding="utf-8") == before


def test_upgrade_dry_run_writes_nothing(tmp_path):
    write_grants(tmp_path, OLD_GRANT)
    store = GrantStore(tmp_path)
    before = (tmp_path / "grants.yaml").read_text(encoding="utf-8")

    report = store.upgrade_tools(dry_run=True)
    assert report["updated"] == {"everything": ["register_project"]}
    assert (tmp_path / "grants.yaml").read_text(encoding="utf-8") == before
    assert report["backup"] is None


def test_upgrade_edits_only_the_named_grants_tools(tmp_path):
    """Two grants, one already current. The scan is bounded by the next
    `- name:`, so the wrong grant's sequence cannot be extended."""
    write_grants(tmp_path, OLD_GRANT + """\
  - name: current
    client: "*"
    tools: ["*"]
    scope: {projects: ["*"], exclude_tags: []}
    expires: "+8h"
""")
    store = GrantStore(tmp_path)
    store.upgrade_tools()

    text = (tmp_path / "grants.yaml").read_text(encoding="utf-8")
    assert text.count("register_project") == 1
    assert 'tools: ["*"]' in text


def test_the_span_scan_never_reaches_into_the_next_grant(tmp_path):
    """A grant with no `tools:` key of its own parses fine — `tools` is
    optional (`raw.get("tools") or []`). Scanning forward from it without a
    bound finds the NEXT grant's `tools:` and would insert there: one grant's
    gap silently widening a different grant's permissions.

    Pinned against the scanner directly. Under the upgrade rule this input
    cannot arrive — an empty tool list is not any release's complete set — so
    a scenario test would assert nothing, while the scanner's contract still
    has to hold for whatever calls it next.
    """
    from evoctx.grants import _tools_sequence_span

    text = """grants:
  - name: notools
    client: "*"
    expires: "+8h"
  - name: current
    client: "*"
    tools: ["*"]
    expires: "+8h"
"""
    assert _tools_sequence_span(text, "notools") is None
    assert _tools_sequence_span(text, "current") == text.index('["*"]') + 4


def test_an_edit_that_would_not_parse_never_reaches_disk(tmp_path):
    """A trailing comma is legal YAML — `[a, b,]` loads as two items — so a
    grant written that way IS the previous release's complete set and does get
    upgraded. Inserting before its `]` then yields `[..., list_grants,,
    register_project]`, and `yaml.safe_load` rejects the double comma.

    Validating after the write and rolling back does not save this: the parse
    raises `yaml.YAMLError`, not `GrantError`, so a rollback guarded on
    `GrantError` never runs and a corrupt file — one that denies EVERY tool
    call — stays on disk. Check the candidate in memory instead; then nothing
    bad ever reaches the file.
    """
    store = store_with(tmp_path, OLD_GRANT.replace("list_grants]", "list_grants,]"))
    before = (tmp_path / "grants.yaml").read_text(encoding="utf-8")

    report = store.upgrade_tools()

    assert (tmp_path / "grants.yaml").read_text(encoding="utf-8") == before
    assert report["updated"] == {}
    assert "everything" in report["manual"]
    # and the store still answers — the real damage of a corrupt grants.yaml
    assert store.grants()["everything"].allows_tool("get_project")


def test_names_inserted_into_a_comment_are_not_reported_as_added(tmp_path):
    """A `]` inside a `#` comment inside the sequence ends the bracket scan
    there, so the insert lands in the comment: valid YAML, parses clean,
    grants nothing. Reporting "added" would be a lie the post-write parse
    cannot catch, and every re-run would append another copy."""
    tricky = OLD_GRANT.replace(
        "tools: [list_projects, get_project, search_context, get_by_id, write_note,\n"
        "            start_session, end_session, recent_sessions, list_grants]",
        "tools: [list_projects, get_project, search_context, get_by_id, write_note,\n"
        "            start_session, end_session, recent_sessions,   # note] here\n"
        "            list_grants]")
    store = store_with(tmp_path, tricky)

    report = store.upgrade_tools()

    assert report["updated"] == {}
    assert "everything" in report["manual"]
    assert not store.grants()["everything"].allows_tool("register_project")


def test_upgrade_preserves_line_endings_and_the_backup_is_a_byte_copy(tmp_path):
    """`read_text`/`write_text` translate newlines, so an LF file on Windows
    comes back CRLF — every line rewritten, and a `.bak` that is not a copy of
    what was there. "Leaving every other byte untouched" has to be literally
    true of a file people keep in git."""
    path = tmp_path / "grants.yaml"
    GrantStore(tmp_path)
    raw = OLD_GRANT.replace("\r\n", "\n").encode("utf-8")
    path.write_bytes(raw)
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))
    store = GrantStore(tmp_path)

    report = store.upgrade_tools()

    after = path.read_bytes()
    assert b"\r\n" not in after
    assert report["backup"].read_bytes() == raw


def test_missing_tools_reports_the_gap_without_writing(tmp_path):
    store = store_with(tmp_path, OLD_GRANT)
    before = (tmp_path / "grants.yaml").read_text(encoding="utf-8")

    assert store.missing_tools() == {"everything": ["register_project"]}
    assert (tmp_path / "grants.yaml").read_text(encoding="utf-8") == before


def test_upgrade_cli_reports_what_it_changed(cli, monkeypatch, capsys, tmp_path):
    write_grants(tmp_path, OLD_GRANT)
    run(cli, monkeypatch, "grant", "upgrade")
    out = capsys.readouterr().out
    assert "register_project" in out
    assert "everything" in out


# ── register_project ──────────────────────────────────────────────────────────

def test_register_project_binds_the_servers_own_directory(db, tmp_path, monkeypatch):
    """The assistant supplies the description; the server supplies the
    location. There is no path argument, and that is the point — a path
    parameter puts the one fact the caller cannot verify back under the
    caller's control."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = engine.t_register_project("claude-code", "myrepo", "what it is")
    from evoctx.db import normalize_root
    assert result["root"] == normalize_root(workdir)
    assert db.project_for_root(workdir) == "myrepo"
    # not merely "some directory got bound" — the parent is a plausible wrong
    # answer and a real one, since a client can launch the server anywhere
    assert db.project_for_root(workdir.parent) is None


def test_register_project_records_the_assistant_as_author(db, tmp_path, monkeypatch):
    engine = PolicyEngine(db, GrantStore(tmp_path))
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    engine.t_register_project("claude-code", "myrepo", "what it is")
    assert db.get_project("myrepo")["description_author"] == "claude-code"


def test_confirmation_does_not_launder_authorship(db, tmp_path, monkeypatch):
    """A human clicking through a proposal has reviewed it, not written it. If
    approval flipped the label, one rubber-stamped "looks good" would erase
    the provenance permanently."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    engine.t_register_project("claude-code", "myrepo", "text", confirmed_by_user=True)
    project = db.get_project("myrepo")
    assert project["description_author"] == "claude-code"
    assert project["description_reviewed_at"] is not None


def test_an_unconfirmed_registration_is_not_marked_reviewed(db, tmp_path, monkeypatch):
    engine = PolicyEngine(db, GrantStore(tmp_path))
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    engine.t_register_project("claude-code", "myrepo", "text")
    assert db.get_project("myrepo")["description_reviewed_at"] is None


def test_linking_an_existing_project_keeps_its_description(db, tmp_path, monkeypatch):
    """The day-one case, not the edge case: every project created before
    directories existed has no root."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    db.upsert_project("myrepo", overview="written by a human", author="cli")
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    engine.t_register_project("claude-code", "myrepo", "assistant's version")
    project = db.get_project("myrepo")
    assert project["overview"] == "written by a human"
    assert project["description_author"] == "cli"


def test_confirming_a_link_does_not_stamp_someone_elses_text_as_reviewed(
        db, tmp_path, monkeypatch):
    """Linking writes no description, so a review recorded there would mark
    words this call never wrote — the laundering the column exists to prevent,
    reached from the other side."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    db.upsert_project("myrepo", overview="written by a human", author="cli")
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    engine.t_register_project("claude-code", "myrepo", "ignored", confirmed_by_user=True)
    assert db.get_project("myrepo")["description_reviewed_at"] is None


def test_register_project_refuses_a_directory_another_project_claims(
        db, tmp_path, monkeypatch):
    engine = PolicyEngine(db, GrantStore(tmp_path))
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    db.register_project("first", workdir)
    monkeypatch.chdir(workdir)

    result = engine.t_register_project("claude-code", "second", "text")
    assert "error" in result
    assert "first" in result["error"]
    assert db.get_project("second") is None


def test_register_project_is_scope_checked(db, tmp_path, monkeypatch):
    """A write to an out-of-scope project fails loudly. Creating a project is
    a write like any other."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    write_grants(tmp_path, NARROW_GRANT.replace(
        "tools: [list_projects, get_project]",
        "tools: [list_projects, get_project, register_project]"))
    engine.grants.activate("payments-only")
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    with pytest.raises(PolicyDenied, match="NOT saved"):
        engine.t_register_project("claude-code", "myrepo", "text")
    assert db.get_project("myrepo") is None


def test_doctor_flags_a_grant_that_predates_a_tool(tmp_path):
    """The client would otherwise discover it as a denial mid-task."""
    from evoctx.install import doctor

    store_with(tmp_path, OLD_GRANT)
    checks = {c: (ok, detail) for c, ok, detail in doctor(tmp_path, tmp_path)}
    ok, detail = checks["grant tools"]
    assert not ok
    assert "register_project" in detail
    assert "evoctx grant upgrade" in detail


def test_doctor_passes_on_a_wildcard_grant(tmp_path):
    from evoctx.install import doctor

    GrantStore(tmp_path)                  # ships the "*" default
    checks = {c: (ok, detail) for c, ok, detail in doctor(tmp_path, tmp_path)}
    assert checks["grant tools"][0]


def test_register_project_returns_the_scoped_read_not_the_raw_row(db, tmp_path, monkeypatch):
    """Linking an existing project is the documented main case, and returning
    `db.get_project` raw hands back an overview this grant redacts and notes
    its exclude_tags hide — content every other read path filters."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    write_grants(tmp_path, """\
grants:
  - name: redacting
    client: "*"
    tools: ["*"]
    scope:
      projects: ["*"]
      exclude_tags: ["#personal"]
    redactions:
      - pattern: "api_key=[A-Za-z0-9]+"
        replace: "[REDACTED_KEY]"
    expires: "+8h"
""")
    engine.grants.activate("redacting")
    db.upsert_project("myrepo", overview="deploy with api_key=SECRET123", author="cli")
    db.add_note("Private thing", "x", project="myrepo", tags=["#personal"])
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = engine.t_register_project("claude-code", "myrepo", "ignored")

    assert "SECRET123" not in result["overview"]
    assert result["recent_notes"] == []


def test_register_project_masks_the_text_it_stores(db, tmp_path, monkeypatch):
    """Inbound masking, like every other write that takes free text — the
    overview lands in the store, where a grant's own redaction patterns are
    the user's statement about what must never be written down."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    write_grants(tmp_path, """\
grants:
  - name: redacting
    client: "*"
    tools: ["*"]
    scope: {projects: ["*"], exclude_tags: []}
    redactions:
      - pattern: "api_key=[A-Za-z0-9]+"
        replace: "[REDACTED_KEY]"
    expires: "+8h"
""")
    engine.grants.activate("redacting")
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    engine.t_register_project("claude-code", "myrepo", "deploy with api_key=SECRET123")
    assert "SECRET123" not in db.get_project("myrepo")["overview"]


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_a_blank_project_name_is_refused(db, tmp_path, monkeypatch, bad):
    """Nothing downstream refuses it: `fnmatch("", "*")` is True so the scope
    check passes, and no layer validates the name. An empty project would take
    the directory binding and make every correct registration afterwards fail
    — unfixable from here, since deleting is human-only by design."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = engine.t_register_project("claude-code", bad, "text")
    assert "error" in result
    assert db.list_projects() == []
    assert db.project_for_root(workdir) is None


def test_register_project_documents_its_error_return(db, tmp_path, monkeypatch):
    """The docstring is the model's interface contract, and this tool is the
    only one here that returns an error dict — so there is no convention to
    fall back on. An assistant planning "register_project → start_session" with
    no documented failure shape proceeds against a project that was never
    created, and the record it writes is stranded.

    Pinned against the behaviour rather than asserted alone: the tool really
    does return the error shape, so the docstring really must say so.
    """
    engine = PolicyEngine(db, GrantStore(tmp_path))
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    db.register_project("first", workdir)

    refused = engine.t_register_project("claude-code", "second", "text")
    assert "error" in refused and "root" not in refused

    import evoctx.server as server_mod
    doc = server_mod.register_project.__doc__ or ""
    assert "error" in doc, (
        "register_project can return {'error': ...} but its docstring — the "
        "schema the model reads — does not say so"
    )


def test_the_unresolved_hint_offers_the_tool_when_the_grant_allows_it(
        db, tmp_path, monkeypatch):
    """The hint is the channel the model actually relays — it is the answer to
    the question just asked, where `instructions=` is background. A hint naming
    only the CLI sends the user out of the conversation to do what the
    assistant could have done, which is the pre-phase-3 dead end.

    Generated at runtime, so it is invisible to any amount of reading the
    docs — and it contradicted three static channels for nine review rounds.
    """
    engine = PolicyEngine(db, GrantStore(tmp_path))
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    hint = engine.t_list_projects("claude-code")["workspace"]["hint"]
    assert "register_project" in hint
    assert "myrepo" in hint
    assert "evoctx init" in hint          # the human option survives alongside


def test_the_unresolved_hint_names_only_the_cli_when_the_grant_denies_the_tool(
        db, tmp_path, monkeypatch):
    """Offering a call the caller will be denied is worse than offering the
    CLI, which always works."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    write_grants(tmp_path, OLD_GRANT)     # predates register_project
    engine.grants.activate("everything")
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    hint = engine.t_list_projects("claude-code")["workspace"]["hint"]
    assert "register_project" not in hint
    assert "evoctx init" in hint


def test_a_refused_context_project_still_points_at_the_cli(db, tmp_path, monkeypatch):
    """Rung 1's refusal is a config typo. Registering there would bind this
    directory to a name that came from a misconfigured env var — so the
    registration offer is scoped to rung 3, which `suggested_name` marks."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    workdir = tmp_path / "myrepo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("CONTEXT_PROJECT", "typoed-name")

    block = engine.t_list_projects("claude-code")["workspace"]
    assert block["project"] is None
    assert "register_project" not in block["hint"]
    assert "typoed-name" in block["hint"]


def test_prior_tool_sets_covers_the_previous_release():
    """The append to `PRIOR_TOOL_SETS` has to be enforced, not documented.

    A release that adds a tool to `ALL_TOOLS` and forgets the outgoing set does
    NOT degrade to the manual channel — the stale grant matches no prior set,
    so it is never considered. `doctor` then passes a grant that does not cover
    the build's tools, `grant upgrade` reports nothing missing while implying
    the user narrowed it themselves, and the denial sends them after a wider
    grant that does not exist: the exact lockout this release removes,
    reintroduced by an omission.

    Fails here, in CI, instead of on someone's machine.

    The literal below is the point. "Strict subset, non-empty difference" is
    the adjacent property and does NOT guard: a stale prior set stays a strict
    subset forever and the gap only widens, so a release that forgets sails
    through. Pinning the gap to exactly what THIS release adds means the only
    way to make it pass again is to append the outgoing set and update the
    literal — i.e. to touch the list.
    """
    assert set(ALL_TOOLS) - set(PRIOR_TOOL_SETS[0]) == {"register_project"}, (
        "ALL_TOOLS grew: append the outgoing complete set to PRIOR_TOOL_SETS "
        "and update this literal to the tools THIS release adds. Skipping it "
        "does not fail safe — the stale grant matches no prior set, so it is "
        "never considered, and doctor/upgrade/denial all report the opposite "
        "of the truth."
    )


def test_a_grant_holding_the_current_full_set_is_not_reported(tmp_path):
    """The other half of the same guarantee: the set `grant upgrade` itself
    produces must not come back as stale on the next run."""
    current = ", ".join(ALL_TOOLS)
    store = store_with(tmp_path, f"""\
grants:
  - name: everything
    client: "*"
    tools: [{current}]
    scope: {{projects: ["*"], exclude_tags: []}}
    expires: "+30d"
""")
    assert store.missing_tools() == {}
    assert store.upgrade_tools()["updated"] == {}


def test_one_unfixable_grant_does_not_veto_the_others(tmp_path):
    """A legal trailing comma is enough to break its own insert. Discarding
    every edit on that failure means one legacy quirk turns the whole upgrade
    into a no-op and tells the user to hand-edit grants that needed nothing."""
    nine = ("list_projects, get_project, search_context, get_by_id, write_note,\n"
            "            start_session, end_session, recent_sessions, list_grants")
    store = store_with(tmp_path, f"""\
grants:
  - name: alpha
    client: "*"
    tools: [{nine},]
    scope: {{projects: ["*"], exclude_tags: []}}
    expires: "+8h"
  - name: beta
    client: "*"
    tools: [{nine}]
    scope: {{projects: ["*"], exclude_tags: []}}
    expires: "+8h"
""")
    report = store.upgrade_tools()

    assert report["updated"] == {"beta": ["register_project"]}
    assert "alpha" in report["manual"]
    grants = store.grants()
    assert grants["beta"].allows_tool("register_project")
    assert not grants["alpha"].allows_tool("register_project")


def test_an_unhashable_tools_entry_elsewhere_does_not_raise(tmp_path):
    """`tools: ["*", [oops]]` loads fine — the wildcard short-circuits
    `_parse_grant`'s unknown-name check, so the nested list survives into
    `Grant.tools`. `set()` on it raises TypeError, which neither the validator
    nor the CLI catches: the user gets a traceback from an unrelated grant."""
    nine = ("list_projects, get_project, search_context, get_by_id, write_note,\n"
            "            start_session, end_session, recent_sessions, list_grants")
    store = store_with(tmp_path, f"""\
grants:
  - name: stale
    client: "*"
    tools: [{nine}]
    scope: {{projects: ["*"], exclude_tags: []}}
    expires: "+8h"
  - name: wild
    client: "*"
    tools: ["*", [oops]]
    scope: {{projects: ["*"], exclude_tags: []}}
    expires: "+8h"
""")
    report = store.upgrade_tools()          # must not raise

    assert report["updated"] == {"stale": ["register_project"]}
    assert store.grants()["stale"].allows_tool("register_project")


def test_all_tools_contains_register_project():
    """`_parse_grant` rejects a grants.yaml naming a tool not in this list, and
    a wildcard grant expands to it — so a tool missing here is uncallable."""
    assert "register_project" in ALL_TOOLS
    assert TOOLS_WILDCARD not in ALL_TOOLS
