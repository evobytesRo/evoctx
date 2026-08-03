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

**6. `list_projects()` now says which project the workspace is.** This is what the roots
above were for. It returns `projects` (the names, as before) plus a `workspace` block
resolved in three rungs: `CONTEXT_PROJECT` if a human set it, otherwise the working
directory matched against registered roots, otherwise **nothing**.

Rung 3 refuses on purpose. No name-similarity matching, no "closest project", and
specifically no picking the only project when the store has exactly one — that is precisely
when a wrong guess looks right. An unregistered workspace gets back the directory, a
suggested name and the command to register it, never an answer resolution did not find.

A cwd match is reported as `confidence: "default"`, not as the answer. One stdio server has
one working directory and a workspace can span many folders, so treating the launching
folder as the truth would file a sibling repo's work under its neighbour — the same
confident-wrong failure rung 3 exists to prevent, one rung up. The block carries `nearby`:
the other registered projects in this workspace, found as siblings of the directory or as
folders declared in a `.code-workspace` near it.

`CONTEXT_PROJECT` naming a project that does not exist **refuses and says so** rather than
falling through to the directory. It was set deliberately by a human, so quietly resolving
somewhere else would hide the typo indefinitely.

**Shape change:** `list_projects()` returned a bare list of names and now returns a dict.
The names are under `projects`. A client whose rules file still carries the 0.1.3
instructions snippet keeps working — the tool is unchanged in name and arguments, and an
assistant reading the response sees the names one key deeper — but reinstall the snippet
(`evoctx install <client>` prints it) to get the part that tells it not to guess.

Changing an existing tool's response was the deliberate choice over adding a new tool: a
grant enumerates the tools that existed when it was written, so **adding** one denies it to
every existing user until they upgrade their grant. Response shapes are not gated.

**7. The Stop hook no longer nags about another workspace's session.** It called
`latest_open_session()` unfiltered, i.e. machine-wide, so closing a window in one repo
asked about a session belonging to a different one. It now asks about the workspace: the
project the directory resolves to **plus the other registered repos in the same workspace**,
because a workspace is routinely several sibling repos and the assistant is told to name
whichever one the work targets.

Note what that redefines: **sibling directories are one workspace**, so repos sharing a
parent still nag across each other by design. What stops is a window in one project tree
asking about work in an unrelated one. The message now names the session's project, because
it may not be this folder's.

Sessions started with **no** project still surface everywhere — that is the one most likely
to be forgotten. And a directory that resolves to nothing falls back to every open session,
exactly as before: the reminder narrows only where resolution actually succeeded, and is
never turned off by a resolution that did not happen.

**8. A project root must be a directory that exists.** `evoctx init` already checked;
`add_project_root` — documented as the function every front-end goes through — did not, so
the registration path took typos and bound them. A bound typo is not inert: resolution
answers with it. Roots recorded for *another* machine are still taken on trust, since this
process cannot see that filesystem and refusing them would break restoring a synced store.

**9. `evoctx doctor` gains two more checks:** which project this directory resolves to, and
which folders of the open workspace declare evoctx in a `.mcp.json`. Several folders
declaring it is not an error and evoctx does not arbitrate it — which one the client honours
is the client's behaviour — but believing one config is live while another is is a real way
to lose an afternoon, so they are listed.

The workspace check **fails** (and `doctor` exits non-zero) in two cases: when other
directories on this machine are bound and this one is not — the genuinely inconsistent case
— and whenever `CONTEXT_PROJECT` names a project that does not exist, regardless of
bindings, since a human typed that name and every tool call in the store is unresolvable
until it is fixed. A store with no directories bound at all otherwise passes, since that is
every store the moment it upgrades, and stays true for anyone whose projects are topics
rather than code.

**10. New tool: `register_project`, so an assistant can register a workspace it has read.**
`list_projects()` now says when a directory belongs to no project; this is how that gets
fixed without the user leaving the conversation to run a CLI command.

**The tool has no path argument, deliberately.** The server binds its own working directory
— the one it already reports as `workspace.cwd`. A path parameter would put the single fact
the caller cannot verify back under the caller's control, which is the failure this whole
release exists to remove. The assistant supplies the description, having been told in the
tool contract to read the workspace rather than paraphrase a directory name.

