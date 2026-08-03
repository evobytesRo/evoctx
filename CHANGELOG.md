# Changelog

## 0.1.4 — unreleased — projects know where they live

**1. A project can now record the directories its code is in.** Until now the store had no
idea where anything was: `evoctx init` resolved a directory, read manifests out of it,
printed it, and dropped it. So an assistant asked to name a project had nothing to match its
workspace against, and could only infer a name from context — which is where invented
project names came from. Not carelessness; an API that offered no other option.

Migration 005 adds `project_roots` (project, root_path, machine). A table rather than a
column because one project legitimately has several roots — a worktree, a work machine and
a home one, a monorepo subdirectory — and a column would force last-write-wins and drop the
earlier path silently. **Path is a hint, never the identity**: the name stays the key, so a
cloud-synced store, a clone, and a project with no directory at all all keep working.

Uniqueness is per machine: on a given machine a directory belongs to at most one project,
but the same absolute path on a different machine may legitimately be a different project.

**2. `evoctx init` on an existing project now links the directory instead of refusing.**
Before, it printed "already registered, use --force" — and `--force` replaces overview and
conventions with scraped output. Recording where the code lives should never cost a
description someone wrote by hand, so linking no longer goes through it.

`--relink` moves a directory that another project already claims; without it, that case
refuses and names both ways out. It asks first, and `--yes` skips the question — **not**
`--force`, which means "re-scrape the description" here. Letting one flag do both jobs made
`--relink --force` move a directory *and* overwrite a curated description when the user had
explicitly asked for neither. `evoctx unbind` removes a binding outright, for when `init`
was run one directory too deep.

**3. Descriptions carry provenance.** `projects.description_author` records who wrote the
overview. Notes have had `author_client` since 0.1.0 for the same reason; projects being
the one unattributed surface meant an assistant-written overview read identically to a
curated one, forever, to every future session. Text scraped by `evoctx init` from manifests
and READMEs is attributed to `init-scrape`, not to you — nobody wrote it.

`description_reviewed_at` and `attention_dismissed_at` exist alongside it as schema only.
Nothing writes either yet — the review writer arrives with assistant-side registration,
where a human confirms a proposed description, and the dismissal writer with the
incomplete-project prompt. They are documented here so the columns are not mistaken for
working features: today the first answers "no human has approved this" for every project,
which is true but not yet informative. The clearing rule *is* live: any write that changes
the overview or conventions clears the review, including the author's own, because a review
approves specific words. Text that does not change keeps it.

**4. A global note is now explicitly global.** `project=None` meant two different things the
store could not tell apart: *"the user asked for a note that spans everything"* and
*"nobody set a project"*. `notes.is_global` records which. Storage stays `NULL` — a sentinel
project name would have changed grant semantics, since `allows_project(None)` passes on its
own branch while any string goes through glob matching, so a narrow grant would have started
hiding global notes that pass today. `evoctx sync` also already creates a real project
called `global`, so the sentinel would have collided with a shipped feature.

**5. Deleting a project deletes its roots.** Left behind, they would resolve a directory to
a project that no longer exists and make the directory un-registerable.

**6. Response shapes changed — check this if you script against evoctx.** All additive, but
worth knowing before you upgrade:

- `evoctx export --format json` gains a top-level `project_roots` key. **An export now
  describes your machine's directory layout** — absolute paths, tagged with the machine
  they were true on. Worth knowing before you share one.
- `get_project` gains `roots`, `description_author`, `description_reviewed_at` and
  `attention_dismissed_at` — over MCP, to every client
- note responses (`search_context`, `get_by_id`) gain `is_global`
- **a project-scoped read now returns global notes too.** `search_context(project="api")`,
  `get_project("api")` and `evoctx list --project api` include notes marked global, because
  "spans every project" has to mean something. This is the one change here that alters what
  an existing call returns rather than only adding keys. A project's own notes are never
  displaced by them — `recent_notes` bounds each set separately, so five global notes
  cannot push out everything the project has.
