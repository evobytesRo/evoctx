# Instructions snippet

Registering the MCP server makes the tools *available*; it does not make the AI *use* them.
Paste this block into your client's global instructions file so every session follows the
same discipline. Same wording everywhere — don't let the ritual drift between clients.

Where it goes:

| Client | File |
|---|---|
| Claude Code | `~/.claude/CLAUDE.md` |
| Cursor | Settings → Rules for AI (or `.cursor/rules/` in a repo) |
| Windsurf | `~/.codeium/windsurf/memories/global_rules.md` |
| VS Code (Copilot) | `.github/copilot-instructions.md` |
| Codex CLI | `~/.codex/AGENTS.md` |

```markdown
## MCP Context Store (evoctx)
Persistent cross-session memory.

MUST DO, every session:
- **Start:** call `start_session(project, intent)` first — registers the work session so notes
  get linked and interrupted sessions stay visible. Then `recent_sessions(project)` — open
  entries were likely interrupted, check their notes. Then `get_project(name)` for anything
  the task touches.
- **Before any non-trivial decision:** `search_context()` for the topic first — don't propose
  an approach that contradicts a stored decision without surfacing the conflict.
- **As you go:** `write_note()` for non-obvious decisions/gotchas immediately, don't batch to
  the end (notes are auto-linked to the session).
- **End:** call `end_session(summary)` — what was built, decided, and left open.
- If a call is denied, call `list_grants()` and relay the `evoctx` command from the error to
  the user.
```

## Honest limits

A local MCP server cannot force a client to call its tools — MCP is pull-based. Three layers,
in decreasing strength:

1. **Client hooks** (Claude Code `SessionStart`/`Stop` hooks) — the only hard mechanism, client-specific.
2. **The server's `instructions` field** — injected automatically into clients that support it; advisory.
3. **This snippet in the rules file** — convention. Models follow it most of the time, not always.

Ship all three where available; promise none of them as "enforced."