Authorship is recorded as the calling client, always. `confirmed_by_user` records that a
human approved the text and never changes who is recorded as having written it: if approval
flipped that label, one rubber-stamped "looks good" would erase the provenance permanently.
It is also an unverifiable self-report, so it is stored as the weaker fact it actually is —
and it is ignored entirely when the call merely *links* an existing project, since no
description was written for anyone to have approved.

**11. `evoctx grant upgrade` — and you may need it.** Adding an MCP tool denies it to every
existing user: a grant lists the tools that existed when it was written, so `register_project`
is refused by every `grants.yaml` already on disk.

**It only touches a grant that meant "every tool".** A grant is upgraded when its tool list
is exactly the complete set of some earlier release — provably "everything, as of then". A
list you composed yourself is left alone entirely, and **not** reported: `doctor` passes it,
because a narrowing you made on purpose is not a fault. The reason is that "every tool this
build ships that you did not name" cannot tell a grant a release left behind from one you
narrowed deliberately, and would answer a read-only payments grant by adding `write_note` to
it. Same rule decides all three answers, so they cannot disagree: what `upgrade` edits, what
`doctor` reports, and whether a denial tells you your grant is out of date.

The cost of that choice, stated plainly: **an enumerated grant you wrote yourself will
silently miss tools added by future releases.** Add them by hand, or use `tools: ["*"]`.

It adds names and nothing else — never scope, expiry or client. It edits the file's text
rather than round-tripping the YAML, so your comments and line endings survive byte for byte;
a `tools:` written as a block sequence is reported for you to edit rather than rewritten by
guesswork. The candidate is parsed **before** anything is written and the result is checked
to have actually granted what it claims, so a malformed or ineffective edit never reaches
disk — a broken `grants.yaml` denies *every* call. The previous file is kept as
`grants.yaml.bak`.

`evoctx doctor` reports grants that predate the tools this build ships, and a denial for a
tool that exists now names `grant upgrade` rather than sending you to look for a wider grant
that was never the problem.

**12. Fresh stores now default to `tools: ["*"]`.** 0.1.3 shipped an enumerated default to
stay readable by 0.1.2, which rejects `"*"`. That reasoning does not survive contact with
this release: an enumerated default naming `register_project` breaks 0.1.2 identically, and
one omitting it would deny the tool on every fresh install. The 0.1.2 exposure is the same
either way, so it decides nothing — and a fresh store has no deliberately-narrow grant to
respect. Existing files are untouched; `grant upgrade` is opt-in.

**13. A project name that does not exist is now refused, not filed.** `write_note()` and
`start_session()` reject an unknown non-empty project name and save nothing, naming the
workspace's own project and the known names in the refusal.

This is the backstop the rest of the release was building toward, and it could not have shipped
earlier: before resolution existed, an assistant had no way to *learn* the right name, so
refusing would have converted stranded notes into failed writes. Now `list_projects()` answers
the question, so an invented name is a mistake with a fix.

**An unset project takes the workspace's own.** Otherwise `recent_sessions(project=…)` keeps
missing sessions that plainly belong to a project. If nothing resolves, that is refused too —
the two honest options are naming a project or marking the note global. Leaving it NULL is what
conflated *"the user asked for a general note"* with *"nobody set a project"*, which is the
defect items 4 and 6 exist to remove; this closes the last path that still produced it.

Resolution happens **before** the scope check, deliberately: `allows_project(None)` is always
True, so checking first would wave through a null that then resolves to a project the grant does
not cover. A scope-denied default fails loudly rather than falling back to NULL, which would
file the note nowhere to dodge an error.

**What this breaks:** an MCP client that wrote notes with no project, or under a name it made
up, now gets a denial instead of a silent success. That is the point — those notes were
invisible under the real project — but it is a behaviour change, not an addition. The CLI is
unaffected: `evoctx add --project whatever` still writes whatever you tell it, because grants
and this refusal govern the MCP path, not your own filesystem.

**14. `evoctx project-merge WRONG RIGHT` — for the phantoms you already have.** Refusing
invented names stops new ones; it does nothing for the notes and sessions already filed under
one, which are invisible under the real project and findable only by searching for a name you
have no reason to know.

**Notes and sessions move together and there is no flag to split them.** A session records what
a stretch of work was for; its notes record what came out of it. Moving one without the other
leaves a session whose notes are elsewhere — a worse state than the one being fixed, and not
undoable by running the command again.

