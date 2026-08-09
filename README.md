# evoctx

A local, personal context store for AI coding assistants, exposed over [MCP](https://modelcontextprotocol.io). By [Evo Bytes](https://evobytes.ro).

Your assistant reads project context at session start, searches past decisions mid-work, and writes notes back before ending — so the next session (in any MCP-capable client) starts oriented instead of from zero. One SQLite store on your machine, shared across every workspace and every client that connects. No cloud component.

> **Status: v0.1.4.** Projects are bound to the directories their code lives in, so the assistant resolves the workspace instead of guessing a name; each client can have its own grant; grants, redaction, audit logging and sessions all work today. On PyPI — `pip install evoctx`. See [CHANGELOG.md](CHANGELOG.md) for what changed in each release.

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
| `active_grant.json` | Which grant is active — machine-wide, plus any [per-client](#a-different-grant-per-client) ones (written by `evoctx grant activate`) |

Default folder (first match wins):
1. `CONTEXT_STORE` env var, if set
2. `~/.context/` if a store already exists there (legacy)
3. Per-OS user data dir — Windows: `%LOCALAPPDATA%\evoctx`, macOS: `~/Library/Application Support/evoctx`, Linux: `~/.local/share/evoctx`

### `CONTEXT_PROJECT`

Names the project a server serves, overriding the directory match. Set it in an MCP config's
`env` block, not in your shell — it is per-workspace, and a shell-level value would follow you
into every workspace on the machine.

A name that does not exist is **refused**, not quietly replaced by the directory match, so a
typo surfaces instead of hiding. Where it surfaces depends on where you set it, and the two do
not overlap: a value in an MCP config reaches the server, so the refusal comes back in
`list_projects()` as `workspace.hint` — ask your assistant what `list_projects()` says. A value
in your shell reaches `evoctx doctor`, which fails on it. `doctor` cannot see an MCP config's
`env` block; nothing reads those back, and having it pick one `.mcp.json` to trust is exactly
the arbitration evoctx deliberately stays out of.

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
which project this directory resolves to and which workspace folders declare the server;
notes or sessions filed under a name that is not a project (they are invisible under the real
one — `evoctx project-merge` moves them), which projects have no directory on this machine,
registered directories that no longer exist, how many notes a scoped grant can no longer read;
your per-client grants and any installed client they leave denied (only once you have some);
a `scope.projects` entry that differs from a real project only by case, which since 0.1.4
matches nothing; store size and note count, days since the last note or session (a configured
client with no recent activity usually means the instructions snippet isn't actually being
followed), the pre-upgrade backups, and whether the store folder sits inside
OneDrive/iCloud/Dropbox/Google Drive — those sync an unencrypted SQLite file off-device by
default, silently.

**Claude Code only:** `evoctx install claude-code --hooks` additionally writes `SessionStart`
and `Stop` hooks into `~/.claude/settings.json` (backed up first, like every other `install_*`
write) — the one enforcement mechanism in this project that goes beyond convention. The same
flag also adds `permissions.deny` rules there, blocking the assistant's own file tools from
reading or editing the store directly ([SECURITY.md §1](SECURITY.md) explains why that matters);
both writes happen together, without a prompt.
`SessionStart` nudges the assistant to call `start_session`/`recent_sessions`; `Stop` blocks
**once** (never twice — it checks Claude Code's own `stop_hook_active` flag) if a session opened
in the last 12 hours was never closed with `end_session`. It's a nudge, not a lock: ignoring the
message and asking Claude to stop again always succeeds.

`Stop` asks only about **this workspace's** sessions — the project the directory resolves to,
plus the other registered repos in the same workspace. **Sibling directories count as one
workspace**, so repos sharing a parent still nag across each other; what stops is a window in
one project tree asking about work in an unrelated one. Sessions started with no project
surface everywhere, and a directory bound to nothing falls back to asking about every open
session, as before. The message names the session's project, since it may not be this folder's.

Then tell the assistant to actually use it — add to your global instructions file (e.g. `~/.claude/CLAUDE.md`; canonical version in [docs/instructions-snippet.md](docs/instructions-snippet.md)):

```markdown
## MCP Context Store (evoctx)
Persistent cross-session memory.

MUST DO, every session:
- **Start:** call `list_projects()` first. It returns `projects` (the names) and a
  `workspace` block naming the project this directory belongs to — **use that name, never
  guess one.** If `workspace.project` is null, nothing matched: show the user
  `workspace.hint`. **Only offer to register when `workspace.suggested_name` is present** —
  its absence means the workspace is not registerable from here (`workspace.source` is
  `CONTEXT_PROJECT`, i.e. a name someone configured that does not exist, or the directory is
  unreadable), and the hint is the whole answer: relay it and stop. **Never invent a name**,
  because a name that does not exist is refused and nothing is saved, so guessing costs you
  the call. When it is present and they agree, call `register_project(name, overview, ...)`:
  read the workspace first (README, manifests, entry points), since a description paraphrased
  from the directory name reads as knowledge to every later session. Set
  `confirmed_by_user=true` **only** if you actually asked in this conversation and they said
  yes — it durably stamps the description as human-approved, and a client that always sends
  it makes every description it writes read that way forever. The server binds
  its own directory — there is no path argument. If `workspace.confidence` is `default`, the
  name is the launching folder's — pass a name explicitly when the work targets one of the
  repos in `workspace.nearby`. Then
  `start_session(project, intent)` — registers the work session so notes get linked and
  interrupted sessions stay visible. **Keep the `id` it returns**; you need it to close this
  session and not somebody else's. It follows the same project rule as `write_note()`: an
  unknown name is **refused**, an unset one takes the workspace's own, and if nothing resolves
  you must name a project or register the workspace — `start_session` has **no `is_global`**,
  since a session happens somewhere. Then `recent_sessions(project)` — open entries were
  likely interrupted, check their notes. Then `get_project(name)` for anything the task touches.
- **Before any non-trivial decision:** `search_context()` for the topic first — don't propose
  an approach that contradicts a stored decision without surfacing the conflict. **Query with
  two or three bare content words, never a sentence:** terms combine with AND, so each extra
  word can only shrink the result. When AND turns up fewer than a handful the store retries
  with OR and then with prefixes, so let it widen rather than rephrasing by hand. Titles,
  bodies **and tags** are indexed and word forms match (`migrating` finds `migration`);
  substrings never do. Quotes, parens, `OR`, `NOT`, `NEAR` and a trailing `*` are honoured as
  written and turn the widening off. **Results are ranked by relevance, not by date** — read
  `created_at` when recency is what matters. The response carries `total`, the number of notes
  that matched, which is **not** the number returned: when `has_more` is true and the topic
  matters, raise `limit` or page with `offset` rather than assuming you have seen everything.
  **Raising `limit` is the reliable way to see more** — one call is one consistent set. If you
  page with `offset` instead, pass the previous response's `page_token` back so both pages read
  the query the same way; paging is positional, so on a store another window is writing to it
  samples rather than guarantees completeness. A `total` of 0 means nothing matched *that you
  can see* — an active grant can be filtering it, so check `list_grants()` before telling the
  user the store has nothing on the topic.
- **As you go:** `write_note()` for non-obvious decisions/gotchas immediately, don't batch to
  the end (notes are auto-linked to the session). A project name that doesn't exist is
  **refused and nothing is saved** — leave `project` unset to use the workspace's own, and
  if nothing resolves, name one, register the workspace, or set `is_global`.
- **End:** call `end_session(summary, session_id=<the id from start_session>)` — what was
  built, decided, and left open. **Pass the id.** Several assistant windows share one store, so
  the newest open session is not reliably yours. If the response carries `other_open_sessions`,
  that is an observation about other windows, not a task: never close a session you did not
  open unless the user says it was abandoned, because a summary of work it never did is worse
  than leaving it open.
- If a call is denied, call `list_grants()` and relay the `evoctx` command from the error **as
  written** — it may name a grant that applies to you specifically, and a generic
  `grant activate everything` would leave you denied. Anything in `<angle brackets>` is a
  placeholder only the user can fill: say so, and never fill one in yourself, because what it
  stands for is a decision about your own access.
```

## MCP tools

| Tool | When the assistant uses it |
|---|---|
| `start_session(project?, intent?)` | First call of a session — registers the work session; every `write_note` after is linked to it. Same project rule as `write_note` |
| `recent_sessions(project?, limit?)` | Right after — see what recent sessions did; open ones were likely interrupted, their notes are the only record |
| `list_projects()` | Discover registered projects, and which one this workspace is — returns `projects` plus a `workspace` block that resolves the working directory to a project, or says plainly that nothing matched |
| `get_project(name)` | Load overview, conventions, open questions, recent notes |
| `search_context(query, project?, limit?, offset?, page_token?)` | Find relevant notes mid-work. Two or three bare words: terms combine with AND, and the store widens to OR and then to prefixes on its own when AND turns up too little. Title, body and tags are all indexed. Returns `{results, total, offset, limit, has_more, page_token}` — `total` is how many matched, not how many came back, so the assistant can tell a complete answer from a page of one and ask for the rest; `page_token` passed back keeps both pages reading the query the same way. Ranked by relevance, **not** by date |
| `get_by_id(id)` | Full content after a search hit |
| `write_note(title, content, project?, tags?, is_global?)` | Record decisions and observations as you go. An unknown project name is refused and nothing is saved; an unset one takes the workspace's own. `is_global` marks a note as spanning every project — deliberately not the same as leaving `project` unset |
| `end_session(summary, session_id?)` | Last call — closes the session with what was built/decided/open. Pass the id `start_session` returned; several windows share one store, so the newest open session is not reliably yours. Returns `other_open_sessions` when any remain, as an observation about other windows rather than a task |
| `register_project(name, overview, ...)` | Register this workspace as a project. No path argument — the server binds its own working directory; the assistant supplies only the description |
| `list_grants()` | What *this client* is allowed — resolved per client, since one may have its own grant — so "do you have access to X?" gets an honest answer |

Writes are append-only by design: the assistant can add notes but never edit or delete. Deletion is a human-only operation. Note content over 256KB is rejected — split it into multiple notes. Every note also carries `author_client` (`cli` for a human, the client name for an assistant) so past notes read as informational context, not as instructions from the current session.

**Why sessions instead of just an end-of-session note:** a summary written at the end depends on the session surviving to the end. A session registered at the start shows up in `recent_sessions()` even if it crashes one minute in — flagged open, with its intent line and any notes it managed to write. Interrupted work stays visible instead of vanishing.

## CLI (`evoctx`)

Every MCP tool has a CLI mirror — the store is fully human-operable, not just AI-operable:

| MCP tool | CLI |
|---|---|
| `list_projects()` | `evoctx list-projects` |
| `register_project(...)` | `evoctx init [--name NAME]` |
| *(none — human-only)* | `evoctx project-merge WRONG RIGHT` — move notes **and** sessions off an invented project name |
| `get_project(name)` | `evoctx get-project NAME` |
| `search_context(...)` | `evoctx search-context QUERY [--project NAME] [--limit N] [--offset N] [--page-token TOK]` (alias: `search`) — reports total matches, not just the page, and prints the next page's command |
| `get_by_id(id)` | `evoctx get-by-id ID` — ID prefix is enough (alias: `show`) |
| `write_note(...)` | `evoctx write-note "Title" --project NAME --tags "#decision" --text "..."` (alias: `add`); `--global` marks a note as spanning every project |
| `start_session(...)` | `evoctx start-session [--project NAME] [--intent "..."]` |
| `end_session(summary, session_id?)` | `evoctx end-session "summary" [--id ID]` — default: latest open; an ID prefix needs at least 8 characters, since closing has no undo |
| `recent_sessions(...)` | `evoctx recent-sessions [--project NAME] [--limit N]` |

Plus store management commands with no MCP equivalent (deliberately — deletion and bulk ops are human-only):

```
evoctx init [--dir PATH] [--template NAME]   # register project + bind the directory to it
            [--rescrape] [--relink] [--yes]   # replace description / move the directory / skip the prompt
evoctx unbind [--dir PATH] [--project NAME]   # remove a directory binding (the project stays)
evoctx new-project NAME --overview "..."
evoctx import-dir path/to/notes/ --project NAME
evoctx update-project NAME --field conventions --file conventions.md
evoctx list [--project NAME]                  # list notes
evoctx delete ID | --project NAME | --tag TAG # delete note(s) — asks to confirm, or pass --force
evoctx delete-project NAME [--with-notes]     # delete a project (notes kept unlinked by default)
evoctx export [--format json|markdown] [PATH] # export everything — the whole store is yours to take
evoctx dump [--project NAME] [--note ID]      # read-only dump of the whole store
evoctx templates                              # reusable convention templates (a fresh store
                                              # has none; `evoctx sync <repo>` loads templates/)
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
(hand-edited, hot-reloaded — no restart needed); one is active machine-wide, and since 0.1.4
[a client can have its own](#a-different-grant-per-client).

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
            write_note, start_session, end_session, recent_sessions,
            list_grants, register_project]
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

A denied call returns an error naming the exact command to fix it — `evoctx grant activate`
for a grant that is too narrow, `evoctx grant upgrade` for one written before the tool
existed — so the AI can relay it to you. Scope-blocked notes read as nonexistent (no existence leak);
out-of-scope **writes** fail loudly instead — silent write loss is worse.

### A different grant per client

The point of installing several assistants is often to give them different access — read-only
for one, full write for another. Since **0.1.4** each client can have its own grant:

```
evoctx grant activate read-only --client "cursor*"    # cursor gets this one
evoctx grant activate everything                      # everyone else gets this one
evoctx grant list                                     # both, side by side
evoctx grant deactivate --client "cursor*"            # cursor falls back to the default
```

The client name is what the client reports about itself — `install` writes it into each
config as `CONTEXT_CLIENT`, and `evoctx doctor` lists which ones are installed. The
`--client` argument is a glob matched against it, and matching is case-insensitive: `Cursor`
and `cursor` mean the same key, so `activate` refuses to create the second, and if both end
up in the file by hand the call is denied rather than resolved by guessing which you meant.

Four rules worth knowing before you rely on it:

- **A per-client grant wins.** The machine-wide grant is the fallback for clients that have
  no entry of their own.
- **When one expires, that client is denied — it does not fall back.** Falling back would
  silently promote a client from its lapsed narrow grant to the wider machine-wide one, at
  exactly the moment the restriction was supposed to take effect.
- **`evoctx grant deactivate` with no `--client` clears everything**, per-client entries
  included. It is the kill switch; leaving entries active would defeat it.
- **Which grant applies is decided per client, not per file order.** An exact key beats a
  glob; between globs the more specific one wins; two equally specific keys are ambiguous
  and the call is denied rather than resolved by guesswork.

`evoctx grant list` and `evoctx doctor` both report the entries, which installed clients are
running on the fallback, and any client that is denied — including one caught by that
ambiguity. They answer for the clients you actually have installed, since whether two globs
can both match *something* is not a question with a cheap answer; whether they both match
`claude-code` is. claude-code's config is workspace-scoped, so run `doctor` inside the
workspace you want checked.

`grant activate --client` exits non-zero when what you asked for would not work — an expired
grant, or a client the key names that would still be denied — and **puts the store back the
way it was**, so a non-zero exit never leaves a grant applied. What it does *not* fail on is
an exact key winning over a broader glob you just wrote: that is the documented precedence
rule, so it says which clients kept their own entry and how to bring them in line. No message
here ever suggests deleting an entry to fix a denial — removing one hands its client back to
the wider machine-wide grant, which is the opposite of what a narrow entry was for. For two
globs that tie, the fix is an addition: giving that client an entry of its own outranks both
and settles it without deleting either rule you wrote.

**Two keys differing only in case are the one thing no command can fix**, and evoctx says so
rather than pretending otherwise. `Cursor` and `cursor` are the same key, so the entry that
would outrank them already exists — twice — and `grant activate` refuses every spelling by
pointing at the other one. Open `active_grant.json` and delete one; the denial names both
keys and the grant behind each, so you can see what you are choosing between. `grant
activate` will not let you create that state, but a hand edit or a merge can.

⚠️ **One-way door across versions.** An evoctx older than 0.1.4 running `grant activate` on
this store rewrites `active_grant.json` and **silently erases every per-client entry** — it
does not know they exist. Nothing on this side can detect it after the fact. If you sync
your store between machines, upgrade all of them.

## Redaction

Two layers, different jobs:

**Write-time (unconditional, built-in).** Every write — `write_note`, `start_session`,
`end_session`, and since 0.1.4 project descriptions too (`register_project`, `evoctx init`,
`update-project`), from the CLI or from an MCP client, regardless of which grant is active —
is scanned for common secret shapes (API key prefixes, AWS/GitHub/Slack/Google tokens,
PEM private key blocks, and `password=` / `api_key=` / `access_token=` assignments) and masked *before it touches disk*.
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
  stemming — "configuring" matches "configuration"), a hand-curated dictionary of common
  dev-term synonyms ("auth bug" also matches a note about "login failure" — `auth`/`login`/
  `authentication`, `bug`/`error`/`issue`/`failure`, and similar groups in
  [synonyms.py](src/evoctx/synonyms.py)), and it widens AND → OR → prefixes when a query
  turns up fewer than a handful of matches. It does not know *meaning* — a synonym pair that isn't
  in the dictionary, or two notes that are conceptually related without sharing any recognized
  term, won't connect. Real embedding-based semantic search is the top post-beta priority; it's
  a real dependency (a local embedding model or a vector index) rather than a quick addition,
  which is why the deeper version didn't make beta — the synonym dictionary is the cheap 80% of
  the gain without the dependency weight.
- **No substring search, in any mode.** FTS5 matches whole terms and prefixes only, so
  `letter` will never find `newsletter`. This is a property of the index, not a setting;
  the LIKE path that *can* match inside a word runs only when FTS5 refuses to parse a query,
  and is an error path rather than a search mode.
- **Encryption at rest** — not implemented; see [SECURITY.md §4](SECURITY.md).
- **Redaction is one-way.** Grant-based read-time redaction masks values in responses but has no
  client-side re-hydration step to reverse it — once masked, the AI only ever sees the
  placeholder, by design for now.
- **No defense against prompt injection via stored notes** — see [SECURITY.md §2](SECURITY.md).
  This isn't solved by any AI memory system today, evoctx included.
- **One grant per client, with no union semantics.** Since 0.1.4 each client can have its own
  grant, but only one applies at a time — there is no combining several into a wider set of
  permissions, and two equally specific per-client keys are refused rather than merged.

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
