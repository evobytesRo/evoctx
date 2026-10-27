"""`hold_session` end to end: the store, the policy layer, the server's
connection slot, the CLI and the Stop hook.

The rule's own boundaries are in test_holds.py. These tests are about every
path that writes or reads a hold agreeing with it — and about one window
never being able to silence another window's reminder.
"""
import importlib
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from evoctx.audit import AuditWriter
from evoctx.db import MIGRATIONS, Database
from evoctx.grants import GrantStore
from evoctx.policy import PolicyDenied, PolicyEngine


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def ahead(**kw) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat()


def engine(db, tmp_path) -> PolicyEngine:
    db.upsert_project("p1")
    return PolicyEngine(db, GrantStore(tmp_path),
                        audit=AuditWriter(db.conn, "test-connection"))


def audit(db, tool="hold_session"):
    return [(r[0], r[1], r[2], r[3]) for r in db.conn.execute(
        "SELECT action, record_ids, query, result_count FROM audit_log "
        "WHERE tool = ? ORDER BY rowid", (tool,))]


def holds_rows(db):
    return db.conn.execute(
        "SELECT session_id, hold_hours FROM session_holds ORDER BY rowid").fetchall()


def backdate(db, sid, *, started=None, held=None):
    if started is not None:
        db.conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (started, sid))
    if held is not None:
        db.conn.execute("UPDATE session_holds SET held_at = ? WHERE session_id = ?", (held, sid))
    db.conn.commit()


def link_note(db, sid, created_at, title="n"):
    nid = db.add_note(title, "body", project="p1", session_id=sid)
    db.conn.execute("UPDATE notes SET created_at = ? WHERE id = ?", (created_at, nid))
    db.conn.commit()
    return nid


def scope_to(e, tmp_path, tools, projects):
    path = tmp_path / "grants.yaml"
    path.write_text(
        "grants:\n  - name: narrow\n    client: \"*\"\n"
        f"    tools: [{', '.join(tools)}]\n"
        f"    scope:\n      projects: [{', '.join(repr(p) for p in projects)}]\n"
        "    expires: \"+1d\"\n", encoding="utf-8")
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))
    e.grants.activate("narrow")


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_STORE", str(tmp_path))
    monkeypatch.setenv("CONTEXT_CLIENT", "pytest")
    import evoctx.server as server_mod
    importlib.reload(server_mod)
    return server_mod


def stop(cli, monkeypatch, stdin=None):
    monkeypatch.setattr(sys, "argv", ["evoctx", "hook", "stop"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin or {})))
    try:
        cli.main()
    except SystemExit as e:
        return e.code
    return 0


# ── the store ─────────────────────────────────────────────────────────────────

def test_a_future_dated_note_does_not_hide_an_older_real_one(cli, monkeypatch, capsys):
    db = cli.db
    db.upsert_project("p1")
    sid = db.start_session(project="p1", intent="work")
    db.hold_session(sid, 2)
    backdate(db, sid, started=ago(hours=3), held=ago(hours=3))
    link_note(db, sid, ago(minutes=30))
    link_note(db, sid, ahead(days=1))

    hold = db.get_session(sid)["hold"]
    assert hold["active"] is True
    assert stop(cli, monkeypatch) == 0, capsys.readouterr().err


def test_a_hold_renews_in_place(db):
    sid = db.start_session(project="p1")
    db.hold_session(sid, 2)
    backdate(db, sid, held=ago(hours=5))
    before = db.get_session(sid)["hold"]["held_at"]
    assert db.hold_session(sid, 5) is True
    assert holds_rows(db) == [(sid, 5.0)]
    assert db.get_session(sid)["hold"]["held_at"] > before


def test_a_hold_is_only_written_for_an_open_session(db, tmp_path):
    closed = db.start_session(project="p1")
    db.end_session(closed, "done")
    assert db.hold_session(closed, 2) is False
    assert db.hold_session("no-such-session", 2) is False
    assert holds_rows(db) == []

    # closed through another connection: the gate is in the statement itself
    sid = db.start_session(project="p1")
    other = Database(tmp_path)
    other.end_session(sid, "closed elsewhere")
    assert db.hold_session(sid, 2) is False
    assert holds_rows(db) == []


