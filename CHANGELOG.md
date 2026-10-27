# Changelog

## 0.1.5 — 2026-10-27 — search finds things, and you close the session you opened

Search is the headline. Notes are only worth writing if they come back, and three separate
things stood between a query and the note that answered it: tags were not indexed, multi-word
queries were combined with AND and therefore usually matched nothing, and a result list could
not say how much of the answer it was leaving out. Migration 006 runs on the next open and
needs nothing from you. It is one-way, like every migration: 0.1.4 refuses the upgraded store,
so machines sharing a store upgrade together; `store.db.bak-pre-migration-006` is written first
and is the rollback.

Sessions gain the other half: `end_session` now takes the id `start_session` gave you, so a
machine running two assistant windows closes the right one.

**Three breaking changes.** `search_context()` returns an object rather than a list (AI clients
handle that transparently, since the response is described to them; a script reading the result
as a list needs `["results"]`). `evoctx search-context` now ends with `showing 1-10 of 40
match(es).` where it used to print `10 result(s).` — anything parsing that line needs updating.
And `evoctx end-session --id` requires at least eight characters of the id, or the whole one,
where any unique prefix used to do; a script passing a short one now exits 1 instead of closing
a session.

### 1. Tags are searchable

`notes_fts` indexed `title` and `content`. Tags were stored, displayed and filterable, but a
search for the word in a tag did not find the note carrying it — on the store this was
developed against, 51 notes were tagged `#gotcha` while 39 mentioned the word in their text,
so a dozen notes were tagged for a topic and unreachable by searching for it.

Migration 006 rebuilds the index as `(title, content, tags)`. Because `tags` is a real column
on `notes`, `content=notes` populates it during the rebuild with no backfill step, so every
note already in the store becomes findable by its tags — not only notes written from here on.

Tags are indexed as the stored JSON (`["#session", "#evobytes-api"]`), which is all that is
needed: unicode61 treats `[`, `"`, `#`, `,` and `-` as separators, so the indexed terms are
`session`, `evobytes`, `api` — what anyone searching for a tag types. A leading `#` on a query
is stripped, so `#gotcha` and `gotcha` are the same search.

The FTS triggers are dropped and recreated as part of the migration rather than left in place.
An external-content FTS5 table stays consistent only if its `'delete'` command replays exactly
the tuple that was indexed, so a trigger writing two columns against a three-column table
would corrupt the index for every note edited or deleted afterwards.

### 2. Search widens instead of coming back empty

FTS5 combines two bare words with **AND**. A natural four- or five-word question therefore
demanded that every term appear in one note, matched nothing, and left the impression that the
store held nothing on the subject. `ANAF efactura submission status polling` returned 0 notes
on a store that held four good answers to it.

`Database.search()` now climbs a ladder and stops at the narrowest rung that returns five or
more matches:

1. **AND**, synonym-expanded, as before.
2. **OR** across the same terms.
3. **Prefixes** — `newslet` finds `newsletter`.

Five, rather than one, because a single incidental match is the shape most likely to be the
wrong one: on a real store the five-word ANAF query above ANDed to exactly one note that
happened to contain all five words, and stopping there would have hidden the four notes that
actually answered it. If no rung reaches five, the fullest rung wins, ties going to the
narrower — not the widest rung with any hits, since rung 3 drops synonym expansion and so is
not a superset of rung 2.

Widening costs no precision, because it does not reorder anything: rung 2 is rung 1's terms
joined with OR, so its results are a superset, and bm25 accumulates a contribution per matched
term — the note carrying every term still comes first. The honest limit is that bm25 is a
score rather than a guarantee, so a very short note matching one term in its title can in
principle outrank a long note matching all of them.

**There is still no substring search, in any mode.** FTS5 has no infix operator, so `letter`
will never find `newsletter`. The LIKE path that *can* match inside a word runs only when FTS5
refuses to parse a query at all; it is an error path, not a search mode.

The ladder rewrites FTS5 barewords — alphanumerics, `_`, and anything at or above U+0080. That
covers what actually gets typed at a developer's context store: `mt940`, `0019`, `sha256`,
`bank_statements`, and words carrying diacritics. A query containing deliberate FTS5 syntax —
quotes, parens, `OR`, `NOT`, `NEAR`, a prefix `*`, a leading `-` — is passed through untouched
and gets neither synonym expansion nor widening, so an exact phrase stays exact. Typographic
quotes (`“ ” ‘ ’ « »`) count as quoting for this purpose even though FTS5 itself does not
honour them: whoever pasted a quoted phrase meant the phrase, and answering a broader question
is the wrong way to fail.

