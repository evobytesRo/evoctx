# evoctx

A local, personal context store for AI coding assistants, exposed over [MCP](https://modelcontextprotocol.io). By [Evo Bytes](https://evobytes.ro).

Your assistant reads project context at session start, searches past decisions mid-work, and writes notes back before ending — so the next session (in any MCP-capable client) starts oriented instead of from zero. One SQLite store on your machine, shared across every workspace and every client that connects. No cloud component.

> **Status: v0.1.3.** Grants, redaction, audit logging, and sessions all work today. On PyPI — `pip install evoctx`. Still early; expect the occasional breaking change ahead of 1.0. See [CHANGELOG.md](CHANGELOG.md) for what changed in each release.

---

## Install

Requires Python 3.10+.

```
pip install evoctx
```

Installing from source (for contributing, or to track `main` ahead of a release):

```
git clone https://github.com/evobytesRo/evoctx
cd evoctx
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip.exe install -e .
```

Either way, this installs one command: `evoctx`.

- `evoctx <subcommand>` — the CLI for humans. `evoctx help` shows everything.
- `evoctx serve` — launches the MCP server (stdio). Not for humans; your AI client runs this via its MCP config, written for you by `evoctx install` below.

## Making `evoctx` a system command

Right after install, `evoctx` only exists inside the venv — running it from a normal terminal
needs the full path or an activated venv. To get a plain `evoctx` from anywhere:

> **PowerShell users:** a quoted full path followed by arguments needs the call operator `&`,
> or PowerShell parses it as an expression instead of running it:
> `& "C:\path\to\evoctx.exe" install claude-code`. cmd.exe doesn't have this quirk. Easiest
> fix is just to put `evoctx` on `PATH` (below) so you never have to type the full path.

**Recommended: [pipx](https://pipx.pypa.io)** (installs the CLI into its own isolated
environment and puts it on PATH for you, on Windows/macOS/Linux alike):

```
pipx install evoctx               # from PyPI
pipx install -e .                 # from a local clone instead
```

**Alternative: [uv](https://docs.astral.sh/uv/)**, same idea:

```
uv tool install -e .
```

**Manual, no extra tool** — add the venv's executable folder to your `PATH`:

```powershell
# Windows (PowerShell) — persists across terminals
[Environment]::SetEnvironmentVariable("Path", "$env:Path;$PWD\.venv\Scripts", "User")
```

```bash
# macOS / Linux — add to ~/.zshrc or ~/.bashrc
export PATH="$(pwd)/.venv/bin:$PATH"
```

Either way, only the CLI needs a friendly name on `PATH` — MCP client configs reference
the `evoctx` executable by full path (`evoctx install` resolves and writes that path,
plus the `serve` argument, for you).

## Where everything lives

One folder holds it all — run `evoctx doctor` and read the "store dir" line:

| File | What it is |
|---|---|
| `store.db` | The store: notes, projects, sessions, audit log (SQLite) |
| `grants.yaml` | What connected AI clients may do — hand-edited, hot-reloaded |
| `active_grant.json` | Which grant is currently active (written by `evoctx grant activate`) |

Default folder (first match wins):
1. `CONTEXT_STORE` env var, if set
2. `~/.context/` if a store already exists there (legacy)
3. Per-OS user data dir — Windows: `%LOCALAPPDATA%\evoctx`, macOS: `~/Library/Application Support/evoctx`, Linux: `~/.local/share/evoctx`

## First ten minutes

```bash
# 1. See that everything is healthy (also prints where your store lives)
evoctx doctor

# 2. Try it with sample data
evoctx demo-seed
evoctx search "JWT"                  # finds the demo decision note
evoctx get-project demo-webapp      # overview, conventions, open questions

# 3. Register your real project (auto-detects stack from package.json/pyproject/README)
cd ~/code/my-api
evoctx init

# 4. Connect your AI client (run from the project root — claude-code is project-scoped)
evoctx install claude-code          # or: cursor, windsurf, vscode, codex, claude-desktop
#    Prints what to do next for that specific client — for most, restarting isn't
#    enough on its own (VS Code needs a manual Start, Claude Desktop needs a full
#    quit not just a window close, Windsurf has its own per-server toggle). Read
#    the tip it prints, or see docs/clients.md for the full list.

# 5. Paste the instructions snippet into the client's rules file
#    (docs/instructions-snippet.md — this is what makes the AI actually use the store)

# 6. Work normally. Afterwards, see exactly what the AI did:
evoctx audit --since 1h
evoctx recent-sessions
```

From then on: the AI registers a session when it starts, reads your project context, records
decisions as it works, and closes the session with a summary. Next session — same client or a
different one — starts oriented.

## Connecting a client

```
evoctx install claude-code     # run from the project root — see note below
evoctx doctor                  # verify store, grants, server command, client configs
```

`install` writes the MCP block into the client's config (existing file backed up to `.bak`,
existing entries merged, block printed for manual paste), then prints what that specific client
needs beyond a restart to actually pick it up — most of them need something. Same command works
for `claude-desktop`, `cursor`, `windsurf`, `vscode`, `codex`. Manual per-client setup, and the
full list of per-client gotchas: [docs/clients.md](docs/clients.md). ChatGPT Desktop is not
supported — no local stdio MCP.

**Claude Code is project-scoped, not global.** The VS Code extension discovers `.mcp.json` as
the union of the open workspace's **folders** — not from a global user file, and not from the
directory that merely contains those folders (in a multi-root workspace, a file there is
silently ignored; add `{"path": "."}` to the `.code-workspace` `folders` array to cover it).
`evoctx install claude-code` writes to the current directory (`--dir` to target another), warns
if that directory isn't a workspace folder, and needs to run once per folder you want it in. If you also have the standalone `claude` CLI
installed, `evoctx install claude-code` detects it and prints a `claude mcp add --scope user`
command for one-time global registration instead. Full explanation: [docs/clients.md](docs/clients.md).

`doctor` checks package/store/schema/FTS5/grant/server-command/client-configs, plus:
store size and note count, days since the last note or session (a configured client with no
recent activity usually means the instructions snippet isn't actually being followed), and
whether the store folder sits inside OneDrive/iCloud/Dropbox/Google Drive — those sync an
unencrypted SQLite file off-device by default, silently.

**Claude Code only:** `evoctx install claude-code --hooks` additionally writes `SessionStart`
and `Stop` hooks into `~/.claude/settings.json` (backed up first, like every other `install_*`
write) — the one enforcement mechanism in this project that goes beyond convention.
`SessionStart` nudges the assistant to call `start_session`/`recent_sessions`; `Stop` blocks
**once** (never twice — it checks Claude Code's own `stop_hook_active` flag) if a session opened
in the last 12 hours was never closed with `end_session`. It's a nudge, not a lock: ignoring the
message and asking Claude to stop again always succeeds.

Then tell the assistant to actually use it — add to your global instructions file (e.g. `~/.claude/CLAUDE.md`; canonical version in [docs/instructions-snippet.md](docs/instructions-snippet.md)):

```markdown
## MCP Context Store (evoctx)
MUST DO, every session:
- **Start:** call `start_session(project, intent)` first, then `recent_sessions()` — open entries
  were likely interrupted; check their notes. Then `get_project(name)` for anything the task touches.
- **Before any non-trivial decision:** `search_context()` first — don't contradict a stored decision
  without surfacing it.
- **As you go:** `write_note()` for non-obvious decisions/gotchas immediately (auto-linked to the session).
- **End:** call `end_session(summary)` — what was built, decided, and left open.
```

## MCP tools

| Tool | When the assistant uses it |
|---|---|
| `start_session(project?, intent?)` | First call of a session — registers the work session; every `write_note` after is linked to it |
| `recent_sessions(project?, limit?)` | Right after — see what recent sessions did; open ones were likely interrupted, their notes are the only record |
| `list_projects()` | Discover registered projects |
| `get_project(name)` | Load overview, conventions, open questions, recent notes |
| `search_context(query, project?, limit?)` | Find relevant notes mid-work |
| `get_by_id(id)` | Full content after a search hit |
| `write_note(title, content, project?, tags?)` | Record decisions and observations as you go |
| `end_session(summary)` | Last call — closes the session with what was built/decided/open |
| `list_grants()` | What the active grant allows — so "do you have access to X?" gets an honest answer |

Writes are append-only by design: the assistant can add notes but never edit or delete. Deletion is a human-only operation. Note content over 256KB is rejected — split it into multiple notes. Every note also carries `author_client` (`cli` for a human, the client name for an assistant) so past notes read as informational context, not as instructions from the current session.

**Why sessions instead of just an end-of-session note:** a summary written at the end depends on the session surviving to the end. A session registered at the start shows up in `recent_sessions()` even if it crashes one minute in — flagged open, with its intent line and any notes it managed to write. Interrupted work stays visible instead of vanishing.

## CLI (`evoctx`)

Every MCP tool has a CLI mirror — the store is fully human-operable, not just AI-operable:

| MCP tool | CLI |
|---|---|
| `list_projects()` | `evoctx list-projects` |
| `get_project(name)` | `evoctx get-project NAME` |
| `search_context(...)` | `evoctx search-context QUERY [--project NAME] [--limit N]` (alias: `search`) |
| `get_by_id(id)` | `evoctx get-by-id ID` — ID prefix is enough (alias: `show`) |
| `write_note(...)` | `evoctx write-note "Title" --project NAME --tags "#decision" --text "..."` (alias: `add`) |
| `start_session(...)` | `evoctx start-session [--project NAME] [--intent "..."]` |
| `end_session(summary)` | `evoctx end-session "summary" [--id ID]` — default: latest open |
| `recent_sessions(...)` | `evoctx recent-sessions [--project NAME] [--limit N]` |

Plus store management commands with no MCP equivalent (deliberately — deletion and bulk ops are human-only):

```
evoctx init [--dir PATH] [--template NAME]   # register project (auto-detects stack)
evoctx new-project NAME --overview "..."
evoctx import-dir path/to/notes/ --project NAME
evoctx update-project NAME --field conventions --file conventions.md
evoctx list [--project NAME]                  # list notes
evoctx delete ID | --project NAME | --tag TAG # delete note(s) — asks to confirm, or pass --force
evoctx delete-project NAME [--with-notes]     # delete a project (notes kept unlinked by default)
evoctx export [--format json|markdown] [PATH] # export everything — the whole store is yours to take
evoctx dump [--project NAME] [--note ID]      # read-only dump of the whole store
evoctx templates                              # reusable convention templates (_fastapi, ...)
evoctx sync [PATH]                            # sync a markdown folder into the store
evoctx demo-seed                              # sample data to try things out (never automatic)
```

**Deletion and export exist because privacy and portability are only real if you can act on
them.** Deletion is CLI-only, confirmed by default, never an MCP tool — an AI client can add
to the store but never remove from it. Export dumps every project, full note content, and every
session to JSON or Markdown, so "portable across vendors" is a command you can run, not a claim
you have to trust.

## Grants — what a client may do

Every tool call passes a policy gate. Grants live in `grants.yaml` in your store folder
(hand-edited, hot-reloaded — no restart needed); exactly one is active at a time.

First run auto-creates an `everything` grant (all tools, all projects, 30-day expiry) and
activates it, so nothing is broken out of the box. When it expires, calls fail with a message
naming the fix:

```
evoctx grant activate everything
```

To tighten from there, add a scoped grant to `grants.yaml`
(full commented examples: [examples/grants.example.yaml](examples/grants.example.yaml)):

```yaml
  - name: daily-coding
    client: "*"
    tools: [list_projects, get_project, search_context, get_by_id,
            write_note, start_session, end_session, recent_sessions, list_grants]
    scope:
      projects: ["*"]
      exclude_tags: ["#personal", "#finance"]   # these notes become invisible
    redactions:
      - pattern: "sk-[A-Za-z0-9]{20,}"          # mask API keys in every response
        replace: "[API_KEY]"
    expires: "+7d"                               # mandatory — ISO or +Nd/+Nh/+Nm
```

…then switch to it:

```
evoctx grant activate daily-coding    # picked up by a running server immediately
evoctx grant list                     # all grants, active one starred
evoctx grant show daily-coding
evoctx grant deactivate               # every call denied until re-activated
evoctx list-grants                    # what the active grant allows (MCP mirror)
```

A denied call returns an error naming the exact `evoctx grant activate` command to fix it,
so the AI can relay it to you. Scope-blocked notes read as nonexistent (no existence leak);
out-of-scope **writes** fail loudly instead — silent write loss is worse.

## Redaction

Two layers, different jobs:

**Write-time (unconditional, built-in).** Every write — `write_note`, `start_session`,
`end_session`, from the CLI or from an MCP client, regardless of which grant is active —
is scanned for common secret shapes (API key prefixes, AWS/GitHub/Slack/Google tokens,
PEM private key blocks, `password=`/`token=` assignments) and masked *before it touches disk*.
This is the one place every write path funnels through, so a pasted secret doesn't sit raw in
`store.db` waiting for a read-time rule to hide it. Best-effort — regex can't catch every secret
shape or a value the model paraphrases — but it means the store isn't a plaintext secrets
aggregator by default.

**Read-time (per-grant, custom).** Grants can also carry their own regex redactions, applied to
every string leaving the store on a *read* (search hits, note content, project docs, session
summaries — and the audit log's own query field). Placeholders are stable within a session — the
same client name always becomes the same `[CLIENT]` token, distinct values get `[CLIENT_2]`,
`[CLIENT_3]` — so the AI can reason about entities without learning them. The original→placeholder
map lives in memory only and dies with the process; it is never written to disk. This layer is
one-way and doesn't touch what's stored — only what's returned.

## Audit — what did the AI actually see?

Every tool call is logged to an append-only `audit_log` table (SQL triggers block UPDATE/DELETE):
timestamp, client, connection, grant, tool, read/write/denied, the effective (post-redaction)
query, record IDs touched, result count, response size. Response *content* is never logged —
that would duplicate the store.

```
evoctx audit                          # newest first
evoctx audit --client cursor --since 24h
evoctx audit --action denied          # what got blocked, and from whom
evoctx audit --tool get_by_id -n 100
evoctx audit-prune --older-than 90d   # reclaim space — the one deliberate exception to append-only
```

Deliberately **not** an MCP tool: the AI reading its own access record would defeat the mirror
and leak cross-client activity between differently-scoped grants. Human-only, CLI-only.

The log grows unbounded by design — pruning it any other way would mean giving some client a
way to edit its own trail. `evoctx audit-prune` is the single human-only exception: it drops the
append-only triggers, deletes rows older than the cutoff, and restores them immediately.
`evoctx doctor` shows the current row count so growth is visible, not silent.

## Threat model — honest limits

This is policy for cooperative clients, not a sandbox. Client identity is self-reported, the
store is unencrypted, and an AI client with its own filesystem tools can bypass grants entirely
by reading `store.db` or editing `grants.yaml` directly — policy gates the MCP protocol path,
not your disk. Full breakdown, including what to actually do about it (pairing with your
client's own permission system), in **[SECURITY.md](SECURITY.md)**.

## Roadmap — known gaps, not hidden ones

- **Search is keyword-based (SQLite FTS5), not semantic.** It knows word *forms* (porter
  stemming — "configuring" matches "configuration") and a hand-curated dictionary of common
  dev-term synonyms ("auth bug" also matches a note about "login failure" — `auth`/`login`/
  `authentication`, `bug`/`error`/`issue`/`failure`, and similar groups in
  [synonyms.py](src/evoctx/synonyms.py)). It does not know *meaning* — a synonym pair that isn't
  in the dictionary, or two notes that are conceptually related without sharing any recognized
  term, won't connect. Real embedding-based semantic search is the top post-beta priority; it's
  a real dependency (a local embedding model or a vector index) rather than a quick addition,
  which is why the deeper version didn't make beta — the synonym dictionary is the cheap 80% of
  the gain without the dependency weight.
- **Encryption at rest** — not implemented; see [SECURITY.md §4](SECURITY.md).
- **Redaction is one-way.** Grant-based read-time redaction masks values in responses but has no
  client-side re-hydration step to reverse it — once masked, the AI only ever sees the
  placeholder, by design for now.
- **No defense against prompt injection via stored notes** — see [SECURITY.md §2](SECURITY.md).
  This isn't solved by any AI memory system today, evoctx included.
- **Single active grant at a time** — no union semantics across multiple simultaneously active
  grants.

None of these are silent limitations — if one of them matters for your use case, it's listed
here on purpose, not something you're expected to discover by hitting it.

## Layout

```
src/evoctx/
├── server.py    MCP server entry point (FastMCP, stdio)
├── db.py        SQLite schema, FTS5 search, migration runner
├── grants.py    grants.yaml model, validation, expiry, hot-reload
├── policy.py    Policy engine — every tool call passes through here
├── redact.py    Response redaction with stable tokenization
├── audit.py     Append-only audit log + query helpers
├── install.py   Client config writers + doctor checks
├── cli.py       evoctx CLI
└── paths.py     Cross-platform store location resolution
docs/
├── clients.md              Per-client manual setup
└── instructions-snippet.md Canonical rules-file block for every client
examples/
└── grants.example.yaml     Commented grant recipes to copy from
SECURITY.md      What this protects against, what it doesn't, and why
CONTRIBUTING.md  How to contribute, and why your data outlives the project either way
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — includes the exact schema, so your notes are never
locked to this project's code even if it stalls.

## License

[MIT](LICENSE) © Evo Bytes SRL