def test_a_hold_placed_before_close_stays_as_the_record(db):
    sid = db.start_session(project="p1")
    db.hold_session(sid, 3)
    db.end_session(sid, "done")
    hold = db.get_session(sid)["hold"]
    assert hold["hours"] == 3.0 and hold["active"] is False


@pytest.mark.parametrize("bad", [0, 25, True, float("nan"), "2"])
def test_the_store_validates_every_hold_written_through_it(db, bad):
    sid = db.start_session(project="p1")
    with pytest.raises(ValueError):
        db.hold_session(sid, bad)
    assert holds_rows(db) == []


def test_the_hold_block_reads_the_right_columns(db):
    sid = db.start_session(project="p1")
    never = db.start_session(project="p1")
    db.hold_session(sid, 3.5)
    db.conn.execute("UPDATE session_holds SET held_at = ? WHERE session_id = ?",
                    ("2026-10-27T10:00:00+00:00", sid))
    db.conn.commit()
    for read in (db.get_session(sid),
                 next(s for s in db.recent_sessions() if s["id"] == sid)):
        assert read["hold"]["held_at"] == "2026-10-27T10:00:00+00:00"
        assert read["hold"]["hours"] == 3.5
    assert db.get_session(never)["hold"] is None


def test_stop_candidates_keep_every_held_session_and_only_recent_unheld(db):
    now = datetime.now(timezone.utc)
    recent = db.start_session(project="p1")
    backdate(db, recent, started=ago(hours=11, minutes=59))
    old = db.start_session(project="p1")
    backdate(db, old, started=ago(hours=13))
    held_old = db.start_session(project="p1")
    db.hold_session(held_old, 2)
    backdate(db, held_old, started=ago(days=3), held=ago(days=3))
    held_closed = db.start_session(project="p1")
    db.hold_session(held_closed, 2)
    db.end_session(held_closed, "done")

    ids = {c["id"] for c in db.stop_candidates([], now)}
    assert ids == {recent, held_old}


def test_notes_of_another_session_do_not_renew_a_hold(cli, monkeypatch, capsys):
    db = cli.db
    db.upsert_project("p1")
    held = db.start_session(project="p1", intent="held")
    db.hold_session(held, 2)
    backdate(db, held, started=ago(hours=3), held=ago(hours=3))
    # recent, so it is a stop candidate beside the held one and its note is
    # in the same grouped read
    busy = db.start_session(project="p1", intent="busy")
    backdate(db, busy, started=ago(hours=1))
    link_note(db, busy, ago(minutes=10))

    assert stop(cli, monkeypatch) == 2
    assert f"Held session {held}" in capsys.readouterr().err


def test_identical_start_times_order_newest_inserted_first(db):
    a = db.start_session(project="p1")
    b = db.start_session(project="p1")
    db.conn.execute("UPDATE sessions SET started_at = ?", (ago(minutes=1),))
    db.conn.commit()
    order = [c["id"] for c in db.stop_candidates([], datetime.now(timezone.utc))]
    assert order == [b, a]


def test_a_stop_reads_the_store_in_two_statements_however_many_sessions(db):
    now = datetime.now(timezone.utc)
    db.conn.executemany(
        "INSERT INTO sessions (id, project, client, started_at) VALUES (?, 'p1', 'c', ?)",
        [(f"old-{i:05d}", ago(days=2)) for i in range(500)])
    for i in range(50):
        sid = db.start_session(project="p1")
        db.hold_session(sid, 2)
        for _ in range(3):
            db.add_note("n", "b", project="p1", session_id=sid)
    statements = []
    db.conn.set_trace_callback(lambda sql: statements.append(sql))
    try:
        candidates = db.stop_candidates([], now)
    finally:
        db.conn.set_trace_callback(None)
    assert len(candidates) == 50
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 2


def test_an_existing_store_gains_the_table_with_no_schema_change(tmp_path):
    db = Database(tmp_path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) == 6
    db.conn.execute("DROP TABLE session_holds")
    db.conn.commit()
    db.conn.close()

    again = Database(tmp_path)
    assert again.conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'session_holds'").fetchone()
    assert again.conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert not list(tmp_path.glob("*.bak-pre-migration-*"))


