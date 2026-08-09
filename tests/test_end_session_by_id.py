"""Closing the session you actually opened — 0.1.5.

Before this, `end_session(summary)` closed whatever session the *connection*
had opened, and the Stop hook nagged about whichever session was newest
anywhere on the machine. Two assistant windows sharing one store is the
ordinary case, and the failure it produces is not a missing summary but a
wrong one: a summary of work the closed session never did.
"""
import importlib

import pytest

from evoctx.db import Database
from evoctx.grants import GrantStore
from evoctx.policy import PolicyEngine


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_STORE", str(tmp_path))
    monkeypatch.setenv("CONTEXT_CLIENT", "pytest")
    import evoctx.server as server_mod
    importlib.reload(server_mod)   # rebind module-level db to the temp store
    return server_mod


def engine(db, tmp_path):
    # `start_session` refuses a project name that does not exist, and this
    # tmp_path resolves to no workspace, so every session below has to name a
    # real one.
    db.upsert_project("p1")
    return PolicyEngine(db, GrantStore(tmp_path))


# ── db.open_sessions ──────────────────────────────────────────────────────────

def test_open_sessions_lists_only_unclosed_and_excludes_the_named_one(db):
    mine = db.start_session(project="a", intent="mine")
    theirs = db.start_session(project="b", intent="theirs")
    closed = db.start_session(project="c")
    db.end_session(closed, "done")

    others = db.open_sessions(exclude_id=mine)
    assert [s["id"] for s in others] == [theirs]


def test_open_sessions_with_no_exclusion_lists_everything_open(db):
    """`exclude_id=None` must not filter to nothing. `id != NULL` is NULL in
    SQL, so the naive comparison returns an empty list rather than every row."""
    a = db.start_session(project="a")
    b = db.start_session(project="b")
    assert {s["id"] for s in db.open_sessions()} == {a, b}


def test_open_sessions_is_newest_first_and_capped(db):
    ids = [db.start_session(project="p") for _ in range(7)]
    others = db.open_sessions(exclude_id=ids[0], limit=3)
    assert [s["id"] for s in others] == list(reversed(ids))[:3]


# ── policy: the warning ───────────────────────────────────────────────────────

def test_closing_one_session_warns_about_the_others(db, tmp_path):
    e = engine(db, tmp_path)
    mine = e.t_start_session("pytest", "p1", "mine")["id"]
    other = db.start_session(project="p1", intent="another window")

    out = e.t_end_session("pytest", mine, "did the thing")
    assert out["id"] == mine
    assert out["summary"] == "did the thing"
    assert [s["id"] for s in out["other_open_sessions"]] == [other]
    assert other[:8] in out["warning"]


def test_the_warning_is_absent_when_nothing_else_is_open(db, tmp_path):
    """A key that appears only sometimes is a signal; one that is always there
    with an empty list gets skimmed past."""
    e = engine(db, tmp_path)
    mine = e.t_start_session("pytest", "p1", "mine")["id"]
    out = e.t_end_session("pytest", mine, "done")
    assert "other_open_sessions" not in out
    assert "warning" not in out


def test_the_warning_carries_full_ids_not_just_prefixes(db, tmp_path):
    """The prose shows 8 characters because a full uuid is unreadable; the
    structured field must carry the whole id, or the only way to act on the
    warning is to paste a prefix and hope."""
    e = engine(db, tmp_path)
    mine = e.t_start_session("pytest", "p1", "mine")["id"]
    other = db.start_session(project="p1", intent="another window")
    out = e.t_end_session("pytest", mine, "done")
    assert out["other_open_sessions"][0]["id"] == other


def test_the_warning_states_the_real_count_not_the_length_of_a_capped_list(db, tmp_path):
    """`open_sessions` is capped for display. Reporting the length of that cap
    as "N other sessions are still open" states a false number with the
    confidence of a count."""
    e = engine(db, tmp_path)
    mine = e.t_start_session("pytest", "p1", "mine")["id"]
    for i in range(9):
        db.start_session(project="p1", intent=f"window {i}")

    out = e.t_end_session("pytest", mine, "done")
    assert "9 other sessions are still open" in out["warning"]
    # The prose names three and counts the rest off the REAL total, not off the
    # length of the list it is naming them from.
    assert "+6 more" in out["warning"]
    assert len(out["other_open_sessions"]) == 5, "the structured list is the capped one"


