"""Client onboarding — write the MCP server block into a client's config,
and `doctor` checks that verify the whole setup end to end.

Config formats drift between client versions, so every write:
- backs the existing file up to <name>.bak first,
- merges into the existing JSON instead of replacing it,
- prints the block afterwards so it can be pasted manually if anything looks off.
"""
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SERVER_NAME = "evoctx"


def server_command() -> tuple[str, list[str]]:
    """Command that launches the MCP server: the `evoctx` console script
    installed next to the running interpreter, given `serve`, falling back
    to `python -m evoctx.cli serve`."""
    exe = shutil.which("evoctx", path=str(Path(sys.executable).parent))
    if exe:
        return exe, ["serve"]
    return sys.executable, ["-m", "evoctx.cli", "serve"]


def _evoctx_shell_command(*args: str) -> str:
    """A single shell command string invoking evoctx — for hook 'command'
    fields, which are shell strings, not argv arrays. Quoted defensively;
    paths under 'Program Files' etc. contain spaces."""
    exe = shutil.which("evoctx", path=str(Path(sys.executable).parent))
    base = f'"{exe}"' if exe else f'"{sys.executable}" -m evoctx.cli'
    return f"{base} {' '.join(args)}"


def install_claude_code_permissions_deny(home: Path, store_dir: Path) -> list[str]:
    """Denies the assistant's own file-read/write/edit tools access to the
    store directory — the mitigation SECURITY.md §1 points at for grants
    only gating the MCP protocol path, not the filesystem. Best-effort: this
    is Claude Code's permission-rule syntax as of writing, not something
    this project controls the stability of. evoctx doctor cannot verify a
    deny rule took effect (that would require driving the actual client),
    so treat this as a starting point to confirm manually, not a guarantee."""
    path = home / ".claude" / "settings.json"
    config: dict = {}
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        config = json.loads(path.read_text(encoding="utf-8") or "{}")
    deny = config.setdefault("permissions", {}).setdefault("deny", [])
    glob = f"{store_dir}/**".replace("\\", "/")
    for tool in ("Read", "Edit", "Write"):
        rule = f"{tool}({glob})"
        if rule not in deny:
            deny.append(rule)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return deny