# ── the policy layer ──────────────────────────────────────────────────────────

def test_the_own_session_is_held_by_omission_case_or_prefix(db, tmp_path):
    e = engine(db, tmp_path)
    own = e.t_start_session("pytest", "p1", "mine")["id"]
    for arg in (None, own.upper(), own[:8].upper()):
        out = e.t_hold_session("pytest", arg, None, own)
        assert out["id"] == own and out["hold"]["active"] is True, arg
    assert audit(db)[-1] == ("write", json.dumps([own]), json.dumps({"hours": 2.0}), 1)


def test_another_sessions_id_is_refused_when_the_connection_has_its_own(db, tmp_path):
    e = engine(db, tmp_path)
    own = e.t_start_session("pytest", "p1", "mine")["id"]
    other = db.start_session(project="p1", intent="another window")

    out = e.t_hold_session("pytest", other, None, own)
    assert out == {"error": f"This connection's session is {own[:8]}; hold only "
                            f"your own. A hold keeps a session's reminder quiet, "
                            f"and on another window's session it would do that "
                            f"while nobody is working in it."}
    assert holds_rows(db) == []
    assert audit(db) == [("denied", "[]", None, 0)]


def test_a_short_whole_id_cannot_get_past_the_ownership_check(db, tmp_path):
    e = engine(db, tmp_path)
    own = e.t_start_session("pytest", "p1", "mine")["id"]
    db.conn.execute(
        "INSERT INTO sessions (id, project, client, started_at) VALUES "
        "('b7short', 'p1', 'c', ?)", (ago(minutes=1),))
    db.conn.commit()

    out = e.t_hold_session("pytest", "b7short", None, own)
    assert "hold only your own" in out["error"]
    assert holds_rows(db) == []


def test_an_ambiguous_prefix_of_the_own_session_holds_the_own_one(db, tmp_path):
    e = engine(db, tmp_path)
    db.conn.executescript(
        "INSERT INTO sessions (id, project, client, intent, started_at) VALUES "
        "('sharedp1-own-0000', 'p1', 'pytest', 'own', '2027-01-01T00:00:00+00:00'),"
        "('sharedp1-oth-0000', 'p1', 'pytest', 'other', '2027-01-01T00:00:00+00:00');")
    db.conn.commit()
    out = e.t_hold_session("pytest", "sharedp1", None, "sharedp1-own-0000")
    assert out["id"] == "sharedp1-own-0000"


def test_a_too_short_prefix_gets_the_resolver_refusal_without_the_closing_line(db, tmp_path):
    e = engine(db, tmp_path)
    own = e.t_start_session("pytest", "p1", "mine")["id"]
    out = e.t_hold_session("pytest", own[:7], None, own)
    assert out == {"error": f"'{own[:7]}' is too short to identify a session. "
                            f"Give at least 8 characters of the id, or the whole id."}
    assert audit(db) == [("denied", "[]", None, 0)]


def test_out_of_scope_sessions_read_as_nonexistent_open_or_closed(db, tmp_path):
    e = engine(db, tmp_path)
    db.upsert_project("secret")
    open_secret = db.start_session(project="secret")
    closed_secret = db.start_session(project="secret")
    db.end_session(closed_secret, "done")
    scope_to(e, tmp_path, ["hold_session"], ["p1"])

    for sid in (open_secret, closed_secret):
        out = e.t_hold_session("pytest", sid, None, None)
        assert out == {"error": "No session with that id is open within this "
                                "grant's scope."}
    assert holds_rows(db) == []
    assert audit(db) == [("denied", "[]", None, 0)] * 2


def test_an_ambiguous_prefix_spanning_scopes_names_nothing(db, tmp_path):
    e = engine(db, tmp_path)
    db.upsert_project("secret")
    db.conn.executescript(
        "INSERT INTO sessions (id, project, client, intent, started_at) VALUES "
        "('sharedpfx-mine', 'p1', 'pytest', 'mine', '2027-01-01T00:00:00+00:00'),"
        "('sharedpfx-theirs', 'secret', 'pytest', 'theirs', '2027-01-01T00:00:00+00:00');")
    db.conn.commit()
    scope_to(e, tmp_path, ["hold_session"], ["p1"])

    out = e.t_hold_session("pytest", "sharedpfx-", None, None)
    assert "sharedpfx-th" not in out["error"]
    assert audit(db) == [("denied", "[]", None, 0)]


