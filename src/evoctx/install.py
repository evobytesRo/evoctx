"""Client onboarding — write the MCP server block into a client's config,
and `doctor` checks that verify the whole setup end to end.

Config formats drift between client versions, so every write:
- backs the existing file up to <name>.bak first,
- merges into the existing JSON instead of replacing it,
- prints the block afterwards so it can be pasted manually if anything looks off.
"""
import json
import os
import re
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
        # claude-code is NOT here — see install_claude_code(). The VS Code
        # extension reads MCP servers from project-scoped .mcp.json, not
        # ~/.claude/settings.json's mcpServers key (confirmed by live
        # testing: the settings.json entry was silently never read by a
        # fresh session; the same entry in .mcp.json connected immediately).
        # It doesn't fit this home-directory-file model.
        "claude-desktop": Client("claude-desktop", claude_desktop, "mcpServers", False),
        "cursor": Client("cursor", home / ".cursor" / "mcp.json", "mcpServers", False),
        "windsurf": Client("windsurf", home / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers", False),
        "vscode": Client("vscode", vscode, "servers", True),
        # codex is TOML, handled separately
    }


# Per-client "config written, now what" tips — each learned the hard way,
# from live testing, not from reading that client's source. None of these
# are evoctx bugs; they're facts about how each client discovers/activates
# a newly-written MCP config, undocumented by evoctx until it bit someone.
NEXT_STEPS: dict[str, str] = {
    "claude-code": (
        "Reload the VS Code window (Command Palette -> 'Developer: Reload Window'). Writing "
        ".mcp.json into a window that is already open does nothing until it reloads, and no "
        "error is shown. Then confirm with /mcp in the Claude Code chat.\n"
        "  IMPORTANT: .mcp.json is discovered as the union of the OPEN WORKSPACE'S FOLDERS. A "
        "file in a parent directory does NOT cover child repos unless that parent is itself a "
        "folder in the .code-workspace. Verified 2026-07-30.\n"
        "  Note: an mcpServers block in ~/.claude/settings.json is ignored by the VS Code "
        "extension — only project-scoped .mcp.json registers a server. (Hooks in settings.json "
        "DO apply globally; it is only mcpServers that is inert.)\n"
        "  Then verify the store itself with: evoctx doctor"
    ),
    "claude-desktop": (
        "Restart means fully quit, not just close the window — on Windows, quit from the "
        "system tray icon or the old process (and old config) keeps running, with no error. "
        "Check %APPDATA%\\Claude\\logs\\mcp-server-evoctx.log if the tools don't show up."
    ),
    "cursor": (
        "Restart Cursor, then check Settings -> MCP for evoctx listed as connected."
    ),
    "windsurf": (
        "Restart Windsurf, then open the MCPs icon in Cascade's top-right menu and confirm "
        "evoctx is enabled there — off by default counts as silently not connected. Cascade "
        "also caps at 100 total tools across all enabled servers; trim others if evoctx's "
        "tools don't appear."
    ),
    "vscode": (
        "Writing the config isn't enough — restart VS Code, then open the Command Palette -> "
        "'MCP: List Servers' -> evoctx -> 'Start Server'. Skipping this step shows no error; "
        "the tools just never appear."
    ),
    "codex": (
        "Codex kills any MCP server that doesn't finish starting within startup_timeout_sec "
        "(default 10s) — unlikely to matter for a plain local install, but add "
        "startup_timeout_sec = 30 to the [mcp_servers.evoctx] block if evoctx doesn't connect."
    ),
    "codex-uvx": (
        "Codex kills any MCP server that doesn't finish starting within startup_timeout_sec "
        "(default 10s). The first --uvx run also has to download the package, which easily "
        "blows past that — run `uvx evoctx serve` once manually first so it's cached, or add "
        "startup_timeout_sec = 30 to the [mcp_servers.evoctx] block."
    ),
}


