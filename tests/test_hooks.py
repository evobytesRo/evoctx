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


# ── cmd_hook: stop, scoped to the workspace it fired for ──────────────────────
# Unfiltered the nag is machine-wide, so closing a window in one repo asks about
# a session belonging to another. Observed twice on 2026-07-31.

@pytest.fixture
def two_repos(cli, tmp_path):
    """Two repos in genuinely different workspaces — NOT siblings.

    Siblings share a workspace, so the hook is supposed to nag across them.
    Nesting each under its own parent is what makes "another workspace" mean
    what the name says.
    """
    here, there = tmp_path / "ws-a" / "repo", tmp_path / "ws-b" / "repo"
    here.mkdir(parents=True); there.mkdir(parents=True)
    cli.db.register_project("here", here)
    cli.db.register_project("there", there)
    return here, there


def test_hook_stop_ignores_another_workspaces_session(cli, monkeypatch, capsys, two_repos):
    here, _there = two_repos
    cli.db.start_session(project="there", intent="other workspace")

    code = run_hook(cli, monkeypatch, capsys, "stop", stdin_json={"cwd": str(here)})
    assert code == 0


def test_hook_stop_still_blocks_for_this_workspaces_session(cli, monkeypatch, capsys, two_repos):
    here, _there = two_repos
    cli.db.start_session(project="here", intent="mine")

    code = run_hook(cli, monkeypatch, capsys, "stop", stdin_json={"cwd": str(here)})
    assert code == 2


def test_hook_stop_still_blocks_for_a_session_with_no_project(cli, monkeypatch, capsys, two_repos):
    """A session started without a project is the one most likely to be
    forgotten. Filtering it out to fix the cross-workspace nagging would have
    traded a wrong reminder for a missing one."""
    here, _there = two_repos
    cli.db.start_session(intent="no project chosen")

    code = run_hook(cli, monkeypatch, capsys, "stop", stdin_json={"cwd": str(here)})
    assert code == 2


def test_hook_stop_blocks_for_a_sibling_repos_session(cli, monkeypatch, capsys, tmp_path):
    """A workspace is several repos, and the assistant is told to name
    whichever one the work targets. Narrowed to the launching folder alone the
    hook goes silent for all the others — worse than the machine-wide nagging
    it replaced, because a wrong reminder is noise and a missing one loses the
    session."""
    here, sibling = tmp_path / "ws" / "repo-a", tmp_path / "ws" / "repo-b"
    here.mkdir(parents=True); sibling.mkdir(parents=True)
    cli.db.register_project("repo-a", here)
    cli.db.register_project("repo-b", sibling)
    cli.db.start_session(project="repo-b", intent="the sibling's work")

    code = run_hook(cli, monkeypatch, capsys, "stop", stdin_json={"cwd": str(here)})
    assert code == 2


def test_hook_stop_on_an_unregistered_directory_stays_machine_wide(cli, monkeypatch, capsys, tmp_path):
    """The degradation has to be today's behaviour, not silence — an
    unresolved workspace must not turn the reminder off.

    Weak on its own, and kept for the documented case rather than for its
    teeth: with no roots anywhere, `nearby` is empty by construction, so this
    passes against the very code that got the rung-3 case wrong. The test
    below, with a registered sibling, is the discriminating one.
    """
    stranger = tmp_path / "unregistered"
    stranger.mkdir()
    cli.db.start_session(project="somewhere", intent="elsewhere")

    code = run_hook(cli, monkeypatch, capsys, "stop", stdin_json={"cwd": str(stranger)})
    assert code == 2


def test_hook_stop_unresolved_with_a_registered_sibling_stays_machine_wide(
        cli, monkeypatch, capsys, tmp_path):
    """`nearby` is populated at rung 3 too — it lists candidates for a
    directory that matched nothing. Feeding it to the filter narrows the
    reminder on the strength of a resolution that did not happen, silencing
    any session whose project has no directory at all.

    The plain unregistered-directory test above cannot catch this: with no
    roots anywhere, `nearby` is empty by construction and the test passes for
    the wrong reason. This one registers a sibling so `nearby` is non-empty.
    """
    ws = tmp_path / "ws"
    bound, stranger = ws / "repo-a", ws / "scratch"
    bound.mkdir(parents=True); stranger.mkdir(parents=True)
    cli.db.register_project("repo-a", bound)
    cli.db.upsert_project("topic-only")            # a real project with no directory
    cli.db.start_session(project="topic-only", intent="no root anywhere")

    code = run_hook(cli, monkeypatch, capsys, "stop", stdin_json={"cwd": str(stranger)})
    assert code == 2


def test_hook_stop_does_not_narrow_on_context_project(cli, monkeypatch, capsys, tmp_path):
    """`CONTEXT_PROJECT` says which project this *server* serves. It is not a
    claim that no other project has an open session, and it describes no
    workspace — so there is nothing in it to narrow with.

    This hook is a separate process launched from settings.json: it does not
    inherit the MCP config's env block, it inherits the user's shell. Narrowing
    on it would let one user-level variable make every session on every other
    project unreachable by the reminder, machine-wide and permanently.

    `conftest` deletes the variable autouse, so setting it here is deliberate
    and also proves the fixture is not what makes this pass.
    """
    ws = tmp_path / "ws"
    a, b = ws / "repo-a", ws / "repo-b"
    a.mkdir(parents=True); b.mkdir(parents=True)
    cli.db.register_project("repo-a", a)
    cli.db.register_project("repo-b", b)
    cli.db.start_session(project="repo-b", intent="the sibling's work")
    monkeypatch.setenv("CONTEXT_PROJECT", "repo-a")

    code = run_hook(cli, monkeypatch, capsys, "stop", stdin_json={"cwd": str(a)})
    assert code == 2


def test_hook_stop_names_the_sessions_project(cli, monkeypatch, capsys, tmp_path):
    """The reminder deliberately spans the workspace, so the session it names
    may belong to the repo next door. Without the project in the line, a
    correct nag is indistinguishable from a bug.

    The session must therefore be on a DIFFERENT project from the one the
    directory resolves to. With both the same, the assertion passes whether the
    message prints the session's project or the resolved one — pinning that
    there is *a* project in the line, not the right one, which is the entire
    justification for printing it.
    """
    ws = tmp_path / "ws"
    a, b = ws / "repo-a", ws / "repo-b"
    a.mkdir(parents=True); b.mkdir(parents=True)
    cli.db.register_project("repo-a", a)
    cli.db.register_project("repo-b", b)
    cli.db.start_session(project="repo-b", intent="the sibling's work")

    run_hook(cli, monkeypatch, capsys, "stop", stdin_json={"cwd": str(a)})
    err = capsys.readouterr().err
    assert "[repo-b]" in err
    assert "[repo-a]" not in err