def test_an_out_of_scope_open_session_is_never_named(db, tmp_path):
    """The planted offender for `_visible_open_sessions`. Its docstring calls
    an unfiltered warning "a leak dressed as helpfulness"; without this, the
    filter could be deleted and every test would still pass."""
    import os
    import time

    db.upsert_project("p1")
    db.upsert_project("secret")
    engine_ = PolicyEngine(db, GrantStore(tmp_path))
    path = tmp_path / "grants.yaml"
    path.write_text(
        "grants:\n"
        "  - name: only-p1\n"
        '    client: "*"\n'
        "    tools: [start_session, end_session, recent_sessions]\n"
        "    scope:\n"
        '      projects: ["p1"]\n'
        '    expires: "+1d"\n',
        encoding="utf-8",
    )
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))
    engine_.grants.activate("only-p1")

    mine = engine_.t_start_session("pytest", "p1", "mine")["id"]
    hidden = db.start_session(project="secret", intent="not yours to see")

    out = engine_.t_end_session("pytest", mine, "done")
    assert "other_open_sessions" not in out, "a session outside the grant was named"
    assert hidden[:8] not in out.get("warning", "")


def test_a_no_op_close_is_audited_as_a_read_not_a_write(db, tmp_path):
    """`_guard` established that the log must distinguish what happened. A call
    that wrote nothing, recorded as a write, is the same lie as a denial
    recorded as a success."""
    from evoctx.audit import AuditWriter

    db.upsert_project("p1")
    e = PolicyEngine(db, GrantStore(tmp_path),
                     audit=AuditWriter(db.conn, "test-connection"))
    sid = e.t_start_session("pytest", "p1", "mine")["id"]
    e.t_end_session("pytest", sid, "the real summary")
    e.t_end_session("pytest", sid, "a second summary")

    actions = [r[0] for r in db.conn.execute(
        "SELECT action FROM audit_log WHERE tool = 'end_session' ORDER BY rowid")]
    assert actions == ["write", "read"]


def test_the_warning_does_not_tell_the_caller_to_close_them(db, tmp_path):
    """Phrasing is load-bearing. An assistant reading "close it" will close a
    session another window is working in, and write a summary of work that
    session never did — strictly worse than leaving it open.

    Asserted both ways round: the caution must be present AND no bare
    instruction to close may be, or a rewrite that adds "close it too" while
    keeping the caution still passes.
    """
    e = engine(db, tmp_path)
    mine = e.t_start_session("pytest", "p1", "mine")["id"]
    db.start_session(project="p1", intent="another window")
    warning = e.t_end_session("pytest", mine, "done")["warning"]

    assert "do not close it on a guess" in warning
    for imperative in ("close it too", "close them", "you should close",
                       "close the other"):
        assert imperative not in warning.lower(), f"the warning says {imperative!r}"


def test_the_warning_names_which_client_opened_each_session(db, tmp_path):
    """The one fact that helps a reader decide whose session it is. The
    warning deliberately refuses to decide for them, which only works if it
    hands over what the decision needs."""
    e = engine(db, tmp_path)
    mine = e.t_start_session("pytest", "p1", "mine")["id"]
    db.conn.execute(
        "INSERT INTO sessions (id, project, client, intent, started_at) VALUES "
        "('other-window-1', 'p1', 'cursor', 'their work', '2027-01-01T00:00:00+00:00')")
    db.conn.commit()

    out = e.t_end_session("pytest", mine, "done")
    assert "(cursor)" in out["warning"]
    assert out["other_open_sessions"][0]["client"] == "cursor"


# ── policy: closing by id ─────────────────────────────────────────────────────