def claude_cli_hint(command: str, args: list[str]) -> str | None:
    """If the standalone `claude` CLI is on PATH, suggest the command for
    real user-scope registration (works in every project automatically).
    Not run automatically: `claude mcp add`'s exact flags aren't something
    this project can verify end-to-end without that CLI installed, and a
    wrong shelled-out command is worse than a suggestion the user can check
    themselves with `claude mcp list` afterward."""
    if not shutil.which("claude"):
        return None
    quoted_args = " ".join(args)
    return (
        f'claude mcp add --scope user --env CONTEXT_CLIENT=claude-code '
        f'evoctx -- "{command}" {quoted_args}'
    )


def install_claude_code(project_dir: Path, uvx: bool = False) -> tuple[Path, dict]:
    """Project-scoped .mcp.json at the given directory — the mechanism actually
    read by the Claude Code VS Code extension.

    The directory must be one of the OPEN WORKSPACE'S FOLDERS, not merely the
    directory containing them: discovery is the union of those folders, and a
    file in a parent is silently ignored (verified 2026-07-30). Callers should
    surface workspace_folder_warning() alongside the write. Must be re-run per
    workspace folder; see claude_cli_hint() for the one-time global alternative
    when the standalone CLI is present."""
    command, args = server_command()
    if uvx:
        command, args = "uvx", ["evoctx", "serve"]
    entry = {"type": "stdio", "command": command, "args": args,
             "env": {"CONTEXT_CLIENT": "claude-code"}}

    path = project_dir / ".mcp.json"
    config: dict = {}
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
        config = json.loads(path.read_text(encoding="utf-8") or "{}")
    path.parent.mkdir(parents=True, exist_ok=True)
    config.setdefault("mcpServers", {})[SERVER_NAME] = entry
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path, entry


_PATH_VALUE_RE = re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"')
_FOLDERS_KEY_RE = re.compile(r'"folders"\s*:\s*\[')

# How far up to look for a .code-workspace before giving up. Bounded so a
# stray workspace file elsewhere on the drive can't produce a bogus warning.
_WORKSPACE_SEARCH_DEPTH = 3


def _strip_jsonc_comments(text: str) -> str:
    """Blank out // and /* */ comments that fall outside string literals.

    Comments are replaced with spaces rather than removed so offsets and line
    structure survive, and so stripping can never fuse two adjacent tokens.

    Without this the scanner below reads comment text as code, which is wrong
    in both directions and was reproduced on all three counts: a `]` inside a
    block comment closed the folders array early; a commented-out
    `"folders": [...]` line stole the key search from the real one; and a
    commented-out `{"path": "legacy"}` entry was reported as a live folder.
    A format whose whole selling point is that comments are legal has to have
    them handled before anything else looks at the text.
    """
    out = list(text)
    i, n = 0, len(text)
    in_string = escaped = False
    while i < n:
        c = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            i += 1
        elif c == '"':
            in_string = True
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] not in "\r\n":
                    out[i] = " "
                i += 1
            for k in range(i, min(i + 2, n)):
                out[k] = " "
            i += 2
        else:
            i += 1
    return "".join(out)


def _folders_array(text: str) -> str | None:
    """The raw text of a .code-workspace "folders" array, brackets excluded.

    Scanned by hand rather than json.loads because .code-workspace is JSONC:
    comments and trailing commas are legal, appear in real files, and make
    json.loads raise on workspaces VS Code opens happily.

    Bracket depth is tracked outside string literals only, so a folder path
    containing [ or ] cannot unbalance the scan.
    """
    m = _FOLDERS_KEY_RE.search(text)
    if not m:
        return None
    depth, i, in_string, escaped = 1, m.end(), False, False
    while i < len(text):
        c = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[m.end():i]
        i += 1
    return None  # unbalanced — treat as unreadable rather than guess