`WRONG` need not be a real project; that is the normal case, since a phantom is a name on notes
that was never registered. `RIGHT` must exist, because merging into a second invented name only
moves the problem. Directory bindings are **not** transferred — re-make that claim deliberately
with `evoctx init` if you want it. Human-only, no MCP tool, like every other bulk rewrite here.

**15. `evoctx delete-project` now actually unlinks what it keeps.** It has always printed
*"kept, unlinked"* when run without `--with-notes`, and the notes kept pointing at the deleted
name — so they became exactly the phantom item 14 is about, produced by the product itself.
Sessions were never cleared in **either** mode: `--with-notes` deletes notes and has never
touched sessions. Notes are now unlinked (or deleted, with the flag) and **sessions are always
unlinked**.

**Unlinking is not recoverable, and there is no `rename-project`** — so delete-and-recreate is
how people rename one, and the association used to survive that by accident. It no longer does.
The prompt says so before asking, and points at `evoctx project-merge` as the way to move
records instead of orphaning them.

**16. Project-less notes and sessions are no longer readable by a narrowly-scoped grant.**
`Grant.allows_project(None)` returns True unconditionally, so anything with no project was
visible to every grant regardless of scope. That was a tolerable edge while such rows were only
legacy notes from before 0.1.4 — and `check_write_global` said so out loud. Item 15 would have
turned it into a routine one: deleting a project would have made its note bodies and session
intents readable by a grant scoped to a *different* project.

The distinction this release already draws is the one that fixes it. A **global** note says "this
applies to every project", so it applies to the scoped grant's project too and stays visible. A
**project-less** note says nothing about where it belongs, so only a grant covering everything
may read it. Silence is not consent to cross a scope boundary — the same reasoning that refuses
a project-less write.

**If you use a scoped grant**, notes written with no project before 0.1.4 will stop appearing in
its results. They are not gone: a grant with `projects: ["*"]` still reads them, and
`evoctx search` is unaffected because grants govern the MCP path, not your own CLI.

**17. `end_session()` is now scoped like every other session read.** It had no scope check at
all, while `recent_sessions()` refused to list the very same session. It now denies the way
`get_by_id` does — returning nothing rather than explaining, so out-of-scope is
indistinguishable from no-such-id — and a denial leaves the session open and closable rather
than clearing the server's slot.

**The reachable window is narrower than it sounds**, and worth stating rather than implying an
attack the tool does not offer: `end_session()` takes no session id, it closes the one
`start_session()` recorded, and that call is already scope-checked. So this fires when a session
opened under a wider grant is still open when a narrower grant is activated — not for a caller
naming someone else's session. Pre-existing, but item 16 is what made the asymmetry matter.

**18. `evoctx doctor` gains three checks, with deliberately different severities.**

**Phantom projects** reports every name that notes or sessions are filed under but which is not
a project, and names the command to fix it — nothing else in the product would ever mention this
data exists. It **fails only when an assistant wrote them**, which is the case item 13 closed.
Names *you* typed through the CLI are reported without failing, because `evoctx add --project
whatever` is documented as still working and `doctor`'s exit code should not go red for a
decision you made. A note whose author is unknown (written before 0.1.0) counts as not-assistant:
an unknown origin is not evidence of one.

**Project roots bound** never fails. It reports which projects have no directory on this machine,
so you can see what resolution will never match. A topic-only project legitimately has none, on
upgrade day every project does, and templates are excluded entirely since they can never hold
one.

**Unscoped notes** never fails either, and exists because of item 16: it counts notes with no
project and no global flag, which a scoped grant can no longer read. Migration 005 adds
`is_global` without backfilling, so every note written before 0.1.4 is in that set and nothing
else in the product would tell you which ones went quiet. They are intact — the default grant
reads them, and so does the CLI.

**19. Response shapes changed — check this if you script against evoctx.** Additive except
for `list_projects()` above, but worth knowing before you upgrade:

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
- `evoctx doctor` gains five checks: registered directories that are no longer present,
  notes marked both global and project-scoped, the pre-upgrade backups described below (so
  you know they exist and that they are yours to delete), the resolved workspace, and the
  workspace folders declaring the server.
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