def test_a_prefix_resolves_like_everywhere_else(db, tmp_path):
    e = engine(db, tmp_path)
    sid = e.t_start_session("pytest", "p1", "mine")["id"]
    out = e.t_end_session("pytest", sid[:8], "closed by prefix")
    assert out["id"] == sid
    assert out["open"] is False


def test_an_ambiguous_prefix_refuses_rather_than_picking(db, tmp_path):
    """Closing is not undoable, and there is no basis for choosing between two
    matches — the same rule `evoctx delete` follows."""
    e = engine(db, tmp_path)
    a = db.start_session(project="p1")
    db.conn.execute("UPDATE sessions SET id = ? WHERE id = ?", ("duplicate-1111", a))
    db.conn.execute(
        "INSERT INTO sessions (id, project, client, intent, started_at) "
        "VALUES ('duplicate-2222', 'p1', 'pytest', 'x', '2026-01-01T00:00:00+00:00')")
    db.conn.commit()

    out = e.t_end_session("pytest", "duplicate-", "summary")
    assert "error" in out
    # AmbiguousPrefix shows each candidate clipped to 12 characters
    assert "duplicate-11" in out["error"] and "duplicate-22" in out["error"]
    # and neither was touched
    assert db.get_session("duplicate-1111")["open"] is True
    assert db.get_session("duplicate-2222")["open"] is True


def test_an_ambiguous_prefix_does_not_name_sessions_outside_the_grant(db, tmp_path):
    """`_id_by_prefix` resolves across the whole table, so an ambiguity between
    a session this grant covers and one it does not would list both — and out
    of scope is supposed to read as nonexistent everywhere else in this file."""
    import os
    import time

    db.upsert_project("p1")
    db.upsert_project("secret")
    db.conn.executescript(
        "INSERT INTO sessions (id, project, client, intent, started_at) VALUES "
        "('sharedpfx-mine', 'p1', 'pytest', 'mine', '2027-01-01T00:00:00+00:00'),"
        "('sharedpfx-theirs', 'secret', 'pytest', 'theirs', '2027-01-01T00:00:00+00:00');"
    )
    db.conn.commit()

    engine = PolicyEngine(db, GrantStore(tmp_path))
    path = tmp_path / "grants.yaml"
    path.write_text(
        "grants:\n  - name: only-p1\n    client: \"*\"\n"
        "    tools: [end_session]\n    scope:\n      projects: [\"p1\"]\n"
        "    expires: \"+1d\"\n",
        encoding="utf-8",
    )
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))
    engine.grants.activate("only-p1")

    out = engine.t_end_session("pytest", "sharedpfx-", "summary")
    assert "sharedpfx-th" not in out["error"], "a session outside the grant was named"
    assert out["error"] == ("No session with that id is open within this "
                           "grant's scope.")
    assert db.get_session("sharedpfx-mine")["open"] is True
    assert db.get_session("sharedpfx-theirs")["open"] is True


def test_an_ambiguous_prefix_still_names_candidates_the_caller_may_read(db, tmp_path):
    """The guard must not swallow the useful message in the ordinary case —
    when every candidate is in scope, the ids are what makes the error
    actionable."""
    e = engine(db, tmp_path)
    db.conn.executescript(
        "INSERT INTO sessions (id, project, client, intent, started_at) VALUES "
        "('bothmine-one', 'p1', 'pytest', 'a', '2027-01-01T00:00:00+00:00'),"
        "('bothmine-two', 'p1', 'pytest', 'b', '2027-01-01T00:00:00+00:00');"
    )
    db.conn.commit()

    out = e.t_end_session("pytest", "bothmine-", "summary")
    assert "bothmine-one" in out["error"] and "bothmine-two" in out["error"]


