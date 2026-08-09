# Contributing

evoctx is a young, single-maintainer project. That's a real risk if you're trusting it with
your accumulated notes — projects like this stall. Two things are designed to make that survivable
rather than catastrophic if it happens.

## Your data outlives the project

The store is 5 plain SQLite tables. If this project goes quiet, your data isn't locked in —
it's queryable with any SQLite client on day one, and `evoctx export` (or `sqlite3 store.db`
directly) gets it all out in JSON or Markdown.

```sql
notes(id, title, content, project, tags, session_id, author_client, created_at, updated_at,
      is_global)
projects(name, overview, conventions, open_questions, updated_at,
         description_author, description_reviewed_at, attention_dismissed_at)
project_roots(project, root_path, machine, added_at)
sessions(id, project, client, intent, started_at, ended_at, summary)
audit_log(id, ts, connection_id, client, grant_name, tool, action, query,
          record_ids, result_count, response_bytes, redactions_applied)
```

`tags` is a JSON array stored as text. Everything else is exactly what it looks like. No opaque
blobs, no proprietary format, no encryption key you'd need this project's code to unwrap.

## How to contribute

- **Bug reports / feature requests:** open an issue. Include `evoctx doctor` output — it covers
  version, schema version, store size, and client-config detection in one command.
- **Pull requests:** run `python scripts/preflight.py` before opening one. It derives the file
  list from git rather than from memory, runs the pinned pyright over exactly those files, runs
  the tests that import them, checks that `_init()` and `MIGRATIONS` still build the same
  schema, checks that the three instruction channels moved together, and measures whether the
  failure paths you added were ever executed. It defaults to diffing against the merge base
  with `main`, so it works on a committed branch; pass `--base <ref>` for anything else. Paste
  its report into the PR — a missing report is visible, a forgotten check is not. It runs the
  full suite whenever a file under `src/evoctx/` changed, but a docs-only change gives it no
  Python to check, so also run `pytest tests/ -v` and `npx --yes pyright@1.1.411` directly;
  CI gates on both — the tests across ubuntu/macOS/Windows × Python 3.10/3.12, pyright once,
  at that pinned version (settings live in `[tool.pyright]` in `pyproject.toml`; the baseline is
  zero errors). New MCP tools need a CLI mirror in the same PR — see the parity table in
  [README.md](README.md#cli-evoctx).
- **Security concerns:** see [SECURITY.md](SECURITY.md) first — it may already be a documented,
  known limitation rather than something to report as new.

## Releasing

A PyPI version number cannot be reused, so check the repo's state — not just the diff —
before tagging:

1. `pyproject.toml` version matches the tag.
2. `CHANGELOG.md` top heading is that version with today's date, and its summary covers the
   whole release.
3. `README.md` status line names that version.
4. **If this release adds an MCP tool:** it is in `ALL_TOOLS`, registered in `server.py`, and
   the *outgoing* complete tool set is appended to `PRIOR_TOOL_SETS` — otherwise every
   existing enumerated grant silently loses the new tool with nothing reporting it.
   `test_prior_tool_sets_covers_the_previous_release` fails until you do, and its literal is
   the thing to update.
5. `pytest tests/ -v` and `npx --yes pyright@1.1.411` both clean, **and**
   `python scripts/preflight.py` clean. Run the first two directly, not only through preflight:
   a release commit is often a version bump plus a CHANGELOG, which gives preflight no Python
   to check and no tests to select. What preflight adds on top is the schema-drift and
   instruction-channel checks that step 6 would otherwise have to catch by reading.
6. **Run the audience sweep below against the release, don't read it.** Steps 1–5 are all
   mechanical — version strings, tool lists, tests — and none of them can see a sentence that
   became false. 0.1.4's release review found the README still carrying the *previous*
   release's instructions snippet byte-for-byte, telling assistants to pass a project name
   that this release turned into a hard refusal; a shipped example contradicting the shipped
   default; and a security document promising write-time redaction on a path that did not have
   it. Every one of those is invisible to a diff and obvious to one execution. Check counts and
   enumerations first ("gains three checks", "5 tables", lists of flags) — they rot fastest and
   are cheapest to verify.
7. Push `main`, wait for CI green — the tag is what publishes, and it does **not** re-run
   the suite. CI on `main` is the gate (3 OS × 2 Python, plus pyright); re-running a
   single platform at tag time would be weaker while reading like the real thing. The
   release workflow checks the one thing CI cannot — that the tagged commit is an
   ancestor of `origin/main`, i.e. the commit CI actually saw — and refuses otherwise.
8. `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`. `release.yml` builds, publishes to PyPI via
   trusted publishing, and cuts the GitHub release.
9. Install from PyPI into a clean venv and check `evoctx version`.

## Change the prose in the same commit as the code

Every behaviour change lands with its documentation, in one commit. Not as a follow-up, not
"once it settles". A claim and the thing it describes living in different places, with only one
edited, is the single most common defect this project produces — 0.1.4's phase 3 took ten review
rounds, and **every behavioural bug was found in the first three; the other seven were sentences
describing behaviour the code no longer had.**

Sweep by **audience**, not by file. One file often serves several, and marking it "done" after
the first is how the rest survive:

| | Audience | Where |
|---|---|---|
| a | Human reading docs | `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `docs/`, `examples/` |
| b | Human reading CLI output | argparse `help=` **and** the long `evoctx help` block — separate code paths |
| c | Model reading tool schemas | every tool docstring, **including its `Returns:` contract** |
| d | Model reading server instructions | `FastMCP(instructions=...)` — automatic, reaches every client |
| e | Model reading pasted config | `docs/instructions-snippet.md` |
| f | Model reading tool **output** | `hint`, `note`, `{"error": ...}`, `PolicyDenied` text |

(f) is the one that gets missed, and it is the most authoritative thing the model sees: it
answers the question just asked, where (d) is background. It does not exist until a tool is
called, so no amount of reading source or docs surfaces it — **verify it by executing the call.**

Where two channels must agree, pin them with a test rather than intending to remember:
`test_both_instruction_channels_offer_registration` and
`test_prior_tool_sets_covers_the_previous_release` both exist because a comment could not
enforce what it asserted.

## Design principles, if you're extending this

- **CLI/MCP parity.** Every MCP tool gets a CLI mirror. The store must stay human-operable, not
  just AI-operable.
- **Deletion and bulk export are human-only.** Never add an MCP tool that deletes or bulk-exports.
  This is deliberate, not an oversight to "fix."
- **Grants gate the MCP path, not the filesystem.** Don't design a feature that implies it's a
  sandbox against the AI client itself — see SECURITY.md §1.
- **Write-time redaction lives in `db.py`, not `policy.py`.** It has to catch CLI writes too,
  which never pass through the policy engine. Keep the built-in secret floor at the lowest
  common layer.