### 3. Ranking distinguishes a title from a body from a tag

`ORDER BY rank` weighted every column equally, so a note *about* webhooks tied with one that
mentions them once in passing. Ordering is now `bm25(title=10, content=1, tags=4)`: a title
naming the thing is the note you wanted, and a hand-applied tag beats incidental prose without
outranking a title. Weights only reorder; nothing is included or excluded by them.

### 4. `search_context` reports how many notes matched

It returned a bare list, which cannot express *"there are 40 of these and you are looking at
10"* — so an assistant had no way to tell a complete answer from the first page of one. The
response is now:

```json
{"results": [...], "total": 40, "offset": 0, "limit": 10, "has_more": true,
 "page_token": "invoice OR reconciliation"}
```

`offset` pages through a large result, and `page_token` keeps the pages talking about the same
thing. Each call decides afresh how broadly to interpret the query, so a note written between
two calls can narrow that decision and make page two a slice of a different set altogether;
passing the previous token back pins the interpretation for the whole walk.

It is a token, not a snapshot, and paging is positional — writes and deletes between calls
still move rows across page boundaries, so a paged walk of a store being written to samples
the matches rather than guaranteeing every one. Where completeness matters, raise `limit` and
take the answer in a single call. The tool documentation says so at the point of use.

`total` is counted after grant filtering, from the same
sequence the page is sliced from, so it never advertises notes the caller would then be denied
— a raw `COUNT` promising 40 and delivering 6 reads as a paging bug rather than as a grant
doing its job. For the same reason the widening ladder now counts what the caller can see: a
rung matching six notes in a project the grant cannot read has, for that caller, matched
nothing, and the search moves on to the next rung instead of reporting zero.

A query matching more than 2000 notes is answered from the first 2000. `total` then arrives as
`total_is_lower_bound`, so it reads as a floor rather than a count, and `has_more` stays false
because those extra matches are not reachable by paging — the answer there is to narrow the
query, which is what the tool documentation says.

Ranking is by relevance and never by date, which the tool docstring, the instruction snippet
and the README now all state outright: the position of a result says nothing about when it was
written, so read `created_at` when recency is what matters.

Counting costs one ordered pass over the matches, per rung the ladder tries. On this hardware a
broad query matching every note takes about 4 ms on a 378-note store and 30 ms on a 3000-note
one; the same page without a count is a few milliseconds either way, and a narrow query is
within noise of its old cost.

### 5. `end_session` takes a session id

Two assistant windows sharing one store is ordinary, and the tool closed whatever session
*this connection* had opened, with no way to name another. The failure that produces is not a
missing summary but a wrong one — a summary of work the closed session never did.

`start_session` now tells the caller to record the id it returns, and `end_session(summary,
session_id=…)` closes that one. A prefix of eight characters or more resolves it, so the short
form printed in listings can be pasted back; shorter prefixes are refused, because closing has
no confirmation step and no undo. Both the MCP tool and `evoctx end-session` enforce it.

Closing also reports `other_open_sessions` and a `warning` line, and only when something else
is genuinely open. The wording is an observation rather than an instruction: the caller cannot
know whether another window's session was abandoned or is being worked in right now, and
closing a live one is worse than leaving it untidy. Those entries are redacted before the
warning quotes them, and the count they report is the real number of open sessions rather than
the length of the list being displayed.

Closing a session that was already closed now says so rather than returning the untouched row
as though this call had written it, and releases the connection's hold on it — reached by
closing from the CLI, or from another window, while an MCP connection still has the session
open.

### 6. Smaller things

- **The Stop hook states what it cannot know.** It runs as a separate process with no view of
  which window opened which session, so it reports the newest open one, prints the full id and
  the exact `end_session(summary, session_id="…")` call, and says plainly that it cannot
  attribute it. Registering a workspace is still what narrows it to the repo you are in.
- **`evoctx doctor` checks which columns the index covers**, not only that the FTS table
  opens, and names a real recovery path if they are wrong.
- **`evoctx search-context` gained `--offset` and `--page-token`** and reports total matches,
  keeping the CLI a full mirror of the MCP tool. It prints the next page's command, token
  included, so paging is a copy-paste rather than a reconstruction.
- **`evoctx end-session --id` now requires at least eight characters** of the id, or the whole
  one. A unique one-character prefix used to close a session, and unlike `evoctx delete` this
  path has no confirmation and no undo.
- **`evoctx demo-seed` detects an existing store again.** Its guard searched for `*`, which
  FTS5 rejects, so it read "empty" on a full store and re-seeded the demo notes every run.
