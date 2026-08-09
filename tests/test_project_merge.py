"""Remediation — finding and fixing the phantoms that already exist.

Refusing invented names (step 4) stops new ones. It does nothing for the notes
and sessions already filed under one, which are invisible under the real
project and reachable only by searching for a name the user has no reason to
know.
"""
import pytest
from conftest import run

from evoctx.db import NoSuchProject


def phantom(db, name, notes=1, sessions=1):
    """Notes and sessions filed under a name that is not a project — written
    through the db layer, which is how they got there before step 4."""
    db.upsert_project("real")
    for i in range(notes):
        db.add_note(f"note {i}", "x", project=name)
    for _ in range(sessions):
        db.start_session(project=name, intent="stranded work")


# ── detection ─────────────────────────────────────────────────────────────────

def test_phantom_projects_counts_notes_and_sessions(db):
    """Sessions too: `start_session` accepted an invented name for exactly as
    long as `write_note` did, and one missed by the count is one the merge
    silently leaves behind."""
    phantom(db, "evobytes-mcp-server", notes=2, sessions=1)

    found = db.phantom_projects()
    assert found == {"evobytes-mcp-server":
                     {"notes": 2, "sessions": 1, "by_client": 0}}


def test_a_real_project_is_not_a_phantom(db):
    db.upsert_project("real")
    db.add_note("t", "x", project="real")
    db.start_session(project="real")
    assert db.phantom_projects() == {}


def test_global_and_project_less_notes_are_not_phantoms(db):
    """A null project is not a name that failed to resolve — it is no name."""
    db.add_note("global", "x", is_global=True)
    db.add_note("none", "x")
    db.start_session()
    assert db.phantom_projects() == {}


def test_doctor_fails_on_a_phantom_an_assistant_created(db):
    """The residue of the bug step 4 fixed — nobody chose it."""
    from evoctx.install import _check_phantom_projects

    db.upsert_project("real")
    db.add_note("t", "x", project="typo-name", author_client="claude-code")
    checks: list = []
    _check_phantom_projects(checks, db)
    name, ok, detail = checks[0]
    assert (name, ok) == ("phantom projects", False)
    assert "typo-name" in detail
    assert "evoctx project-merge" in detail


def test_doctor_does_not_fail_on_a_name_the_human_typed(db):
    """`evoctx add --project whatever` is documented as still working — this
    release's own CHANGELOG promises it. `doctor` exits non-zero on any FAIL,
    so failing here would make the health check permanently red for a state
    the product creates and blesses, which is the mistake the roots check was
    written to avoid."""
    from evoctx.install import _check_phantom_projects

    db.upsert_project("real")
    db.add_note("t", "x", project="scratchpad", author_client="cli")
    checks: list = []
    _check_phantom_projects(checks, db)
    name, ok, detail = checks[0]
    assert (name, ok) == ("phantom projects", True),         "a name the user typed themselves is a choice, not a fault"
    assert "scratchpad" in detail, "but it must still be visible"
    assert "evoctx project-merge" in detail


def test_doctor_does_not_fail_after_delete_project(db):
    """`delete-project` without --with-notes promises the notes are 'kept,
    unlinked'. Before 0.1.4 they kept pointing at the deleted name, so this
    check would have failed permanently on a state the CLI itself produced
    and printed a blessing for."""
    from evoctx.install import _check_phantom_projects

    db.upsert_project("gone")
    db.add_note("t", "x", project="gone", author_client="claude-code")
    db.start_session(project="gone")
    db.delete_project("gone")

    checks: list = []
    _check_phantom_projects(checks, db)
    assert checks[0][1] is True


def test_doctor_passes_when_everything_is_filed_properly(db):
    from evoctx.install import _check_phantom_projects

    db.upsert_project("real")
    db.add_note("t", "x", project="real")
    checks: list = []
    _check_phantom_projects(checks, db)
    assert checks[0][1] is True


@pytest.mark.parametrize("setup,why", [
    (lambda db, d: db.upsert_project("topic-only"), "a project with no directory"),
    (lambda db, d: db.register_project("bound", d), "every project bound"),
    (lambda db, d: None, "an empty store"),
])
def test_projects_without_roots_never_fails(db, tmp_path, setup, why):
    """Parametrized because the name claims *never*, and asserting one branch
    pins nothing: the all-bound and empty-store paths were both untested, so
    flipping either to False stayed green."""
    from evoctx.install import _check_projects_without_roots

    d = tmp_path / "repo"
    d.mkdir()
    setup(db, d)
    checks: list = []
    _check_projects_without_roots(checks, db)
    assert all(ok for _, ok, _ in checks), why