@pytest.mark.parametrize("bad", [0, -1, 24.0001, float("nan"), float("inf"), True, "2"])
def test_out_of_range_hours_are_refused_audited_and_never_clamped(db, tmp_path, bad):
    e = engine(db, tmp_path)
    own = e.t_start_session("pytest", "p1", "mine")["id"]
    out = e.t_hold_session("pytest", None, bad, own)
    assert "at most 24h" in out["error"]
    assert holds_rows(db) == []
    assert audit(db) == [("denied", "[]", None, 0)]


def test_the_default_and_the_maximum_are_accepted(db, tmp_path):
    e = engine(db, tmp_path)
    own = e.t_start_session("pytest", "p1", "mine")["id"]
    assert e.t_hold_session("pytest", None, None, own)["hold"]["hours"] == 2.0
    assert e.t_hold_session("pytest", None, 24, own)["hold"]["hours"] == 24.0


def test_holding_a_closed_session_says_so_and_is_a_read(db, tmp_path):
    e = engine(db, tmp_path)
    sid = db.start_session(project="p1")
    db.end_session(sid, "done")
    out = e.t_hold_session("pytest", sid, None, None)
    assert out["already_closed"] is True
    assert out["error"].startswith(f"Session {sid[:8]} was closed at ")
    assert out["error"].endswith("; nothing to hold.")
    assert audit(db) == [("read", json.dumps([sid]), json.dumps({"hours": 2.0}), 1)]
    assert holds_rows(db) == []


def test_a_grant_written_for_0_1_5_is_told_to_upgrade(db, tmp_path):
    e = engine(db, tmp_path)
    path = tmp_path / "grants.yaml"
    path.write_text(
        "grants:\n  - name: everything\n    client: \"*\"\n"
        "    tools: [list_projects, get_project, search_context, get_by_id, write_note,\n"
        "            start_session, end_session, recent_sessions, list_grants,\n"
        "            register_project]\n"
        "    scope: {projects: [\"*\"], exclude_tags: []}\n    expires: \"+30d\"\n",
        encoding="utf-8")
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))
    e.grants.activate("everything")
    with pytest.raises(PolicyDenied) as exc:
        e.t_hold_session("pytest", None, None, None)
    assert "grant upgrade" in str(exc.value)


# ── the server's connection slot ──────────────────────────────────────────────

def test_holding_the_own_session_needs_no_warning_and_notes_renew_it(server):
    server.db.upsert_project("p1")
    sid = server.start_session(ctx=None, project="p1", intent="mine")["id"]
    out = server.hold_session(ctx=None)
    assert "warning" not in out and out["hold"]["active"] is True

    backdate(server.db, sid, started=ago(hours=3), held=ago(hours=3))
    assert server.db.get_session(sid)["hold"]["active"] is False
    server.write_note("progress", "body", ctx=None, project="p1")
    assert server.db.get_session(sid)["hold"]["active"] is True


def test_a_connection_with_no_session_holds_by_id_with_a_warning_and_adopts_nothing(server):
    server.db.upsert_project("p1")
    sid = server.db.start_session(project="p1", intent="from before a restart")
    out = server.hold_session(ctx=None, session_id=sid)
    assert out["hold"]["active"] is True
    assert sid in out["warning"]
    assert server._current_session_id is None
    note = server.write_note("after", "body", ctx=None, project="p1")
    assert server.db.conn.execute(
        "SELECT session_id FROM notes WHERE id = ?", (note,)).fetchone()[0] is None


def test_an_own_session_closed_out_of_band_lets_go_of_the_slot_first(server):
    server.db.upsert_project("p1")
    mine = server.start_session(ctx=None, project="p1", intent="mine")["id"]
    other = server.db.start_session(project="p1", intent="other")
    server.db.end_session(mine, "closed from the CLI")

    out = server.hold_session(ctx=None, session_id=other)
    assert out["id"] == other and "warning" in out
    assert server._current_session_id is None
    bare = server.hold_session(ctx=None)
    assert "No session was started on this connection" in bare["error"]