def test_a_prefix_shorter_than_the_promised_minimum_is_refused(db, tmp_path):
    """The docstring promises "at least 8 characters". Promising a minimum and
    not enforcing it means a unique one-character prefix closes a session — and
    unlike `evoctx delete`, this path has no confirmation and no undo."""
    e = engine(db, tmp_path)
    sid = e.t_start_session("pytest", "p1", "mine")["id"]

    out = e.t_end_session("pytest", sid[:4], "summary")
    assert "too short" in out["error"]
    assert db.get_session(sid)["open"] is True, "a 4-char prefix closed a session"

    # the full id is never "too short", whatever its length
    assert "error" not in e.t_end_session("pytest", sid, "summary")


def test_closing_an_already_closed_session_says_so(db, tmp_path):
    """`db.end_session` only updates rows with `ended_at IS NULL`, so a second
    call silently keeps the first summary. Returning that untouched row without
    comment is how an assistant concludes its summary was saved when another
    one was."""
    e = engine(db, tmp_path)
    sid = e.t_start_session("pytest", "p1", "first")["id"]
    e.t_end_session("pytest", sid, "the real summary")

    out = e.t_end_session("pytest", sid, "a second, different summary")
    assert "error" in out
    assert "already closed" in out["error"]
    assert db.get_session(sid)["summary"] == "the real summary"


def test_an_unknown_id_is_refused_without_saying_whether_it_exists(db, tmp_path):
    e = engine(db, tmp_path)
    out = e.t_end_session("pytest", "no-such-session-id", "summary")
    # "within this grant's scope", not "for this client": closing a session
    # another client opened is the feature, and only project scope is checked.
    assert out == {"error": "No session with that id is open within this "
                            "grant's scope."}


# ── server surface ────────────────────────────────────────────────────────────

def test_server_closes_the_connection_session_when_no_id_is_given(server):
    server.db.upsert_project("p1")
    started = server.start_session(ctx=None, project="p1", intent="work")
    ended = server.end_session("done", ctx=None)
    assert ended["id"] == started["id"]
    assert ended["open"] is False


def test_server_closes_a_named_session_and_keeps_its_own_open(server):
    """The whole point of the parameter: closing somebody else's session must
    not make this connection think its own is closed. If it did, a later bare
    end_session() would answer "no session was started" while this
    connection's session is still open — which reads as "already closed" and
    is how a session ends up abandoned."""
    server.db.upsert_project("p1")
    mine = server.start_session(ctx=None, project="p1", intent="mine")
    other = server.db.start_session(project=None, intent="another window")

    closed = server.end_session("closing theirs", ctx=None, session_id=other)
    assert closed["id"] == other

    still_mine = server.end_session("closing mine", ctx=None)
    assert still_mine["id"] == mine["id"]
    assert still_mine["open"] is False


def test_server_clears_its_slot_when_the_id_names_its_own_session(server):
    """Passing your own id explicitly is the documented happy path, so it must
    behave exactly like omitting it — including forgetting the session."""
    server.db.upsert_project("p1")
    mine = server.start_session(ctx=None, project="p1", intent="mine")
    server.end_session("done", ctx=None, session_id=mine["id"])

    again = server.end_session("again", ctx=None)
    assert "No session was started on this connection" in again["error"]


def test_server_clears_its_slot_when_its_own_session_is_named_by_prefix(server):
    """The slot is compared against the id the policy layer actually closed,
    not against the argument — a prefix would never equal the stored uuid."""
    server.db.upsert_project("p1")
    mine = server.start_session(ctx=None, project="p1", intent="mine")
    server.end_session("done", ctx=None, session_id=mine["id"][:8])

    again = server.end_session("again", ctx=None)
    assert "No session was started on this connection" in again["error"]


