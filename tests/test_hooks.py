import io
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from evoctx.install import install_claude_code_hooks, install_claude_code_permissions_deny


# ── install_claude_code_hooks (scratch home only, never the real one) ─────────

def test_install_hooks_fresh_settings(tmp_path):
    hooks = install_claude_code_hooks(tmp_path)
    assert "SessionStart" in hooks
    assert "Stop" in hooks
    config = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert config["hooks"]["SessionStart"][0]["hooks"][0]["type"] == "command"
    assert "hook" in config["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "stop" in config["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_install_hooks_preserves_existing_settings(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"model": "opus", "mcpServers": {"other": {}}}), encoding="utf-8")

    install_claude_code_hooks(tmp_path)

    config = json.loads(settings_path.read_text(encoding="utf-8"))
    assert config["model"] == "opus"
    assert "other" in config["mcpServers"]
    assert "hooks" in config
    assert settings_path.with_suffix(".json.bak").exists()


def test_install_hooks_command_paths_are_quoted(tmp_path):
    hooks = install_claude_code_hooks(tmp_path)
    cmd = hooks["SessionStart"][0]["hooks"][0]["command"]
    assert cmd.startswith('"')  # defensive quoting for paths with spaces


# ── install_claude_code_permissions_deny ───────────────────────────────────────

def test_install_permissions_deny_fresh_settings(tmp_path):
    store = tmp_path / "store"
    deny = install_claude_code_permissions_deny(tmp_path, store)
    assert any("Read(" in d for d in deny)
    assert any("Edit(" in d for d in deny)
    assert any("Write(" in d for d in deny)
    config = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert config["permissions"]["deny"] == deny


def test_install_permissions_deny_preserves_existing_and_no_duplicates(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "permissions": {"deny": ["Bash(rm:*)"]},
    }), encoding="utf-8")

    store = tmp_path / "store"
    install_claude_code_permissions_deny(tmp_path, store)
    install_claude_code_permissions_deny(tmp_path, store)  # idempotent, no dupes

    config = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "Bash(rm:*)" in config["permissions"]["deny"]
    read_rules = [d for d in config["permissions"]["deny"] if d.startswith("Read(")]
    assert len(read_rules) == 1


# ── cmd_hook: session-start ────────────────────────────────────────────────────

def run_hook(cli, monkeypatch, capsys, event: str, stdin_json: dict | None = None):
    monkeypatch.setattr(sys, "argv", ["evoctx", "hook", event])
    if stdin_json is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_json)))
    else:
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    try:
        cli.main()
    except SystemExit as e:
        return e.code
    return 0


def test_hook_session_start_prints_reminder(cli, monkeypatch, capsys):
    run_hook(cli, monkeypatch, capsys, "session-start")
    out = capsys.readouterr().out
    assert "start_session" in out


def test_hook_stop_no_open_session_allows_stop(cli, monkeypatch, capsys):
    code = run_hook(cli, monkeypatch, capsys, "stop")
    assert code == 0


def test_hook_stop_blocks_once_for_recent_open_session(cli, monkeypatch, capsys):
    cli.db.start_session(project="p1", intent="testing")
    code = run_hook(cli, monkeypatch, capsys, "stop")
    assert code == 2
    err = capsys.readouterr().err
    assert "end_session" in err


def test_hook_stop_never_blocks_twice(cli, monkeypatch, capsys):
    cli.db.start_session(project="p1", intent="testing")
    code = run_hook(cli, monkeypatch, capsys, "stop", stdin_json={"stop_hook_active": True})
    assert code == 0


def test_hook_stop_ignores_old_open_session(cli, monkeypatch, capsys):
    sid = cli.db.start_session(project="p1", intent="ancient")
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    cli.db.conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (old, sid))
    cli.db.conn.commit()
    code = run_hook(cli, monkeypatch, capsys, "stop")
    assert code == 0


def test_hook_stop_ignores_closed_session(cli, monkeypatch, capsys):
    sid = cli.db.start_session(project="p1")
    cli.db.end_session(sid, "done")
    code = run_hook(cli, monkeypatch, capsys, "stop")
    assert code == 0
