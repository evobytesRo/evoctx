"""CLI smoke tests — invoke evoctx main() with argv against a temp store."""
import os
import time

import pytest

from conftest import run


def test_write_note_and_list_projects(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "new-project", "p1", "--overview", "test project")
    run(cli, monkeypatch, "write-note", "T1", "--project", "p1", "--text", "body", "--tags", "#x")
    run(cli, monkeypatch, "list-projects")
    out = capsys.readouterr().out
    assert "p1" in out
    assert "1 project(s)" in out


def test_search_and_show_roundtrip(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "add", "Findable title", "--text", "unique keyword here")
    capsys.readouterr()  # drain the add output
    run(cli, monkeypatch, "search", "unique")
    out = capsys.readouterr().out
    assert "Findable title" in out
    note_id = out.strip().splitlines()[0].split()[0]
    run(cli, monkeypatch, "get-by-id", note_id)
    assert "unique keyword here" in capsys.readouterr().out


def test_get_project_output(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "new-project", "p1", "--overview", "the overview")
    run(cli, monkeypatch, "get-project", "p1")
    out = capsys.readouterr().out
    assert "the overview" in out
    assert "Open questions" in out


def test_session_lifecycle(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "start-session", "--project", "p1", "--intent", "cli test")
    out = capsys.readouterr().out
    assert "Started session" in out

    run(cli, monkeypatch, "recent-sessions")
    out = capsys.readouterr().out
    assert "OPEN" in out
    assert "cli test" in out

    run(cli, monkeypatch, "end-session", "wrapped up")
    assert "Closed session" in capsys.readouterr().out

    run(cli, monkeypatch, "recent-sessions")
    out = capsys.readouterr().out
    assert "closed" in out
    assert "wrapped up" in out


def test_end_session_without_open_exits_nonzero(cli, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        run(cli, monkeypatch, "end-session", "nothing to close")


def test_delete_force_refuses_an_ambiguous_prefix(cli, monkeypatch, capsys):
    """--force skips the confirmation prompt, so an ambiguous prefix must not
    resolve to an arbitrary note — that would be an unrecoverable delete of
    something the user never named."""
    run(cli, monkeypatch, "add", "first", "--text", "x")
    run(cli, monkeypatch, "add", "second", "--text", "y")
    cli.db.conn.execute("UPDATE notes SET id = 'dupe-' || id")
    cli.db.conn.commit()
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "delete", "dupe-", "--force")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Ambiguous id" in out
    assert "matches 2 records" in out
    assert len(cli.db.list_notes()) == 2, "nothing may be deleted on an ambiguous prefix"


def test_end_session_refuses_an_ambiguous_prefix(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "start-session", "--project", "p1", "--intent", "one")
    run(cli, monkeypatch, "start-session", "--project", "p2", "--intent", "two")
    cli.db.conn.execute("UPDATE sessions SET id = 'dupe-' || id")
    cli.db.conn.commit()
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "end-session", "summary", "--id", "dupe-")
    assert exc.value.code == 1
    sessions = cli.db.recent_sessions()
    assert "Ambiguous id" in capsys.readouterr().out
    assert len(sessions) == 2  # or the all() below is vacuously true
    assert all(s["open"] for s in sessions), "neither may be closed"


# ── grants ────────────────────────────────────────────────────────────────────

def _replace_grants(tmp_path, body: str):
    path = tmp_path / "grants.yaml"
    path.write_text(body, encoding="utf-8")
    # 1s mtime granularity on some filesystems would hide the change from the
    # store's hot-reload check
    stamp = time.time() + 2
    os.utime(path, (stamp, stamp))


def test_grant_activate_then_list(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "grant", "activate", "everything")
    assert "expires" in capsys.readouterr().out

    run(cli, monkeypatch, "grant", "list")
    out = capsys.readouterr().out
    assert "* everything" in out
    assert "Active: everything" in out


def test_grant_activate_already_expired_explains_and_exits_nonzero(
    cli, monkeypatch, capsys, tmp_path
):
    """Writing active_grant.json is not the same as having a usable grant.

    A grant whose expiry has already passed activates fine and then denies
    every call. Before this was handled, the success line dereferenced the
    None expiry and died with an AttributeError instead of saying so.
    """
    _replace_grants(
        tmp_path,
        'grants:\n  - name: stale\n    tools: [get_by_id]\n    expires: "+0m"\n',
    )
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "grant", "activate", "stale")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "not usable" in out
    assert "expired" in out
    assert "grants.yaml" in out  # points at the file to fix


def test_help_command(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "help")
    out = capsys.readouterr().out
    assert "evoctx list-projects" in out
    assert "Examples:" in out


def test_version_subcommand(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "version")
    assert "evoctx" in capsys.readouterr().out


def test_version_flag(cli, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "--version")
    assert exc.value.code == 0
    assert "evoctx" in capsys.readouterr().out


def test_demo_seed(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "demo-seed")
    out = capsys.readouterr().out
    assert "demo-webapp" in out
    run(cli, monkeypatch, "search", "httpOnly")
    assert "JWT" in capsys.readouterr().out


def test_delete_by_id_force(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "add", "T1", "--text", "x")
    note_id = cli.db.list_notes()[0]["id"]
    run(cli, monkeypatch, "delete", note_id, "--force")
    assert "Deleted" in capsys.readouterr().out
    assert cli.db.get_note(note_id) is None


def test_delete_without_force_prompts_and_cancels(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "add", "T1", "--text", "x")
    note_id = cli.db.list_notes()[0]["id"]
    monkeypatch.setattr("builtins.input", lambda: "n")
    run(cli, monkeypatch, "delete", note_id)
    assert "Cancelled" in capsys.readouterr().out
    assert cli.db.get_note(note_id) is not None


def test_delete_requires_exactly_one_selector(cli, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        run(cli, monkeypatch, "delete")


def test_delete_project_force(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "new-project", "p1", "--overview", "x")
    run(cli, monkeypatch, "delete-project", "p1", "--force")
    assert "Deleted project" in capsys.readouterr().out
    assert cli.db.get_project("p1") is None