- `evoctx add --global` marks a note cross-project from the CLI, matching what the
  `write_note` MCP tool can now do. Every MCP capability gets a CLI mirror; without it the
  assistant would be the only one able to express the distinction this release adds.
  `evoctx list`, `show`, `search` and `dump` print `[global]` where they used to print a
  dash, so the two cases are distinguishable by eye and not only in the API. Bracketed
  because `evoctx sync` creates a real project *named* `global` — an unbracketed marker
  would rebuild the same conflation on the display side.
- **A grant scoped to specific projects can no longer write a global note.** One that spans
  every project appears in every project's scoped read, so claiming that scope now requires
  holding it (`projects: ["*"]`). Writing a note with no project at all is unaffected. If a
  client hits this, the denial names the fix: activate a wider grant.
- `evoctx doctor` gains three checks: registered directories that are no longer present,
  notes marked both global and project-scoped, and the pre-upgrade backups described below
  (so you know they exist and that they are yours to delete).
- **A grant whose `scope.projects` is present but empty is now rejected**, in either
  spelling — `projects: []` or a bare `projects:`. It previously parsed as *every* project,
  which is a scope failing open, and it now also decides who may write a global note. If you
  have hand-edited `grants.yaml` this way, fix it before upgrading: grant parsing runs
  inside every tool call, so a rejected file denies all of them.
- **Upgrading is one-way, and now backs itself up first.** Once migrated, an older evoctx
  refuses to open the store — correct, but irreversible, and this store is built to be
  synced between machines that may not upgrade together. The migration writes
  `store.db.bak-pre-migration-005` before touching anything, via SQLite's backup API rather
  than a file copy (in WAL mode the file alone is missing whatever is still in the `-wal`).


## 0.1.3 — 2026-07-30 — forward-compatible grants, install guidance that matches reality, first pyright pass, deterministic ordering

**1. A future release could have locked users out of their own store.** `_parse_grant`
raised `GrantError` on any tool name not in `ALL_TOOLS`. That parse runs inside
`grants()` → `active_grant()` → `PolicyEngine.check()`, so the moment any newer version
wrote a new tool name into `grants.yaml`, an older build reading that file would fail
**every tool call** — not just the unrecognised one. The trigger was inevitable: adding a
tool is a normal thing to do, and stores are shared across versions via rollback and
cloud-synced home directories. 0.1.2 cannot be retro-fixed, so this only protects
0.1.3-and-later from releases after it.

Now: unknown names are reported on stderr and dropped. Dropping rather than keeping is
deliberate — this build cannot serve those tools, so retaining them would make
`effective_tools()`, and therefore `list_grants()`, advertise something unservable.
`grants.yaml` on disk is never rewritten, so upgrading restores them automatically.

**2. `tools: ["*"]` is now supported** — a grant that never goes stale as tools are added.
`Grant.effective_tools()` expands it for responses so `list_grants()` and `evoctx grant
list/show` stay concretely answerable instead of reporting `["*"]`.

**Deliberately NOT the default.** 0.1.2 rejects `"*"` exactly as it rejects any unknown
name, so shipping it as the default would create the very lockout this release removes,
pointed backwards: a fresh 0.1.3 store reaching a 0.1.2 machine would fail every call
there. The default stays enumerated; switch to `"*"` by hand once every machine sharing
your store is on 0.1.3+.

**3. `evoctx install claude-code` printed the wrong post-install guidance** — `NEXT_STEPS` had
no `claude-code` key, so the most-used install path fell through to the generic
"Restart the client" tail, which is wrong for a VS Code extension. Writing `.mcp.json` into an
already-open VS Code window does nothing until the window reloads, and shows no error.

**4. New: warning when `.mcp.json` is written where the client will never read it.**
Claude Code discovers `.mcp.json` as the union of the **open workspace's folders**. A file
at a repo-group root — a directory that merely *contains* the repos — is written
successfully and then silently ignored. `evoctx install claude-code` now inspects nearby
`.code-workspace` files and names the folders that would actually work.

**5. `evoctx grant activate` crashed on an already-expired grant.** Activating a grant
whose `expires` is already in the past (`"+0m"`, or an absolute timestamp that has passed)
wrote `active_grant.json` successfully, then died with an `AttributeError` printing the
expiry — because the store reports an expired grant as *no* grant. It now says the grant
is not usable, names the reason and the file to fix, and exits non-zero, so a script that
activates a grant can treat exit 0 as "calls will work".

