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

`~/.claude/settings.json` (global — every workspace):

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

## Claude Desktop

`claude_desktop_config.json` — Windows: `%APPDATA%\Claude\`, macOS:
`~/Library/Application Support/Claude/`, Linux: `~/.config/Claude/`. Same block as above,
without the `"type"` field, `CONTEXT_CLIENT: "claude-desktop"`.

## Cursor

`~/.cursor/mcp.json` — same `mcpServers` block, `CONTEXT_CLIENT: "cursor"`.

## Windsurf

`~/.codeium/windsurf/mcp_config.json` — same `mcpServers` block, `CONTEXT_CLIENT: "windsurf"`.

## VS Code (Copilot MCP)

User `mcp.json` — Windows: `%APPDATA%\Code\User\`, macOS:
`~/Library/Application Support/Code/User/`, Linux: `~/.config/Code/User/`.
Note the different top-level key:

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

## ChatGPT Desktop

Not supported — it has no local stdio MCP support. Context never touches your machine there;
this is a walled-garden limit, not something this project can bridge.

## The `CONTEXT_CLIENT` env var

Sets the client name used for grant matching (`client:` globs in `grants.yaml`) and audit
attribution (`evoctx audit --client cursor`). Without it the server falls back to the name
from the MCP handshake, which varies between client versions. Self-reported either way —
attribution, not a security boundary.
