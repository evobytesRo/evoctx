import json
from pathlib import Path

from evoctx.db import Database
from evoctx.grants import GrantStore
from evoctx.install import (
    SERVER_NAME, claude_cli_hint, clients, doctor, install_claude_code,
    install_codex, install_json_client, workspace_folder_warning,
)


# ── workspace-folder detection ────────────────────────────────────────────────
# Claude Code discovers .mcp.json as the union of the OPEN WORKSPACE'S folders.
# Writing one outside those folders succeeds and is then silently ignored —
# verified 2026-07-30, and the cause of hours of misdiagnosis.

WS_MULTI_ROOT = """\
{
    "folders": [
        { "name": "API", "path": "api" },
        { "name": "Web", "path": "sub/web" }
    ]
}
"""

# .code-workspace is JSONC: VS Code accepts comments and trailing commas,
# json.loads does not. Real files in this workspace have both.
WS_JSONC = """\
{
    // the backend
    "folders": [
        { "name": "API", "path": "api", },
    ],
    "settings": {},
}
"""


def test_warns_when_target_is_not_a_workspace_folder(tmp_path):
    (tmp_path / "root.code-workspace").write_text(WS_MULTI_ROOT, encoding="utf-8")
    (tmp_path / "api").mkdir()

    warning = workspace_folder_warning(tmp_path)

    assert warning is not None
    assert "root.code-workspace" in warning
    assert str(tmp_path / "api") in warning          # names a folder that would work


def test_no_warning_when_target_is_a_listed_folder(tmp_path):
    (tmp_path / "root.code-workspace").write_text(WS_MULTI_ROOT, encoding="utf-8")
    api = tmp_path / "api"
    api.mkdir()

    assert workspace_folder_warning(api) is None


def test_no_warning_for_nested_listed_folder(tmp_path):
    (tmp_path / "root.code-workspace").write_text(WS_MULTI_ROOT, encoding="utf-8")
    web = tmp_path / "sub" / "web"
    web.mkdir(parents=True)

    assert workspace_folder_warning(web) is None


def test_dot_path_makes_the_root_itself_a_folder(tmp_path):
    (tmp_path / "root.code-workspace").write_text(
        '{"folders": [{"path": "."}, {"path": "api"}]}', encoding="utf-8"
    )

    assert workspace_folder_warning(tmp_path) is None


def test_jsonc_trailing_commas_and_comments_are_tolerated(tmp_path):
    """json.loads would raise on this; the folder list is regex-extracted."""
    (tmp_path / "ws.code-workspace").write_text(WS_JSONC, encoding="utf-8")
    (tmp_path / "api").mkdir()

    assert workspace_folder_warning(tmp_path) is not None      # root not listed
    assert workspace_folder_warning(tmp_path / "api") is None  # listed folder


def test_no_workspace_file_means_no_warning(tmp_path):
    """A single-folder window reads that folder's own .mcp.json — nothing to warn about."""
    solo = tmp_path / "solo"
    solo.mkdir()

    assert workspace_folder_warning(solo) is None


# ── regressions: all four reproduced against the first implementation ─────────

def test_path_keys_outside_folders_array_are_ignored(tmp_path):
    """A whole-file regex advertised a shell executable as a folder to install into."""
    (tmp_path / "ws.code-workspace").write_text(
        '{"folders": [{"path": "api"}],'
        ' "settings": {"terminal.integrated.profiles.windows":'
        ' {"Git Bash": {"path": "C:\\\\Program Files\\\\Git\\\\bin\\\\bash.exe"}}}}',
        encoding="utf-8",
    )
    (tmp_path / "api").mkdir()

    warning = workspace_folder_warning(tmp_path)

    assert warning is not None          # root still isn't a folder
    assert "bash.exe" not in warning    # but never offer it as one


def test_stray_dot_path_outside_folders_does_not_suppress_warning(tmp_path):
    """A '"path": "."' in a tasks block made the root look listed, silencing a real warning."""
    (tmp_path / "ws.code-workspace").write_text(
        '{"folders": [{"path": "api"}],'
        ' "tasks": {"version": "2.0.0", "tasks": [{"options": {"path": "."}}]}}',
        encoding="utf-8",
    )
    (tmp_path / "api").mkdir()

    assert workspace_folder_warning(tmp_path) is not None