- **`scripts/preflight.py`, for contributors.** It derives the changed-file list from git rather
  than from memory, runs the pinned pyright over exactly those files, runs the tests that import
  them, checks that `_init()` and the migrations still build the same schema, checks that the
  three instruction channels moved together, and measures whether newly added failure paths were
  ever executed. `CONTRIBUTING.md` asks for its report on a pull request.

## 0.1.4 — 2026-08-04 — projects know where they live, and each client can have its own grant

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

`description_reviewed_at` is written by `register_project(confirmed_by_user=True)` —
item 10 below — and only when that call authored the description; linking an existing
project leaves it alone. It records that a human was *said* to have approved the text, which
is an unverified client claim, so read `description_author` when provenance matters
([SECURITY.md §5](SECURITY.md)). `attention_dismissed_at` is still schema only: nothing
writes it, and its writer arrives with the incomplete-project prompt.

The clearing rule is live for both: any write that changes the overview or conventions
clears the review, including the author's own, because a review approves specific words.
Text that does not change keeps it.

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
assistant reading the response sees the names one key deeper — but re-read
`docs/instructions-snippet.md` and replace the block in your rules file to get the part that
tells it not to guess. (`evoctx install` points at that file; it does not print the text.)

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

**19. Each client can have its own grant.** Installing several assistants is usually about
giving them *different* access — read-only for one, full write for another — and that was
impossible. One grant was active machine-wide, and grants carry a `client` glob, so a
client-scoped grant did not scope that client: it **denied every other one**. With the
default `client: "*"` grant this never bit, which is why it went unnoticed; it bit the
moment anyone did the natural thing and wrote a narrow grant per assistant.

```
evoctx grant activate read-only --client "cursor*"
evoctx grant activate everything                    # everyone else
evoctx grant deactivate --client "cursor*"          # cursor falls back to the default
```

`active_grant.json` gains a `clients` map, additively. **The scalar `name` and
`activated_at` keys are always present**, so an older build reading this file still parses
it — 0.1.3 subscripts `activated_at` directly, and a file without that key fails every call
with a `KeyError`. Present, not necessarily non-null: if you deactivate and then set only
per-client grants, an older build correctly reads "no grant is active" and denies calls,
because from its point of view that is exactly what the file says. It is a downgrade path
that degrades honestly, not one that keeps working.

Four rules, each of which had a wrong answer available:

- **A per-client grant takes precedence; the machine-wide one is the fallback** for clients
  with no entry.
- **An expired entry denies that client and does not fall back.** This is the one place
  fallback would be dangerous: a `read-only` grant for cursor with an 8-hour expiry would,
  on falling back, silently promote cursor to the machine-wide `everything` at the exact
  moment its restriction was supposed to take effect. Narrowing fails closed.
- **Which entry applies is decided per client, at lookup, not per file order.** `claude*`,
  `claude-c*` and `*-code` all match `claude-code`, and JSON preserves insertion order, so
  "most specific" would otherwise have meant "whichever the file listed first". Exact key
  wins; then the most literal characters; a tie is **ambiguous and denied**, naming both
  keys, the way an ambiguous note-id prefix is refused rather than resolved by guesswork.
- **Matching is case-insensitive on every platform**, both tiers. `fnmatch` casefolds via
  `os.path.normcase` on Windows and not on Linux, so a synced store would otherwise resolve
  differently on each machine. `Grant.matches_client` casefolds too, in the same change:
  fixing only the map would have moved the divergence one check down rather than removing it.

  **This widens `client:` matching on Linux**, where a grant scoped `client: "cursor"`
  previously refused a client calling itself `Cursor` and now accepts it. Called out because
  it is the one loosening in this release. It costs nothing an attacker did not already have
  — client names are self-reported and unverified ([SECURITY.md §5](SECURITY.md)), so a
  process willing to send `Cursor` was equally willing to send `cursor` — while the
  alternative was a grant that behaves differently on two machines sharing one store.
  Removal is the exception: `grant deactivate --client` takes the key exactly as written,
  case included, because that one deletes.

  **`scope.projects` went the other way, and this narrows on Windows.** It was on the same
  platform-divergent `fnmatch`, so a grant scoped to `Payments` also matched a project called
  `payments` on Windows and did not on Linux. A project name is a store key rather than a
  self-reported string — the store holds those as two projects, and `list_projects()` returns
  the exact spelling — so folding there let a scope reach a project the grant does not name.
  It is now case-sensitive everywhere. If a `grants.yaml` of yours relies on the Windows
  behaviour, fix the casing in `scope.projects` to match the project names.

