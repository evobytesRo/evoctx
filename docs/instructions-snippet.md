# Instructions snippet

Getting an AI to actually use evoctx takes **two separate things**, and confusing them costs
hours:

1. **Registration** — the client must start the server. Mechanism differs per client and is
   not always where you'd guess.
2. **Instruction** — the model must be told to call the tools. Registration alone does
   nothing; MCP is pull-based and a connected server sits idle until something asks for it.

Get (1) wrong and the tools are absent. Get (2) wrong and they're present and ignored. Both
fail silently.

---

## 1. Registration

### Claude Code (VS Code extension)

**Project-scoped `.mcp.json`, discovered as the union of the open workspace's folders.**
`evoctx install claude-code` writes it into the current directory.

Three things about this, all verified 2026-07-30 and each one previously a wrong guess:

- **A parent directory's `.mcp.json` does NOT cover child repos.** If your `.code-workspace`
  lists `repos/api` and `repos/web`, a file at `repos/` is never read. It must live in a
  listed folder — or add `{"path": "."}` to the workspace's `folders` array to make the
  parent a folder in its own right. `evoctx install claude-code` warns when it detects this.
- **An `mcpServers` block in `~/.claude/settings.json` is ignored.** Proven by emptying a
  project `.mcp.json` while `settings.json` still declared the server: the tools were absent
  from the session. **Hooks in that same file DO apply globally** — it is only `mcpServers`
  that is inert.
- **Writing `.mcp.json` into an already-open window does nothing until the window reloads**,
  with no error shown. Reload, then confirm with `/mcp` in the chat.

`/mcp enable|disable` works in the terminal CLI only; in the extension the panel is read-only
status (verified 2026-07-30). To change which servers a window sees, edit `.mcp.json` and reload.

### Other clients

`evoctx install <client>` writes a home-directory config: `claude-desktop`, `cursor`,
`windsurf`, `vscode`, `codex`. Each prints its own post-install step — read it, they are all
things that bit someone.

---

## 2. Instruction

Paste this into your client's global instructions file. Same wording everywhere — don't let
the ritual drift between clients.

| Client | File |
|---|---|
| Claude Code | `~/.claude/CLAUDE.md` |
| Cursor | Settings → Rules for AI (or `.cursor/rules/` in a repo) |
| Windsurf | `~/.codeium/windsurf/memories/global_rules.md` |
| VS Code (Copilot) | `.github/copilot-instructions.md` — see note below, this alone does not enable the tools |
| Codex CLI | `~/.codex/AGENTS.md` |

**VS Code (Copilot) extra steps:** registering the server + pasting the snippet is not enough.

1. `evoctx install vscode` already writes `env.CONTEXT_CLIENT = "vscode"` into the user-scope
   `mcp.json` (`%APPDATA%\Code\User\mcp.json`) — no manual edit needed. VS Code will also read
   a per-workspace `.vscode/mcp.json` if you create one by hand; `evoctx install` never writes
   that one. It's what makes sessions/notes attribute correctly instead of falling back to
   whatever Copilot Chat's MCP handshake self-reports (observed 2026-07-15: `"claude-code"`,
   not accurate).
   Only gotcha: `mcp.json` doesn't hot-reload an already-running connection — if you install
   (or hand-edit) while VS Code is open, reload the window or restart the MCP connection, or
   the old process keeps running without the new env var.

2. In Copilot Chat's tool picker you must also: enable the evoctx tool, trust it, and set it to
   "always allow" (globally or per-workspace). Skip this and calls stay gated behind manual
   approval or never fire.

```markdown
## MCP Context Store (evoctx)
Persistent cross-session memory.

MUST DO, every session:
- **Start:** call `list_projects()` first. It returns `projects` (the names) and a
  `workspace` block naming the project this directory belongs to — **use that name, never
  guess one.** If `workspace.project` is null, nothing matched: show the user
  `workspace.hint` and offer to register this workspace — **never invent a name**, because a
  name that does not exist is refused and nothing is saved, so guessing costs you the call.
  If they agree, call `register_project(name, overview, ...)`: read the workspace
  first (README, manifests, entry points), since a description paraphrased from the directory
  name reads as knowledge to every later session. The server binds its own directory — there
  is no path argument, and `workspace.suggested_name` is the name to propose. If
  `workspace.confidence` is
  `default`, the name is the launching folder's — pass a name explicitly when the work
  targets one of the repos in `workspace.nearby`. Then
  `start_session(project, intent)` — registers the work session so notes get linked and
  interrupted sessions stay visible. It follows the same project rule as `write_note()`: an
  unknown name is **refused**, an unset one takes the workspace's own, and if nothing resolves
  you must name a project or register the workspace — `start_session` has **no `is_global`**,
  since a session happens somewhere. Then `recent_sessions(project)` — open entries were
  likely interrupted, check their notes. Then `get_project(name)` for anything the task touches.
- **Before any non-trivial decision:** `search_context()` for the topic first — don't propose
  an approach that contradicts a stored decision without surfacing the conflict.
- **As you go:** `write_note()` for non-obvious decisions/gotchas immediately, don't batch to
  the end (notes are auto-linked to the session). A project name that doesn't exist is
  **refused and nothing is saved** — leave `project` unset to use the workspace's own, and
  if nothing resolves, name one, register the workspace, or set `is_global`.
- **End:** call `end_session(summary)` — what was built, decided, and left open.
- If a call is denied, call `list_grants()` and relay the `evoctx` command from the error **as
  written** — it may name a grant that applies to you specifically, and a generic
  `grant activate everything` would leave you denied. Anything in `<angle brackets>` is a
  placeholder only the user can fill: say so, and never fill one in yourself, because what it
  stands for is a decision about your own access.
```

---

## Honest limits

A local MCP server cannot force a client to call its tools. Four things stand between a
running server and a tool call, in decreasing strength:

1. **Client hooks** (Claude Code `SessionStart`/`Stop`) — the only hard mechanism, and
   client-specific. `evoctx install claude-code --hooks`.
2. **The server's `instructions` field** — injected automatically into clients that support
   it; advisory. Necessary, nowhere near sufficient: a server whose instruction is *"call
   `initial_instructions` to read the manual"* was observed being skipped entirely, because
   at the moment the model read it, the tool wasn't loaded and its value was unknown.
3. **This snippet in the rules file** — convention. Followed most of the time, not always.
4. **Nothing outranking it.** This is the one people miss. Instructions compete, and the
   **more specific instruction wins**. A project-level rules file saying *"use these grep
   recipes instead of broad searches"* will beat a global rule pointing at an MCP tool,
   because it is concrete and local. If a tool is going unused despite being registered and
   instructed, look for a competing instruction before blaming the model.

**Also: tools may be deferred.** When tool definitions exceed a token threshold, Claude Code
withholds their schemas and offers a `ToolSearch` tool instead — the model must discover them
before it can call anything. A server can therefore be connected, trusted, and instructed, and
still never be reached. Set `ENABLE_TOOL_SEARCH=false` (or `"enable_tool_search": false` in
settings) to load definitions eagerly. Verified 2026-07-30.

Ship every layer available to you; promise none of them as "enforced."