def test_projects_without_roots_names_them(db):
    from evoctx.install import _check_projects_without_roots

    db.upsert_project("topic-only")
    checks: list = []
    _check_projects_without_roots(checks, db)
    assert "topic-only" in checks[0][2]


def test_templates_are_not_reported_as_missing_a_directory(db):
    """Templates are `projects` rows prefixed with `_` by design and can never
    hold a root, so telling the user to run `init` in one is advice that
    cannot be followed."""
    from evoctx.install import _check_projects_without_roots

    db.upsert_project("_fastapi")
    checks: list = []
    _check_projects_without_roots(checks, db)
    assert checks == [], "a store of only templates has nothing to report"


# ── the merge ─────────────────────────────────────────────────────────────────

def test_merge_moves_notes_and_sessions_together(db):
    """The whole point. A session records what a stretch of work was for and
    its notes record what came out of it; moving one without the other leaves
    a session whose notes are elsewhere."""
    phantom(db, "wrong", notes=2, sessions=1)

    moved = db.merge_project("wrong", "real")
    assert moved == {"notes": 2, "sessions": 1, "project_removed": False}
    assert db.phantom_projects() == {}
    assert [n["project"] for n in db.list_notes()] == ["real", "real"]
    assert db.recent_sessions()[0]["project"] == "real"


def test_merge_works_when_the_wrong_name_was_never_a_project(db):
    """The normal case — a phantom is a name on notes that was never
    registered, so requiring it to exist would refuse the situation this
    exists for."""
    phantom(db, "never-registered")
    assert not db.project_exists("never-registered")

    moved = db.merge_project("never-registered", "real")
    assert moved["notes"] == 1
    assert moved["project_removed"] is False


def test_merging_a_real_project_removes_it_afterwards(db):
    db.upsert_project("real")
    db.upsert_project("duplicate")
    db.add_note("t", "x", project="duplicate")

    moved = db.merge_project("duplicate", "real")
    assert moved["project_removed"] is True
    assert db.get_project("duplicate") is None


def test_merge_refuses_a_destination_that_does_not_exist(db):
    """Merging into a second invented name only moves the problem."""
    phantom(db, "wrong")
    with pytest.raises(NoSuchProject, match="no project named 'also-invented'"):
        db.merge_project("wrong", "also-invented")
    assert db.phantom_projects() == {"wrong": {"notes": 1, "sessions": 1, "by_client": 0}}


def test_merge_refuses_a_project_into_itself(db):
    db.upsert_project("real")
    with pytest.raises(ValueError, match="into itself"):
        db.merge_project("real", "real")


def test_merge_does_not_transfer_directory_bindings(db, tmp_path):
    """A root is a claim about where code lives. Folding one project into
    another should not silently re-point that claim — the user re-makes it
    with `init` if they mean it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    db.upsert_project("real")
    db.register_project("duplicate", repo)

    db.merge_project("duplicate", "real")
    assert db.project_roots() == []
    assert db.project_for_root(repo) is None


def test_merge_is_atomic(db):
    """Notes moved but sessions left behind is the exact half-state the
    no-split rule exists to prevent, so a failure partway must roll back.

    Forced with a trigger rather than a patched method: `sqlite3.Connection`
    is C-level and its `execute` is read-only, and a trigger exercises the
    real rollback instead of a mock of it.
    """
    import sqlite3

    phantom(db, "wrong", notes=1, sessions=1)
    db.conn.execute("""
        CREATE TRIGGER halt_sessions BEFORE UPDATE ON sessions
        BEGIN SELECT RAISE(ABORT, 'interrupted'); END
    """)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            db.merge_project("wrong", "real")
    finally:
        db.conn.execute("DROP TRIGGER halt_sessions")

    assert db.phantom_projects() == {"wrong": {"notes": 1, "sessions": 1, "by_client": 0}}, \
        "the notes UPDATE must have rolled back with the sessions one"


# ── the CLI ───────────────────────────────────────────────────────────────────

def test_cli_merge_reports_what_it_moved(cli, monkeypatch, capsys):
    phantom(cli.db, "wrong", notes=2, sessions=1)
    run(cli, monkeypatch, "project-merge", "wrong", "real", "--force")
    out = capsys.readouterr().out
    assert "2 note(s)" in out and "1 session(s)" in out
    assert cli.db.phantom_projects() == {}


def test_cli_merge_asks_before_moving(cli, monkeypatch, capsys):
    phantom(cli.db, "wrong")
    monkeypatch.setattr("builtins.input", lambda: "n")
    run(cli, monkeypatch, "project-merge", "wrong", "real")
    assert "Cancelled" in capsys.readouterr().out
    assert cli.db.phantom_projects() == {"wrong": {"notes": 1, "sessions": 1, "by_client": 0}}


def test_cli_merge_says_so_when_there_is_nothing_to_move(cli, monkeypatch, capsys):
    cli.db.upsert_project("real")
    run(cli, monkeypatch, "project-merge", "nothing-here", "real", "--force")
    out = capsys.readouterr().out
    assert "Nothing is filed under" in out
    assert "evoctx doctor" in out


# ── delete-project must not widen what a scoped grant can read ────────────────

SCOPED_TO_KEEPER = """\
grants:
  - name: narrow
    client: "*"
    tools: ["*"]
    scope: {projects: ["keeper"], exclude_tags: []}
    expires: "+1d"
