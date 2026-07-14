import json

from evoctx.db import Database
from evoctx.grants import GrantStore
from evoctx.install import (
    SERVER_NAME, claude_cli_hint, clients, doctor, install_claude_code,
    install_codex, install_json_client,
)


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