def _workspace_folders(ws: Path) -> list[str]:
    """The "path" values declared in a .code-workspace "folders" array.

    Deliberately scoped to that array. .code-workspace files routinely carry
    "path" keys elsewhere — terminal profiles, task options, launch configs —
    and a whole-file regex is wrong in both directions (both reproduced
    2026-07-30): it advertised "C:\\Program Files\\Git\\bin\\bash.exe" as a
    folder to install into, and a stray '"path": "."' inside a tasks block
    suppressed a warning that should have fired.
    """
    try:
        # utf-8-sig so a BOM doesn't end up glued to the first token
        text = ws.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    block = _folders_array(_strip_jsonc_comments(text))
    if block is None:
        return []
    # JSON escapes survive into the captured value ("C:\\repos" stays doubled).
    # Path() collapses repeated separators so resolve() still lands correctly,
    # but unescape rather than rely on that accident.
    return [p.replace("\\\\", "\\") for p in _PATH_VALUE_RE.findall(block)]


def workspace_folder_warning(project_dir: Path) -> str | None:
    """Warn when .mcp.json is being written where the client will never read it.

    Claude Code discovers .mcp.json as the union of the OPEN WORKSPACE'S
    folders. A file outside those folders — at a repo-group root, say, or in a
    directory that merely contains them — is written successfully and then
    silently ignored, with no error anywhere.

    Verified 2026-07-30: a .mcp.json at a multi-root workspace's parent never
    appeared in /mcp, while the same block inside a listed folder connected
    immediately.

    Returns None when there is nothing to warn about — no .code-workspace
    found (a single-folder window reads that folder's own file), or the target
    is listed by ANY workspace file discovered.

    Every candidate file is examined, not just the first: a directory can hold
    several .code-workspace files, and stopping at the alphabetically-first one
    warned about setups the user's actual workspace handles fine (reproduced
    2026-07-30). Silence wins — one false alarm on the feature whose whole job
    is to be trusted teaches people to ignore it.

    The wording is conditional on purpose. A workspace file existing on disk
    does not prove that workspace is the one currently OPEN; the same folder
    may be open as its own single-folder window, where its .mcp.json is read
    normally.
    """
    project_dir = project_dir.resolve()
    bases = [project_dir, *list(project_dir.parents)[:_WORKSPACE_SEARCH_DEPTH]]

    candidates: list[tuple[Path, list[str]]] = []
    for base in bases:
        for ws in sorted(base.glob("*.code-workspace")):
            rel_paths = _workspace_folders(ws)
            if not rel_paths:
                continue
            folders = {(ws.parent / p).resolve() for p in rel_paths}
            if project_dir in folders:
                return None          # some workspace covers it — say nothing
            candidates.append((ws, rel_paths))

    if not candidates:
        return None

    lines = [
        f"NOTE: {project_dir}",
        "  is not listed as a folder in any .code-workspace found nearby:",
        "",
    ]
    for ws, rel_paths in candidates:
        lines.append(f"    {ws}")
        lines.extend(f"        {ws.parent / p}" for p in rel_paths)
    lines += [
        "",
        "  If you open one of those workspaces, Claude Code will NOT read the .mcp.json",
        "  written here — it fails silently, the server simply never appears. Write it",
        "  into one of the folders listed above instead, or add {\"path\": \".\"} to that",
        "  workspace's \"folders\" array.",
        "",
        "  If you open this directory as its own single-folder window, ignore this —",
        "  the file will be read normally.",
    ]
    return "\n".join(lines)


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
        active, reason = gs.active_grant()
        if active:
            checks.append(("active grant", True,
                           f"'{active.grant.name}' expires {active.expires_at.isoformat()}"))
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
    # claude-code is project-scoped, not under home — checked against the
    # current directory only, since that's the one location doctor can
    # reasonably guess without being told which workspace to look at
    claude_code_path = Path.cwd() / ".mcp.json"
    if claude_code_path.exists():
        try:
            config = json.loads(claude_code_path.read_text(encoding="utf-8") or "{}")
            if SERVER_NAME in config.get("mcpServers", {}):
                found.append("claude-code (this directory)")
        except (json.JSONDecodeError, OSError):
            pass
    checks.append(("client configs", bool(found),
                   ", ".join(found) if found else
                   "no client config references the server here — run: evoctx install <client> "
                   "(claude-code: run from the project root you want it in)"))

    return checks