**`evoctx grant deactivate` with no `--client` still clears everything, per-client entries
included.** It is the kill switch — it says every call is now denied — and preserving the
map there would leave every client with an entry fully active after you pulled it. With
`--client` it takes the key **exactly as written, case included**: everything else here
folds case, but this one deletes, and a case typo silently removing a real access rule can
promote the client it belonged to.

**No message this feature prints ever tells you to remove an entry in order to fix a
denial.** Removing one hands its client back to the machine-wide grant, which is normally
the wider one — so the remedy for a misconfigured narrowing would be to undo the narrowing,
and an assistant relaying the command verbatim would be asking for the access it was just
refused. Every fix is an **addition**: even an ambiguous pair of keys is settled by giving
that client an entry of its own, which outranks both, rather than by deleting one of them.
Where the grant to use is your choice, the command carries a `<name>` placeholder and the
reason names the grants behind each key so the choice is informed.

Two tests hold that. One runs every remediation across seven failure states and asserts no
client ends with more access than was *configured* for it — measured against what is written
down for that client, not against what it currently has, because renewing a lapsed entry
legitimately restores access. The other scans the source: no module may emit a string
suggesting an entry be removed. The second exists because "these two functions are the only
producers" was asserted in prose and was not true — four other modules compose `evoctx
grant ...` strings, and the same defect kept reappearing in whichever one had not been
looked at.

**A file that cannot be read is never overwritten.** `active_grant.json` failing to parse
resolves to "no grant is active" — the safe answer for reads. It was also, until now, what
the *write* path merged onto: one stray byte, and the next routine `grant activate` saw no
`clients` map, wrote a clean two-key file, and silently deleted every per-client narrowing,
exit 0, with a success line. `activate` and `deactivate --client` now refuse and name the
two recoveries — repair the JSON to keep the entries, or `grant deactivate` to reset. The
kill switch is deliberately exempt: it never reads the file, and it denies everything, so it
cannot widen.

**`grant activate --client` restores the store on any non-zero exit.** Every check it makes
asks what the store resolves to, which is only answerable once the entry is written — so it
writes, checks, and rolls back if the answer is bad. Exit 0 still means "this is applied and
usable"; exit non-zero now means nothing changed.

⚠️ **An evoctx older than 0.1.4 running `grant activate` on this store silently erases every
per-client entry.** Its `activate` writes a fresh two-key file and does not know the map
exists. There is no error, nothing in the audit log, and 0.1.4 cannot detect it afterwards —
what it reads is a perfectly valid 0.1.3-shaped file. This cannot be fixed from this side.
0.1.4's own `activate` merge-preserves the map, `doctor` warns whenever entries exist, and
if you sync a store between machines, upgrade all of them.

**20. Response shapes changed — check this if you script against evoctx.** Additive except
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
- `list_grants()` gains `client_key` — which per-client entry answered, or null for the
  machine-wide grant. It now resolves **for the calling client**: answering "may I call X?"
  from the machine default while that client's real calls run against a narrower entry
  would be the one wrong answer this tool cannot afford.
- `evoctx doctor` gains a **grant scope casing** check, because the case-sensitivity change
  above is otherwise silent: a `scope.projects` entry differing from a real project only by
  case now matches nothing, and the only symptom is a denial mid-task. Globs and
  not-yet-created projects are left alone — only the near miss is evidence of the flip.
- `evoctx doctor` gains three more checks beyond the two in item 9: registered directories
  that are no longer present, notes marked both global and project-scoped, and the
  pre-upgrade backups described below (so you know they exist and that they are yours to
  delete). Three more appear **only once a per-client grant
  exists** — `per-client grants` (the entries, their expiry, and the older-build wipe
  warning), `client grant coverage` (installed clients running on the machine-wide
  fallback), and `per-client grant resolution` (installed clients that resolution denies,
  which is the only one of the three that fails). Unconditionally they would fire on every
  fresh install, where every client legitimately runs on the default.
- **A grant whose `scope.projects` is present but empty is now rejected**, in either
  spelling — `projects: []` or a bare `projects:`. It previously parsed as *every* project,
  which is a scope failing open, and it now also decides who may write a global note. If you
  have hand-edited `grants.yaml` this way, fix it before upgrading: grant parsing runs
  inside every tool call, so a rejected file denies all of them.
- **Project descriptions written before 0.1.4 are not retroactively masked.** Write-time
  secret masking now covers `overview`/`conventions`/`open_questions`, which it did not
  before — but it runs on writes, and migration 005 does not rewrite text you wrote. An old
  description holding something secret-shaped stays as it is until you rewrite it
  (`update-project`, or `init --rescrape`). Deliberate: a sweep would edit prose that merely
  looked like a key, silently, in the one field a human is most likely to have curated.
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
