# Changelog

## 0.1.3 — unreleased — forward-compatible grants, and install guidance that matches reality

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

**7. `evoctx end-session` could close the wrong session.** With no `--id` it closes the
latest open one, and `latest_open_session()` ordered by `started_at` with no tiebreaker.
`datetime.now()` on Windows is coarse enough that two sessions started back to back land on
the identical ISO string — measured at 3-4% of pairs — and on a tie SQLite returns rows in
scan order, so `LIMIT 1` handed back the *older* session: wrong on 62 of 62 ties. The
summary went onto the wrong session and the intended one stayed open. Both session queries
now break ties by `rowid DESC`; the paged one needed it too, since an order that isn't
total can repeat or skip a row between pages.

This was on the backlog as a flaky test. It was not flaky — it was a correctness bug
sampling itself at 3%, and the test that caught it only failed when the race happened to
fire. The replacement pins the tie deliberately, so it fails every run against the old
query instead of one run in thirty.

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
