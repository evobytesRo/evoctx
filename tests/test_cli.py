"""CLI smoke tests — invoke evoctx main() with argv against a temp store."""
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