Underneath, `GrantStore.active_grant()` no longer returns three independent values
`(grant, expires_at, reason)` — the grant and its expiry are one `ActiveGrant` object, so
they can no longer be checked and dereferenced separately. That uncorrelated tuple was the
whole cause: every code path that returned a grant also returned an expiry, but nothing
said so, and four call sites dereferenced the expiry on the strength of the grant.

**6. First pyright run on the repo — 26 errors, now 0**, with the configuration pinned in
`pyproject.toml` so the result is reproducible rather than dependent on each person's flags.
Most were type-level noise; two were real and are fixed above (the crash in 5, plus
`evoctx dump` skipping instead of crashing on a project deleted mid-listing). Worth
recording that 186 passing tests and four adversarial review rounds all ran green over a
latent crash that a type checker found in three seconds.

**7. Queries that page or truncate now pick deterministically — or refuse.** Several
didn't, and the consequences ranged from cosmetic to deleting the wrong note.

The one with teeth: `latest_open_session()` ordered by `started_at` alone. `datetime.now()`
resolution on Windows is coarse enough that two sessions created back to back land on the
identical ISO string — 3-4% of programmatically-created pairs — and on a tie SQLite returns
rows in scan order, so `LIMIT 1` handed back the *older* session, wrong on 62 of 62 ties.

Scope, stated precisely: it needs two sessions created inside the same clock tick with both
still open, which means scripted creation or a CLI racing the MCP server, not a human
typing two commands. The MCP `end_session` tool was never *affected* — it closes
`_current_session_id`, held in the server process, and never consults
`latest_open_session()`. The two callers that could hit it are `evoctx end-session` without
`--id` (closes the wrong session) and the Stop hook (names the wrong session's intent).

The same defect was in four more places, and paging makes it worse than truncation:
`PolicyEngine._fetch_filtered` walks `search()` and `recent_sessions()` with LIMIT/OFFSET,
and an order that isn't total can repeat or skip a row between one page and the next.
`search()` ordered by `rank` alone (bm25 ties are routine — same-shape notes score
identically); its LIKE fallback, which fires on a malformed FTS query, had **no** `ORDER BY`
at all, and paging an unordered query is undefined rather than merely unstable. `get_project`
took the 5 most recent notes with no tiebreaker, so a tie decided the cut arbitrarily. The
audit log ordered by `ts` alone while carrying an `AUTOINCREMENT` id. All now order by a
total key.

One case needed the opposite treatment. `find_note_by_prefix` resolved an id prefix with an
unordered first-row read, so an ambiguous prefix returned an arbitrary note — and
`evoctx delete <prefix> --force` skips the confirmation prompt, so it could delete a note
the user never named, unrecoverably. Ordering that query would only have made the wrong
pick *deterministic*. It now refuses and lists the candidates, the way git refuses an
ambiguous rev.

`evoctx end-session --id <prefix>` silently resolved an ambiguous prefix too — to the
newest match inside a 50-session window — and now shares that same lookup. The window was
its own bug: a unique prefix for anything older reported "No matching open session found"
while the session existed, and a prefix ambiguous only outside the window never reported
it. Both lookups now scan the whole table and escape LIKE metacharacters, since an
unescaped `_` in a mistyped prefix is a single-character wildcard and a widened match that
happens to hit exactly one record resolves instead of reporting ambiguity.

**One visible consequence:** equally-ranked search results now come back **newest first**.
They previously came back oldest-first, because scan order is insertion order — not by
design, just by accident of there being no tiebreaker. Newest-first matches the
`created_at DESC` used everywhere else, and is the behaviour the new tests pin.

Recorded because the label was doing damage: this sat on the backlog as a *flaky test*. It
was not flaky — the ordering was wrong whenever the tie occurred, and the old test only
failed when the race happened to fire, so it read as noise. The replacement pins two
sessions to one `started_at`, so it fails every run against the old query rather than one
run in thirty.

Also documented, because both cost real debugging time: an `mcpServers` block in
`~/.claude/settings.json` is ignored by the VS Code extension (hooks in the same file
*do* apply globally — only `mcpServers` is inert), and a parent directory's `.mcp.json`
does not cover child repos.

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