def test_all_workspace_files_are_checked_not_just_the_first(tmp_path):
    """Stopping at the alphabetically-first file warned about setups that work."""
    (tmp_path / "client").mkdir()
    (tmp_path / "a-client.code-workspace").write_text(
        '{"folders":[{"path":"client"}]}', encoding="utf-8")
    (tmp_path / "b-full.code-workspace").write_text(
        '{"folders":[{"path":"."},{"path":"client"}]}', encoding="utf-8")

    # b-full lists "." — some open workspace covers this directory, so stay silent
    assert workspace_folder_warning(tmp_path) is None


def test_warning_is_conditional_not_absolute(tmp_path):
    """A workspace file on disk doesn't prove that workspace is the one open."""
    (tmp_path / "old.code-workspace").write_text(
        '{"folders":[{"path":"legacy"}]}', encoding="utf-8")
    (tmp_path / "legacy").mkdir()
    newproj = tmp_path / "newproj"
    newproj.mkdir()

    warning = workspace_folder_warning(newproj)

    assert warning is not None
    assert "If you open" in warning      # conditional, not "will not read"
    assert "ignore this" in warning      # names the single-folder case


def test_bracket_inside_a_folder_path_does_not_unbalance_the_scan(tmp_path):
    weird = tmp_path / "a[1]"
    weird.mkdir()
    (tmp_path / "ws.code-workspace").write_text(
        '{"folders":[{"path":"a[1]"}]}', encoding="utf-8")

    assert workspace_folder_warning(weird) is None
    assert workspace_folder_warning(tmp_path) is not None


def test_bracket_in_block_comment_does_not_close_the_array(tmp_path):
    """A `]` inside a comment closed the folders array early, and the
    commented-out entry was then advertised as a folder to install into."""
    (tmp_path / "ws.code-workspace").write_text(
        '{\n  "folders": [\n    /* { "path": "old" } ] */\n    { "path": "." }\n  ]\n}',
        encoding="utf-8",
    )

    warning = workspace_folder_warning(tmp_path)

    assert warning is None              # "." is listed — this directory is covered


def test_commented_out_folders_key_does_not_steal_the_search(tmp_path):
    """`_FOLDERS_KEY_RE` locked onto a commented-out key, so the real array
    was never read and the whole file became invisible."""
    # The commented-out array must contain a real "path" value, or this test
    # passes against the broken implementation too: with no paths to extract,
    # the stolen scan yields [], the candidate is skipped, and the result is
    # None for the wrong reason. With "ghost" present, the pre-fix code
    # extracts it, warns, and the assert below fails as it should.
    (tmp_path / "ws.code-workspace").write_text(
        '{\n    // "folders": [ { "path": "ghost" } ]\n'
        '    "folders": [ { "path": "." } ]\n}',
        encoding="utf-8",
    )

    assert workspace_folder_warning(tmp_path) is None


def test_commented_out_entry_is_not_a_live_folder(tmp_path):
    """A commented-out entry counted as a real folder, wrongly silencing the notice."""
    (tmp_path / "legacy").mkdir()
    (tmp_path / "ws.code-workspace").write_text(
        '{\n  "folders": [\n    // { "path": "legacy" },\n    { "path": "api" }\n  ]\n}',
        encoding="utf-8",
    )
    (tmp_path / "api").mkdir()

    warning = workspace_folder_warning(tmp_path / "legacy")

    assert warning is not None
    assert "legacy" not in warning.split("is not listed")[1]  # never offered as a folder


def test_bom_and_crlf_are_tolerated(tmp_path):
    # Non-discriminating by nature: the folders key is found by regex mid-file,
    # so a leading BOM never reaches it and this passes with or without the
    # utf-8-sig read. Kept as a smoke test of realistic file shape — do not
    # treat it as protecting the encoding choice.
    (tmp_path / "ws.code-workspace").write_text(
        '﻿{\r\n  "folders": [\r\n    { "path": "." }\r\n  ]\r\n}',
        encoding="utf-8",
    )

    assert workspace_folder_warning(tmp_path) is None


def test_next_steps_covers_every_client_install_supports():
    """The original bug was a missing dict key that no test caught."""
    from evoctx.install import NEXT_STEPS

    for name in [*clients(Path.home()), "claude-code", "codex", "codex-uvx"]:
        assert name in NEXT_STEPS, f"NEXT_STEPS missing guidance for {name!r}"


def test_install_fresh_config(tmp_path):
    client = clients(tmp_path)["cursor"]
    entry = install_json_client(client)
    config = json.loads(client.config_path.read_text(encoding="utf-8"))
    assert config["mcpServers"][SERVER_NAME] == entry
    assert entry["env"]["CONTEXT_CLIENT"] == "cursor"
    assert "type" not in entry  # cursor block carries no type field