def install_claude_code_hooks(home: Path) -> dict:
    """SessionStart nudges the assistant to call start_session/recent_sessions;
    Stop blocks once (never twice — see stop_hook_active in cli.py's cmd_hook)
    if a session was opened recently and never closed. This is the one
    mechanism that goes beyond convention, and the one place a malformed hook
    could visibly misbehave — kept as an opt-in flag (--hooks), never the
    install default, and every write here is backed up first like every
    other install_* function."""
    path = home / ".claude" / "settings.json"
    config: dict = {}
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        config = json.loads(path.read_text(encoding="utf-8") or "{}")
    hooks = config.setdefault("hooks", {})
    hooks["SessionStart"] = [
        {"hooks": [{"type": "command", "command": _evoctx_shell_command("hook", "session-start")}]}
    ]
    hooks["Stop"] = [
        {"hooks": [{"type": "command", "command": _evoctx_shell_command("hook", "stop")}]}
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return hooks


def _appdata(home: Path) -> Path:
    # honor %APPDATA% only for the real home — a substituted home (tests,
    # doctor against another profile) must stay self-contained
    if home == Path.home() and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"])
    return home / "AppData" / "Roaming"


@dataclass
class Client:
    name: str
    config_path: Path
    key: str            # top-level key holding the server map
    with_type: bool     # include "type": "stdio" in the entry


def clients(home: Path) -> dict[str, Client]:
    if sys.platform == "win32":
        claude_desktop = _appdata(home) / "Claude" / "claude_desktop_config.json"
        vscode = _appdata(home) / "Code" / "User" / "mcp.json"
    elif sys.platform == "darwin":
        claude_desktop = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        vscode = home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    else:
        claude_desktop = home / ".config" / "Claude" / "claude_desktop_config.json"
        vscode = home / ".config" / "Code" / "User" / "mcp.json"

    return {
        "claude-code": Client("claude-code", home / ".claude" / "settings.json", "mcpServers", True),
        "claude-desktop": Client("claude-desktop", claude_desktop, "mcpServers", False),
        "cursor": Client("cursor", home / ".cursor" / "mcp.json", "mcpServers", False),
        "windsurf": Client("windsurf", home / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers", False),
        "vscode": Client("vscode", vscode, "servers", True),
        # codex is TOML, handled separately
    }


def entry_for(client: Client, command: str, args: list[str], uvx: bool) -> dict:
    if uvx:
        command, args = "uvx", ["evoctx", "serve"]
    entry: dict = {}
    if client.with_type:
        entry["type"] = "stdio"
    entry["command"] = command
    entry["args"] = args
    entry["env"] = {"CONTEXT_CLIENT": client.name}
    return entry


def install_json_client(client: Client, uvx: bool = False) -> dict:
    command, args = server_command()
    entry = entry_for(client, command, args, uvx)

    path = client.config_path
    config = {}
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        config = json.loads(path.read_text(encoding="utf-8") or "{}")
    path.parent.mkdir(parents=True, exist_ok=True)
    config.setdefault(client.key, {})[SERVER_NAME] = entry
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return entry


CODEX_BLOCK = """
[mcp_servers.{name}]
command = "{command}"
args = [{args}]
env = {{ CONTEXT_CLIENT = "codex" }}
"""


def install_codex(home: Path, uvx: bool = False) -> str:
    command, args = ("uvx", ["evoctx", "serve"]) if uvx else server_command()
    block = CODEX_BLOCK.format(
        name=SERVER_NAME,
        command=command.replace("\\", "\\\\"),
        args=", ".join(f'"{a}"' for a in args),
    )
    path = home / ".codex" / "config.toml"
    text = ""
    if path.exists():
        shutil.copy2(path, path.with_suffix(".toml.bak"))
        text = path.read_text(encoding="utf-8")
    header = f"[mcp_servers.{SERVER_NAME}]"
    if header in text:
        # replace the existing block: from the header to the next section or EOF
        lines = text.splitlines(keepends=True)
        out, skipping = [], False
        for line in lines:
            if line.strip() == header:
                skipping = True
                continue
            if skipping and line.lstrip().startswith("["):
                skipping = False
            if not skipping:
                out.append(line)
        text = "".join(out).rstrip("\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((text.rstrip("\n") + "\n" if text else "") + block.lstrip("\n"),
                    encoding="utf-8")
    return block


# ── doctor ────────────────────────────────────────────────────────────────────

def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


# Substrings that show up in a resolved path when it's inside a folder some
# cloud-sync client silently uploads. Heuristic, not exhaustive — catches the
# common cases (OneDrive Known Folder Move, iCloud Drive, Dropbox, Google
# Drive) without needing to detect every sync client by its actual mechanism.
_CLOUD_SYNC_MARKERS = ("onedrive", "icloud", "cloudstorage", "dropbox", "google drive", "googledrive")


def _check_cloud_sync(checks: list, store_dir: Path) -> None:
    resolved = str(store_dir.resolve()).lower()
    hit = next((m for m in _CLOUD_SYNC_MARKERS if m in resolved), None)
    if hit:
        checks.append((
            "cloud sync", False,
            f"store path contains '{hit}' — this folder may silently sync your unencrypted "
            f"store off-device. Set CONTEXT_STORE to relocate it, e.g. outside any synced folder."
        ))
    else:
        checks.append(("cloud sync", True, "store path is not inside a known cloud-sync folder"))


def _check_staleness(checks: list, db, home: Path) -> None:
    from datetime import datetime, timezone
    row = db.conn.execute("""
        SELECT MAX(ts) FROM (
            SELECT created_at AS ts FROM notes
            UNION ALL SELECT started_at FROM sessions
        )
    """).fetchone()
    if not row or not row[0]:
        checks.append(("last activity", True, "no notes or sessions yet"))
        return
    last = datetime.fromisoformat(row[0])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - last).days
    if days > 14:
        checks.append((
            "last activity", False,
            f"{days} day(s) ago — a client is likely configured but the session discipline "
            f"(start_session/write_note/end_session) may not be happening. Check the "
            f"instructions snippet is in the client's rules file."
        ))
    else:
        checks.append(("last activity", True, f"{days} day(s) ago"))


def doctor(store_dir: Path, home: Path) -> list[tuple[str, bool, str]]:
    """Returns (check, ok, detail) tuples."""
    from . import __version__
    from .grants import GrantError, GrantStore

    checks: list[tuple[str, bool, str]] = []
    checks.append(("package", True, f"evoctx {__version__} on Python {sys.version.split()[0]}"))

    # store dir + db
    try:
        store_dir.mkdir(parents=True, exist_ok=True)
        writable = os.access(store_dir, os.W_OK)
        checks.append(("store dir", writable, str(store_dir)))
    except OSError as e:
        checks.append(("store dir", False, f"{store_dir}: {e}"))
        return checks

    try:
        from .db import MIGRATIONS, Database, SchemaTooNew
        try:
            db = Database(store_dir)
            version = db.conn.execute("PRAGMA user_version").fetchone()[0]
            note_count = db.conn.execute("SELECT count(*) FROM notes").fetchone()[0]
            db_size = sum(
                f.stat().st_size for f in store_dir.glob("store.db*") if f.is_file()
            )
            checks.append(("database", True,
                           f"schema v{version}/{len(MIGRATIONS)}, "
                           f"{note_count} notes, {_human_size(db_size)}"))
            try:
                db.conn.execute("SELECT count(*) FROM notes_fts")
                checks.append(("fts5 search", True, "available"))
            except Exception:
                checks.append(("fts5 search", False, "FTS5 missing — search falls back to LIKE"))
            try:
                audit_count = db.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
                checks.append(("audit log", True,
                               f"{audit_count} entries — append-only by design, grows unbounded; "
                               f"evoctx audit-prune --older-than 90d to reclaim space"))
            except Exception:
                pass
            _check_staleness(checks, db, home)
        except SchemaTooNew as e:
            checks.append(("database", False, str(e)))
    except Exception as e:
        checks.append(("database", False, str(e)))

    _check_cloud_sync(checks, store_dir)

    # grants
    try:
        gs = GrantStore(store_dir)
        grant, expires_at, reason = gs.active_grant()
        if grant:
            checks.append(("active grant", True, f"'{grant.name}' expires {expires_at.isoformat()}"))
        else:
            checks.append(("active grant", False, f"{reason} — run: evoctx grant activate everything"))
    except GrantError as e:
        checks.append(("grants.yaml", False, str(e)))

    # server executable
    command, args = server_command()
    checks.append(("server command", Path(command).exists(), f"{command} {' '.join(args)}".strip()))

    # which clients reference the server
    found = []
    for name, client in clients(home).items():
        if client.config_path.exists():
            try:
                config = json.loads(client.config_path.read_text(encoding="utf-8") or "{}")
                if SERVER_NAME in config.get(client.key, {}):
                    found.append(name)
            except (json.JSONDecodeError, OSError):
                pass
    codex_path = home / ".codex" / "config.toml"
    if codex_path.exists() and f"[mcp_servers.{SERVER_NAME}]" in codex_path.read_text(encoding="utf-8"):
        found.append("codex")
    checks.append(("client configs", bool(found),
                   ", ".join(found) if found else "no client config references the server — run: evoctx install <client>"))

    return checks