def test_an_own_session_closed_during_the_call_lets_go_of_the_slot(server, monkeypatch):
    """The server checks its own session before the policy call; closing it
    in between must still leave the slot empty afterwards."""
    server.db.upsert_project("p1")
    mine = server.start_session(ctx=None, project="p1", intent="mine")["id"]
    real = server.policy.t_hold_session

    def close_first(*args, **kwargs):
        server.db.end_session(mine, "closed elsewhere")
        return real(*args, **kwargs)

    monkeypatch.setattr(server.policy, "t_hold_session", close_first)
    out = server.hold_session(ctx=None)
    assert out["already_closed"] is True
    assert server._current_session_id is None


def test_hold_session_is_a_registered_tool_with_a_cli_mirror(server, cli):
    import asyncio
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "hold_session" in names
    assert "Same as MCP hold_session()" in (cli.__doc__ or "")


# ── the CLI ───────────────────────────────────────────────────────────────────

def test_cli_hold_session(cli, monkeypatch, capsys):
    from conftest import run
    sid = cli.db.start_session(project="p1", intent="mine")
    run(cli, monkeypatch, "hold-session", sid[:8], "--hours", "3")
    assert "3h idle" in capsys.readouterr().out
    assert holds_rows(cli.db) == [(sid, 3.0)]

    for bad in ("0", "nan", "25"):
        with pytest.raises(SystemExit) as exc:
            run(cli, monkeypatch, "hold-session", sid, "--hours", bad)
        assert exc.value.code == 1
        assert "at most 24h" in capsys.readouterr().out
    assert holds_rows(cli.db) == [(sid, 3.0)]

    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "hold-session", sid[:7])
    assert exc.value.code == 1

    cli.db.end_session(sid, "done")
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "hold-session", sid)
    assert exc.value.code == 1
    assert "is closed; nothing to hold." in capsys.readouterr().out

    run(cli, monkeypatch, "recent-sessions")
    assert "hold:    3h idle" in capsys.readouterr().out


# ── the Stop hook ─────────────────────────────────────────────────────────────

def test_stop_is_quiet_for_an_active_hold_and_says_when_it_lapsed(cli, monkeypatch, capsys):
    sid = cli.db.start_session(project=None, intent="long slice")
    cli.db.hold_session(sid, 2)
    assert stop(cli, monkeypatch) == 0

    backdate(cli.db, sid, started=ago(hours=3), held=ago(hours=3))
    assert stop(cli, monkeypatch) == 2
    err = capsys.readouterr().err
    assert f"Held session {sid}" in err
    assert "past its 2h hold" in err
    assert f'hold_session(session_id="{sid}")' in err
    assert f'end_session(summary, session_id="{sid}")' in err
    assert "cannot tell which window opened it" in err
    # A lapsed hold is held, and may be the reader's own: no state-based rule
    # may tell the owner that a held session is somebody else's.
    assert "closed or held" not in err
    assert "only if its id is the one start_session() returned to you" in err
    assert "If it is yours and the work is still going, renew it" in err


def test_stop_lists_every_due_session_lapsed_first_none_hidden(cli, monkeypatch, capsys):
    db = cli.db
    unheld = []
    for i in range(6):
        sid = db.start_session(project=None, intent=f"u{i}")
        backdate(db, sid, started=ago(minutes=60 - i))
        unheld.append(sid)
    lapsed = db.start_session(project=None, intent="lapsed")
    db.hold_session(lapsed, 2)
    backdate(db, lapsed, started=ago(days=2), held=ago(hours=3))
    active = db.start_session(project=None, intent="active")
    db.hold_session(active, 2)

    assert stop(cli, monkeypatch) == 2
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith(f"Held session {lapsed}")
    detail = [ln for ln in lines if ln.startswith(("Held session", "Open session"))]
    assert len(detail) == 5
    newest_first = list(reversed(unheld))
    assert lines[5] == f"Also due: {', '.join(newest_first[4:])}."
    assert all(f'session_id="{s}"' in "\n".join(lines) for s in newest_first[:4])
    assert active not in "\n".join(lines)


