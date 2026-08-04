# Client setup

`evoctx install <client>` writes the right config block automatically (with a `.bak` backup
of the existing file). This page shows what it writes, per client, for manual setup or when
a client update moves things around. After any install: restart the client, run `evoctx doctor`,
and add the [instructions snippet](instructions-snippet.md) to the client's rules file.

The `command` below is the `evoctx` executable from your install (run `evoctx doctor`
to see the resolved path), given `serve` to launch the MCP server rather than the CLI.
Once the package is on PyPI, `--uvx` writes `"command": "uvx", "args": ["evoctx", "serve"]`
instead — machine-independent.

## Claude Code

**Project-scoped, not global — and scoped to workspace *folders*, not the workspace root.**
The Claude Code VS Code extension discovers `.mcp.json` as the **union of the open workspace's
folders**. In a single-folder window that's simply that folder. In a multi-root workspace it is
every entry in the `.code-workspace` `folders` array — and a file in the directory that
*contains* those folders is **not** read, even though that's where the `.code-workspace` file
itself usually lives. To cover such a parent, add `{"path": "."}` to the `folders` array so it
becomes a folder in its own right.

**Each declaring folder gets its own server connection**, so a multi-root workspace with five
folders that all declare evoctx opens five of them. Measured 2026-08-04 on Windows: one
connection costs five OS processes — the console-script shim, two venv redirectors, the
launcher and the server — so that workspace is running ~25. They are reclaimed correctly when
the client closes the connection (verified: closing stdin exits the whole chain), and they
share one SQLite store safely under WAL, so this is a memory cost rather than a fault. If it
matters, declare evoctx in fewer folders: one is enough to reach the store, since the store
is machine-wide and not per folder.

It does **not** read `~/.claude/settings.json`'s `mcpServers` key, even though that key looks
like it should be the global mechanism. (Verified 2026-07-30: with a project `.mcp.json`
emptied while `settings.json` still declared the server, the tools were absent from the
session; the same block in a listed folder connected immediately. A bare `~/.claude/.mcp.json`
was also tested and does not work. Note that **hooks** in `settings.json` *do* apply globally —
it is only `mcpServers` that is ignored.)

`evoctx install claude-code` writes to the current directory by default; pass `--dir` to target
a different one, and re-run it once per workspace folder you want evoctx in. It warns if the
target isn't listed by a nearby `.code-workspace`, and names the folders that would work.

Writing into an already-open window does nothing until the window reloads, with no error
shown. Reload, then confirm with `/mcp` in the Claude Code chat.

`.mcp.json` in a workspace folder:

```json
{
  "mcpServers": {
    "evoctx": {
      "type": "stdio",
      "command": "<path-to>/evoctx",
      "args": ["serve"],
      "env": { "CONTEXT_CLIENT": "claude-code" }
    }
  }
}
```

If you also have the standalone `claude` CLI installed (separate from the VS Code extension —
[docs](https://code.claude.com/docs/en/setup)), `claude mcp add --scope user` registers a server
once for every project instead of per-project `.mcp.json` files. `evoctx install claude-code`
prints the exact command when it detects the CLI on `PATH`.

## Claude Desktop

`claude_desktop_config.json` — Windows: `%APPDATA%\Claude\`, macOS:
`~/Library/Application Support/Claude/`, Linux: `~/.config/Claude/`. Same block as above,
without the `"type"` field, `CONTEXT_CLIENT: "claude-desktop"`.

There's no official Linux build — the Linux path above only applies if you're on an unofficial
Linux port. Config is only read at launch, no hot-reload: **restart means fully quit**, not just
close the window — on Windows closing the window leaves the old process (and old config) running
in the system tray, so quit from the tray icon. No error is shown if you skip this; the server
just silently stays on the old config. Logs land at `%APPDATA%\Claude\logs\mcp-server-evoctx.log`
(Windows) if you need to check what actually loaded.

## Cursor

`~/.cursor/mcp.json` — same `mcpServers` block, `CONTEXT_CLIENT: "cursor"`. Cursor also reads a
project-scoped `.cursor/mcp.json` if present, unlike the other clients here — no known conflict
between the two, but if evoctx isn't showing up, check Settings → MCP for a green/enabled status
next to the server.

## Windsurf

`~/.codeium/windsurf/mcp_config.json` — same `mcpServers` block, `CONTEXT_CLIENT: "windsurf"`.
After restart, check the MCPs icon in Cascade's top-right menu — each server has its own
enable/disable toggle there, off by default counts as silently not connected. Cascade also caps
at **100 total tools across all enabled servers**; past that cap, tools quietly stop appearing,
so trim other servers if evoctx's don't show up. On Enterprise plans, MCP may be disabled
workspace-wide until an admin turns it on.

## VS Code (Copilot MCP)

User `mcp.json` — Windows: `%APPDATA%\Code\User\`, macOS:
`~/Library/Application Support/Code/User/`, Linux: `~/.config/Code/User/`.
Note the different top-level key:

**Writing the config isn't enough — VS Code doesn't auto-start it.** After `evoctx install
vscode` (and a restart), open the Command Palette → **MCP: List Servers** → `evoctx` → **Start
Server**. Without this step Copilot just won't see the tools, with no error.

```json
{
  "servers": {
    "evoctx": {
      "type": "stdio",
      "command": "<path-to>/evoctx",
      "args": ["serve"],
      "env": { "CONTEXT_CLIENT": "vscode" }
    }
  }
}
```

## Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.evoctx]
command = "<path-to>/evoctx"
args = ["serve"]
env = { CONTEXT_CLIENT = "codex" }
```

Codex kills any MCP server that doesn't finish starting within `startup_timeout_sec` (default
10s). With `--uvx`, the *first* run also has to download the package — easily blows past 10s and
fails. Run `uvx evoctx serve` once manually first so the package is cached, or add
`startup_timeout_sec = 30` to the `[mcp_servers.evoctx]` block. Codex also supports a
project-scoped `.codex/config.toml` in trusted projects, which can shadow the global one — check
there first if a project behaves differently than expected.

## ChatGPT Desktop

Not supported — it has no local stdio MCP support. Context never touches your machine there;
this is a walled-garden limit, not something this project can bridge.

## Multiple clients, same workspace

Running Copilot MCP and Claude Code (or any combination) in the same workspace at the same
time is fine — each spawns its own `evoctx serve` process against the same store, and SQLite's
WAL mode plus a busy-timeout retry handle the concurrent writes (tested under real multi-handle
write contention, see `tests/test_concurrency.py`). Each process tracks its own session state
independently, so one client ending its session can't touch another's. `CONTEXT_CLIENT` (or the
MCP handshake name as a fallback) tags every note and audit entry with which client wrote it, so
`evoctx audit --client cursor` and friends still separate them out afterward. The default
`grants.yaml` grants every client the same access (`client: "*"`); scope it per-client if you
want Copilot and Claude Code to see different tools or projects. One thing to know before you
do: an enumerated `tools:` list you wrote yourself is **never** auto-upgraded — `evoctx grant
upgrade` only refreshes a grant that listed every tool of an earlier release — so it will
silently miss tools added by future releases. Add them by hand when you want them, or use
`tools: ["*"]` and restrict with `scope:` instead.

## The `CONTEXT_CLIENT` env var

Sets the client name used for grant matching (`client:` globs in `grants.yaml`) and audit
attribution (`evoctx audit --client cursor`). Without it the server falls back to the name
from the MCP handshake, which varies between client versions. Self-reported either way —
attribution, not a security boundary.