def test_install_merges_and_backs_up(tmp_path):
    client = clients(tmp_path)["claude-desktop"]
    client.config_path.parent.mkdir(parents=True)
    client.config_path.write_text(json.dumps({
        "model": "opus",
        "mcpServers": {"other-server": {"command": "x"}},
    }), encoding="utf-8")

    install_json_client(client)

    config = json.loads(client.config_path.read_text(encoding="utf-8"))
    # pre-existing settings survive the merge
    assert config["model"] == "opus"
    assert "other-server" in config["mcpServers"]
    assert SERVER_NAME in config["mcpServers"]
    # backup created
    assert client.config_path.with_suffix(".json.bak").exists()


# ── claude-code: project-scoped .mcp.json, NOT ~/.claude/settings.json ────────
# Confirmed by live testing: an entry in settings.json's mcpServers key was
# never read by a fresh session; the same entry in project-root .mcp.json
# connected immediately. A bare ~/.claude/.mcp.json (no project scope) was
# also tested live and does NOT work either — .mcp.json must be at the
# actual open workspace root.

def test_claude_code_writes_project_scoped_mcp_json(tmp_path):
    path, entry = install_claude_code(tmp_path)
    assert path == tmp_path / ".mcp.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["mcpServers"][SERVER_NAME] == entry
    assert entry["type"] == "stdio"
    assert entry["env"]["CONTEXT_CLIENT"] == "claude-code"


def test_claude_code_merges_and_backs_up(tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")

    install_claude_code(tmp_path)

    config = json.loads(path.read_text(encoding="utf-8"))
    assert "other" in config["mcpServers"]
    assert SERVER_NAME in config["mcpServers"]
    assert path.with_suffix(".json.bak").exists()


def test_claude_code_uvx_variant(tmp_path):
    _, entry = install_claude_code(tmp_path, uvx=True)
    assert entry["command"] == "uvx"
    assert entry["args"] == ["evoctx", "serve"]


def test_claude_cli_hint_none_when_cli_absent(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert claude_cli_hint("evoctx", ["serve"]) is None


def test_claude_cli_hint_present_when_cli_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: r"C:\bin\claude.exe" if name == "claude" else None)
    hint = claude_cli_hint("evoctx", ["serve"])
    assert hint is not None
    assert "claude mcp add" in hint
    assert "--scope user" in hint
    assert "evoctx" in hint


def test_install_uvx_variant(tmp_path):
    client = clients(tmp_path)["windsurf"]
    entry = install_json_client(client, uvx=True)
    assert entry["command"] == "uvx"
    assert entry["args"] == ["evoctx", "serve"]


def test_vscode_uses_servers_key(tmp_path):
    client = clients(tmp_path)["vscode"]
    install_json_client(client)
    config = json.loads(client.config_path.read_text(encoding="utf-8"))
    assert SERVER_NAME in config["servers"]


def test_codex_toml_append_and_replace(tmp_path):
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('model = "o3"\n', encoding="utf-8")

    install_codex(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert 'model = "o3"' in text
    assert f"[mcp_servers.{SERVER_NAME}]" in text

    # re-running replaces the block instead of duplicating it
    install_codex(tmp_path, uvx=True)
    text = path.read_text(encoding="utf-8")
    assert text.count(f"[mcp_servers.{SERVER_NAME}]") == 1
    assert 'command = "uvx"' in text


def test_doctor_healthy_store(tmp_path):
    store = tmp_path / "store"
    Database(store)
    GrantStore(store)
    results = {check: ok for check, ok, _ in doctor(store, tmp_path)}
    assert results["store dir"]
    assert results["database"]
    assert results["active grant"]
    # no client configs in the fake home
    assert results["client configs"] is False


def test_doctor_detects_installed_client(tmp_path):
    store = tmp_path / "store"
    Database(store)
    GrantStore(store)
    install_json_client(clients(tmp_path)["cursor"])
    checks = {check: (ok, detail) for check, ok, detail in doctor(store, tmp_path)}
    ok, detail = checks["client configs"]
    assert ok and "cursor" in detail


def test_doctor_flags_missing_grant(tmp_path):
    store = tmp_path / "store"
    Database(store)
    gs = GrantStore(store)
    gs.deactivate()
    checks = {check: ok for check, ok, _ in doctor(store, tmp_path)}
    assert checks["active grant"] is False