def test_stop_scope_applies_to_held_sessions(cli, monkeypatch, tmp_path):
    here, there = tmp_path / "ws-a" / "repo", tmp_path / "ws-b" / "repo"
    here.mkdir(parents=True)
    there.mkdir(parents=True)
    cli.db.register_project("here", here)
    cli.db.register_project("there", there)
    sid = cli.db.start_session(project="there", intent="lapsed elsewhere")
    cli.db.hold_session(sid, 2)
    backdate(cli.db, sid, started=ago(hours=3), held=ago(hours=3))
    assert stop(cli, monkeypatch, {"cwd": str(here)}) == 0

    mine = cli.db.start_session(project="here", intent="lapsed here")
    cli.db.hold_session(mine, 2)
    backdate(cli.db, mine, started=ago(hours=3), held=ago(hours=3))
    assert stop(cli, monkeypatch, {"cwd": str(here)}) == 2


def test_an_unreadable_session_does_not_hide_the_others(cli, monkeypatch, capsys):
    bad = cli.db.start_session(project=None, intent="hand-edited")
    cli.db.hold_session(bad, 2)
    backdate(cli.db, bad, started="garbage")
    good = cli.db.start_session(project=None, intent="recent")

    assert stop(cli, monkeypatch) == 2
    err = capsys.readouterr().err
    assert good in err and bad not in err


def test_stop_hook_active_still_wins_over_a_lapsed_hold(cli, monkeypatch):
    sid = cli.db.start_session(project=None)
    cli.db.hold_session(sid, 2)
    backdate(cli.db, sid, started=ago(hours=3), held=ago(hours=3))
    assert stop(cli, monkeypatch, {"stop_hook_active": True}) == 0


def test_end_session_without_an_id_still_finds_an_old_open_session(cli, monkeypatch, capsys):
    """The scope clause is shared with the hook's query; the hook's time filter
    is not, so the CLI's default still reaches a session forgotten days ago."""
    from conftest import run
    sid = cli.db.start_session(project=None, intent="forgotten")
    backdate(cli.db, sid, started=ago(hours=48))
    run(cli, monkeypatch, "end-session", "closing it")
    assert cli.db.get_session(sid)["open"] is False


# ── what both tools share ─────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", ["end_session", "hold_session"])
@pytest.mark.parametrize("shape", ["too-short", "ambiguous", "unknown"])
def test_every_resolver_refusal_is_logged_under_its_own_tool_with_nothing_in_it(
        db, tmp_path, tool, shape):
    e = engine(db, tmp_path)
    db.conn.executescript(
        "INSERT INTO sessions (id, project, client, intent, started_at) VALUES "
        "('twinpfx2-one', 'p1', 'pytest', 'a', '2027-01-01T00:00:00+00:00'),"
        "('twinpfx2-two', 'p1', 'pytest', 'b', '2027-01-01T00:00:00+00:00');")
    db.conn.commit()
    arg = {"too-short": "twin", "ambiguous": "twinpfx2-", "unknown": "no-such-session"}[shape]
    if tool == "end_session":
        e.t_end_session("pytest", arg, "summary")
    else:
        e.t_hold_session("pytest", arg, None, None)
    rows = db.conn.execute(
        "SELECT tool, action, record_ids, query, result_count FROM audit_log").fetchall()
    assert rows == [(tool, "denied", "[]", None, 0)]


def test_every_instruction_channel_says_when_to_hold(cli, monkeypatch, capsys):
    from pathlib import Path

    import evoctx.server as server_mod
    root = Path(__file__).resolve().parent.parent
    snippet = (root / "docs" / "instructions-snippet.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["evoctx", "hook", "session-start"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    cli.main()
    session_start = capsys.readouterr().out
    for channel, text in (("instructions=", server_mod.mcp.instructions or ""),
                          ("snippet", snippet), ("README", readme),
                          ("SessionStart", session_start)):
        assert "hold_session" in text, channel
        assert "hold.active" in text, channel