def test_a_session_closed_out_of_band_does_not_strand_the_connection(server):
    """The regression this release nearly shipped.

    `db.end_session` only updates rows with `ended_at IS NULL`, so once the CLI
    or another window closes the session this connection opened, the connection
    holds an id it can never close. Left set, every later bare `end_session()`
    repeats the same error forever AND every `write_note()` attaches to a
    closed session — which is silent, and worse than the error.
    """
    server.db.upsert_project("p1")
    mine = server.start_session(ctx=None, project="p1", intent="mine")

    server.db.end_session(mine["id"], "closed from the CLI")   # out of band

    out = server.end_session("my summary", ctx=None)
    assert out["already_closed"] is True
    assert "already closed" in out["error"]
    assert server.db.get_session(mine["id"])["summary"] == "closed from the CLI"

    # the slot let go, so a note written now is NOT filed under the dead session
    note_id = server.write_note("after", "body", ctx=None, project="p1")
    assert server.db.get_note(note_id)["id"] == note_id
    assert [n["title"] for n in server.db.get_session(mine["id"])["notes"]] == []


def test_server_with_no_session_and_no_id_names_the_way_out(server):
    out = server.end_session("orphan summary", ctx=None)
    assert "recent_sessions()" in out["error"]
    assert "session_id" in out["error"]


def test_start_session_tells_the_caller_to_keep_the_id(server):
    """The parameter is useless if nothing tells the assistant to record the
    id — the docstring is the only channel that reaches it at call time."""
    doc = server.start_session.__doc__ or ""
    assert "Record the `id`" in doc


def test_end_session_is_still_one_tool(server):
    """A new tool would be denied to every existing user, whose grant
    enumerates the tools that existed when it was written. A new *parameter*
    is not something grants gate — which is why this shipped as one."""
    import asyncio
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "end_session" in names
    assert not {n for n in names if "end_session" in n and n != "end_session"}


# ── CLI parity ────────────────────────────────────────────────────────────────

def test_cli_end_session_reports_the_sessions_it_did_not_close(cli, monkeypatch, capsys):
    from conftest import run
    mine = cli.db.start_session(project=None, intent="mine")
    other = cli.db.start_session(project=None, intent="another window")

    run(cli, monkeypatch, "end-session", "done", "--id", mine[:8])
    out = capsys.readouterr().out
    assert f"Closed session {mine[:8]}" in out
    assert f"still open: {other[:8]}" in out


def test_cli_end_session_stays_quiet_when_nothing_else_is_open(cli, monkeypatch, capsys):
    from conftest import run
    mine = cli.db.start_session(project=None, intent="mine")
    run(cli, monkeypatch, "end-session", "done", "--id", mine[:8])
    assert "still open" not in capsys.readouterr().out


# ── the store the hook reads ──────────────────────────────────────────────────

def test_stop_hook_prints_the_full_id_and_the_exact_call(tmp_path, monkeypatch, capsys):
    """The hook is a separate process with no idea which window opened what,
    so the one thing it can do is hand over an id precise enough to act on and
    say plainly that it cannot attribute it."""
    monkeypatch.setenv("CONTEXT_STORE", str(tmp_path))
    import evoctx.cli as cli_mod
    importlib.reload(cli_mod)
    sid = cli_mod.db.start_session(project=None, intent="interrupted")

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    with pytest.raises(SystemExit) as exc:
        cli_mod.cmd_hook(type("A", (), {"event": "stop"})())
    assert exc.value.code == 2

    err = capsys.readouterr().err
    assert sid in err, "a prefix cannot be pasted into end_session"
    assert f'session_id="{sid}"' in err
    assert "cannot tell which window opened it" in err


def test_a_store_with_nothing_open_produces_no_stop_nag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTEXT_STORE", str(tmp_path))
    import evoctx.cli as cli_mod
    importlib.reload(cli_mod)
    sid = cli_mod.db.start_session(project=None, intent="finished")
    cli_mod.db.end_session(sid, "done")

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    cli_mod.cmd_hook(type("A", (), {"event": "stop"})())   # must not raise
    assert capsys.readouterr().err == ""


def test_open_sessions_returns_exactly_the_fields_the_warning_formats(db):
    """Guards the shape `open_sessions` returns — the warning formats these
    fields directly, and a missing key there is a KeyError inside a call that
    has already committed a close."""
    db.start_session(project="p", intent="x", client="c")
    other = Database.open_sessions(db)[0]
    assert set(other) == {"id", "project", "client", "intent", "started_at"}