"""


def scoped_engine(db, tmp_path):
    import os
    import time

    from evoctx.grants import GrantStore
    from evoctx.policy import PolicyEngine

    engine = PolicyEngine(db, GrantStore(tmp_path))
    y = tmp_path / "grants.yaml"
    y.write_text(SCOPED_TO_KEEPER, encoding="utf-8")
    stamp = time.time() + 2
    os.utime(y, (stamp, stamp))
    engine.grants.activate("narrow")
    return engine


def test_deleting_a_project_does_not_expose_its_notes_to_a_scoped_grant(db, tmp_path):
    """`allows_project(None)` is True unconditionally, so unlinking a whole
    project's notes made every one of them readable by a grant scoped
    elsewhere — the same widening `check_write_global` refuses on the write
    side, reached as a side effect of a human running `delete-project`."""
    engine = scoped_engine(db, tmp_path)
    grant = engine.check("c", "search_context")
    db.upsert_project("keeper")
    db.upsert_project("doomed")
    db.add_note("secret", "confidential doomed material", project="doomed")

    assert engine.search(grant, "confidential", project=None, limit=10)["results"] == []
    db.delete_project("doomed")
    assert engine.search(grant, "confidential", project=None, limit=10)["results"] == [], \
        "unlinking must not move a note across a scope boundary"


def test_deleting_a_project_does_not_expose_its_sessions_to_a_scoped_grant(db, tmp_path):
    engine = scoped_engine(db, tmp_path)
    grant = engine.check("c", "recent_sessions")
    db.upsert_project("keeper")
    db.upsert_project("doomed")
    db.start_session(project="doomed", intent="doomed work")

    assert engine.recent_sessions(grant, None, 10) == []
    db.delete_project("doomed")
    assert engine.recent_sessions(grant, None, 10) == []


def test_a_global_note_stays_visible_to_a_scoped_grant(db, tmp_path):
    """The distinction that makes the fix coherent: "applies to every project"
    includes the scoped grant's project, so a global note is still in scope.
    Only a project-LESS note — which says nothing about where it belongs — is
    withheld."""
    engine = scoped_engine(db, tmp_path)
    grant = engine.check("c", "search_context")
    db.upsert_project("keeper")
    db.add_note("spans all", "findable everywhere", is_global=True)

    hits = engine.search(grant, "findable", project=None, limit=10)["results"]
    assert [h["title"] for h in hits] == ["spans all"]


def test_a_wide_grant_still_reads_project_less_notes(db, tmp_path):
    """The narrowing must not hide legacy notes from a grant that covers
    everything — that would be data loss by another name."""
    from evoctx.grants import GrantStore
    from evoctx.policy import PolicyEngine

    engine = PolicyEngine(db, GrantStore(tmp_path))      # default "*" grant
    grant = engine.check("c", "search_context")
    db.add_note("legacy", "written before 0.1.4", project=None)

    hits = engine.search(grant, "legacy", project=None, limit=10)["results"]
    assert [h["title"] for h in hits] == ["legacy"]


# ── --with-notes leaves no session behind ─────────────────────────────────────

def test_delete_project_with_notes_still_unlinks_sessions(db):
    """`--with-notes` deletes notes and never touched sessions, so branching
    the unlink left every session an assistant opened under the name pointing
    at a project that no longer exists — the same phantom, other branch."""
    db.upsert_project("doomed")
    db.add_note("n", "x", project="doomed")
    db.start_session(project="doomed", client="claude-code")

    db.delete_project("doomed", with_notes=True)
    assert db.phantom_projects() == {}


def test_end_session_is_scoped_like_every_other_read(db, tmp_path):
    """`end_session` had no scope check at all: a grant scoped to one project
    could close another project's session and read back its intent and its
    notes' titles, while `recent_sessions` refused to list it. `get_note`
    closes exactly that asymmetry for notes; sessions now match."""
    engine = scoped_engine(db, tmp_path)
    db.upsert_project("keeper")
    db.upsert_project("elsewhere")
    sid = db.start_session(project="elsewhere", intent="private intent")
    db.add_note("elsewhere note", "x", project="elsewhere", session_id=sid)

    result = engine.t_end_session("c", sid, "summary from an out-of-scope grant")
    assert "error" in result
    assert "private intent" not in str(result)
    assert "elsewhere note" not in str(result)
    assert db.get_session(sid)["ended_at"] is None, "and it must not have been closed"


def test_end_session_still_works_inside_scope(db, tmp_path):
    """The denial must not swallow the legitimate case."""
    engine = scoped_engine(db, tmp_path)
    db.upsert_project("keeper")
    sid = db.start_session(project="keeper", intent="mine")

    result = engine.t_end_session("c", sid, "done")
    assert result.get("summary") == "done"


def test_doctor_reports_notes_a_scoped_grant_can_no_longer_read(db):
    """0.1.4 narrowed project-less rows out of scoped grants, and migration
    005 does not backfill `is_global` — so nothing else would ever tell a user
    which of their notes stopped reaching a scoped client."""
    from evoctx.install import _check_unscoped_notes

    db.add_note("legacy", "written before 0.1.4")
    checks: list = []
    _check_unscoped_notes(checks, db)
    name, ok, detail = checks[0]
    assert (name, ok) == ("unscoped notes", True), "information, not a fault"
    assert "1 note(s)" in detail


def test_doctor_says_nothing_when_every_note_has_a_home(db):
    from evoctx.install import _check_unscoped_notes

    db.upsert_project("p")
    db.add_note("filed", "x", project="p")
    db.add_note("global", "x", is_global=True)
    checks: list = []
    _check_unscoped_notes(checks, db)
    assert checks == []


def test_delete_with_notes_does_not_offer_to_move_what_it_deletes(cli, monkeypatch, capsys):
    """`project-merge` moves the notes — offering it in the branch that
    destroys them is advice pointing the wrong way."""
    cli.db.upsert_project("p1")
    cli.db.add_note("n", "x", project="p1")
    cli.db.start_session(project="p1")

    run(cli, monkeypatch, "delete-project", "p1", "--with-notes", "--force")
    out = capsys.readouterr().out
    assert "project-merge" not in out
    assert "unlinked too" not in out, "nothing else was unlinked in this branch"
    assert "sessions are never deleted" in out


def test_a_denied_end_session_leaves_it_closable(tmp_path, monkeypatch):
    """The guard must not strand what it refuses to close. Clearing the
    server's slot on a denial leaves the session open with no summary — which
    the instructions tell every model means "interrupted, its notes are the
    only record" — and unclosable, since the retry has no id to use. That is
    worse than the state before the guard existed."""
    import importlib

    monkeypatch.setenv("CONTEXT_STORE", str(tmp_path))
    monkeypatch.setenv("CONTEXT_CLIENT", "pytest")
    import evoctx.server as srv
    importlib.reload(srv)

    calls = {"n": 0}

    def denied(*a, **k):
        calls["n"] += 1
        return {"error": "No session with that id is open for this client."}

    srv._current_session_id = "some-session-id"
    monkeypatch.setattr(srv.policy, "t_end_session", denied)

    first = srv.end_session("summary", ctx=None)
    assert "error" in first
    assert srv._current_session_id == "some-session-id", "the slot must survive a denial"

    second = srv.end_session("summary", ctx=None)
    assert second == first, "a retry must repeat the same denial, not a different one"
    assert calls["n"] == 2, "and must still reach the policy layer"


def test_a_denied_end_session_is_audited(db, tmp_path):
    """`t_get_by_id` logs its blocked reads; SECURITY.md calls the audit log
    the accountability record. An attempt to close a session outside scope is
    exactly what it should hold."""
    from evoctx.audit import AuditWriter
    from evoctx.grants import GrantStore
    from evoctx.policy import PolicyEngine
    import os
    import time

    engine = PolicyEngine(db, GrantStore(tmp_path),
                          audit=AuditWriter(db.conn, "test-connection"))
    y = tmp_path / "grants.yaml"
    y.write_text(SCOPED_TO_KEEPER, encoding="utf-8")
    stamp = time.time() + 2
    os.utime(y, (stamp, stamp))
    engine.grants.activate("narrow")

    db.upsert_project("keeper")
    db.upsert_project("elsewhere")
    sid = db.start_session(project="elsewhere", intent="private")

    engine.t_end_session("c", sid, "nope")
    logged = db.conn.execute(
        "SELECT tool, action, record_ids FROM audit_log "
        "WHERE tool = 'end_session'").fetchall()
    assert len(logged) == 1, "a denied attempt must leave a trace"
    assert logged[0][1] == "denied", (
        "`evoctx audit --action denied` is the query a user runs to ask what a "
        "client tried and could not do; a 'write' row is invisible to it and "
        "reads like a successful close"
    )
    assert sid not in (logged[0][2] or ""), \
        "but must not log the id the denial withholds from the caller"
