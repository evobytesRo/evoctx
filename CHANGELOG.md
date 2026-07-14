# Changelog

## 0.1.2 — 2026-07-14 — critical fix: MCP server wouldn't start or connect on most setups

Two independent issues, both severe enough to break the MCP connection outright.

**1. Server process wouldn't start on most Windows installs.** `evoctx serve` used
`os.execv(sys.executable, ...)` to hand off from the CLI process to the actual MCP server.
CPython's `os.execv` is broken on Windows whenever `sys.executable` contains a space — and
most Windows install paths have one somewhere (`Program Files`, `OneDrive - Company`, a
username, a folder like this project's own dev path). The failure mode was silent from an
MCP client's point of view: the server process would exit immediately without ever binding
to stdio, so the client just saw a connection that never came up.

Fixed by replacing `os.execv` with `subprocess.run` — spawns the same target
(`python -m evoctx.server`) as a child process instead of replacing the current process
image; stdio is inherited by default so the MCP stdio transport passes through transparently.
Verified against a minimal reproduction fully outside this codebase (confirms it's an
`os.execv`-on-Windows limitation, not evoctx-specific) and against the exact process-spawn
mechanism MCP clients use (`CreateProcess` via a FileName+Arguments invocation, not a shell).

Added `tests/test_serve.py` — there was previously zero test coverage of `cmd_serve` at all,
which is how this shipped in 0.1.0 despite 160 passing tests. Now asserts `subprocess.run` is
used and fails loudly if `os.execv` is ever reintroduced.

**2. `evoctx install claude-code` wrote to a file Claude Code never reads.** It wrote to
`~/.claude/settings.json`'s `mcpServers` key, which looks like the natural "global" location
but is not actually consulted by the Claude Code VS Code extension for MCP server discovery.
Confirmed by live testing: a fresh session with only that entry present showed zero configured
servers; the identical entry placed in a project-scoped `.mcp.json` at the workspace root
connected immediately. A bare `~/.claude/.mcp.json` with no project scope was also tested and
does not work either.

`evoctx install claude-code` now writes `.mcp.json` in the current directory (or `--dir
<path>`) instead, and re-running it once per project is genuinely required — this is not a
workaround, it's how the extension's project scoping actually works. If the standalone
`claude` CLI is also installed, the command now detects it and prints the equivalent
`claude mcp add --scope user` invocation for true one-time global registration. `evoctx
doctor` checks the current directory's `.mcp.json` accordingly.

If you installed 0.1.0 or 0.1.1: `pip install -U evoctx`, then re-run
`evoctx install claude-code` from each project's root.

**3. `evoctx install <client>` now prints what that client needs beyond a restart.**
Every client here turned out to have its own way of silently not picking up a freshly-written
config: VS Code needs a manual Command Palette → "MCP: List Servers" → Start; Claude Desktop
needs a full quit (on Windows, from the system tray — closing the window isn't enough); Windsurf
has a per-server enable toggle in Cascade plus a 100-tool cap across all servers; Codex CLI kills
any server that doesn't start within its 10s `startup_timeout_sec`, which a first `--uvx` run
(which has to download the package) can easily exceed. None of these are evoctx bugs — they're
undocumented facts about each client, found by live testing rather than reading evoctx's own
source. `evoctx install <client>` now prints the relevant tip immediately after writing the
config; the full list is in [docs/clients.md](docs/clients.md).

## 0.1.1 — 2026-07-14 — docs fix

The 0.1.0 README committed on `main` still had the pre-release status banner ("pre-beta,
not yet on PyPI, install from source") — since `pyproject.toml` uses README.md as the PyPI
long_description, that stale text shipped verbatim to the published package page. No code
changes; this release exists solely to correct that: status banner and install instructions
now say what's actually true (`pip install evoctx` works, v0.1.0 is public).

## 0.1.0 — 2026-07-14 — first beta

Initial public release: a local, personal context store for AI coding assistants over MCP.

**Store & tools**
- SQLite store with FTS5 search — porter stemming (word-form matching) plus a hand-curated
  dev-term synonym dictionary ("auth bug" also finds "login failure"); projects, notes, and
  work sessions
- 9 MCP tools: `list_projects`, `get_project`, `search_context`, `get_by_id`, `write_note`,
  `start_session`, `end_session`, `recent_sessions`, `list_grants`
- Sessions registered at start (not just summarized at the end) so an interrupted session still
  surfaces its intent and partial notes instead of vanishing silently
- Every note records `author_client` — which client wrote it, so past notes read as
  informational context rather than as commands from the current session

**Policy engine**
- Grants (`grants.yaml`, hand-edited, hot-reloaded): per-client tool allow-lists, project/tag
  scoping, mandatory expiry
- Write-time secret redaction, unconditional, at the storage layer — common secret shapes are
  masked before they ever touch disk, regardless of entry point or active grant
- Read-time per-grant redaction with stable session-local tokenization
- Append-only audit log (SQL-trigger enforced) recording every tool call, denial, and the
  effective post-redaction query

**CLI (`evoctx`)**
- Full parity with every MCP tool, plus human-only operations an AI client can never call:
  `delete`, `delete-project`, `export` (JSON/Markdown)
- `evoctx install <client>` writes MCP config for Claude Code, Claude Desktop, Cursor,
  Windsurf, VS Code, and Codex CLI
- `evoctx install claude-code --hooks` additionally wires SessionStart/Stop hooks and
  `permissions.deny` rules — the one enforcement mechanism beyond convention
- `evoctx doctor` — store health, schema version, active grant, cloud-sync warning,
  activity-staleness warning, client-config detection

**Safety & portability**
- WAL-mode SQLite for real multi-process concurrency
- Cross-platform store location via `platformdirs`, with a legacy-path fallback for
  pre-packaging installs
- Schema migrations (`PRAGMA user_version`), including a guard against opening a store with a
  newer release than the running code

See [SECURITY.md](SECURITY.md) for what this does and doesn't protect against, and
[CONTRIBUTING.md](CONTRIBUTING.md) for the schema, so your data is never locked to this
project's code.
