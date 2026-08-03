# Contributing

evoctx is a young, single-maintainer project. That's a real risk if you're trusting it with
your accumulated notes — projects like this stall. Two things are designed to make that survivable
rather than catastrophic if it happens.

## Your data outlives the project

The store is 4 plain SQLite tables. If this project goes quiet, your data isn't locked in —
it's queryable with any SQLite client on day one, and `evoctx export` (or `sqlite3 store.db`
directly) gets it all out in JSON or Markdown.

```sql
notes(id, title, content, project, tags, session_id, author_client, created_at, updated_at)
projects(name, overview, conventions, open_questions, updated_at)
sessions(id, project, client, intent, started_at, ended_at, summary)
audit_log(id, ts, connection_id, client, grant_name, tool, action, query,
          record_ids, result_count, response_bytes, redactions_applied)
```

`tags` is a JSON array stored as text. Everything else is exactly what it looks like. No opaque
blobs, no proprietary format, no encryption key you'd need this project's code to unwrap.

## How to contribute

- **Bug reports / feature requests:** open an issue. Include `evoctx doctor` output — it covers
  version, schema version, store size, and client-config detection in one command.
- **Pull requests:** run `pytest tests/ -v` and `npx --yes pyright@1.1.411` before opening one;
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
5. `pytest tests/ -v` and `npx --yes pyright@1.1.411` both clean.
6. Push `main`, wait for CI green — the tag is what publishes.
7. `git tag -a vX.Y.Z && git push origin vX.Y.Z`. `release.yml` builds, publishes to PyPI via
   trusted publishing, and cuts the GitHub release.
8. Install from PyPI into a clean venv and check `evoctx version`.

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
