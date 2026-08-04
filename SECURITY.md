# Security

evoctx is a local personal-context store for AI coding assistants. This document says plainly
what it protects against, what it doesn't, and why — before an outside reviewer has to find out
by testing it. If you're deciding whether to trust it with your notes, read this first.

**One-line summary: this is policy rails for cooperative clients, not a sandbox.** Every
mitigation below assumes the MCP client is playing by the rules of the protocol. None of it
holds up against a client (or a process on your machine) that decides not to.

## 1. Grants police the MCP protocol path, not your filesystem

Grants, scopes, and redaction all gate calls made *through the MCP tool interface*. An AI
client that also has filesystem tools — which describes most coding assistants, including the
ones this project is built for — can simply:

- read `store.db` directly with its own file-read tool, bypassing every scope and redaction rule
- edit `grants.yaml` or `active_grant.json` to grant itself whatever it wants
- set its own `CONTEXT_CLIENT` environment variable to match a more permissive grant's `client:` pattern

**This is not a bug we plan to patch — it's a structural limit of policing an API layer when
the same actor has a lower-level door into the same data.** The honest way to close it (partially)
is client-side: pair evoctx with your MCP client's own permission system so the assistant's file
tools can't read or write the store's files directly. For Claude Code, that means a
`permissions.deny` rule covering the store directory (`evoctx doctor` shows the path) —
`evoctx install claude-code --hooks` writes this for you, alongside the hooks and without a
separate prompt. Other clients vary; check
whether yours supports scoping file-tool access and apply the same idea.

If your threat model includes "the AI itself is adversarial," grants don't solve it. Grants
solve "I want different MCP clients to see different slices of my knowledge, with a record of
who saw what" — a much narrower and more realistic problem for a personal, single-user tool.

## 2. Stored notes are a persistent injection surface

Every note written in a past session gets read back into a future session's context, often at
the highest-trust moment (session start, "here's what you should know"). A note containing
attacker-authored instructions — from a compromised session, a poisoned file swept in by
`import-dir`, or a supply-chain issue upstream of the assistant that wrote it — gets replayed as
trusted context indefinitely, since writes are append-only and there's no automatic expiry.

We don't have a fix for this; nobody building AI memory systems does. What evoctx does instead:

- **`author_client` provenance.** Every note records who wrote it — `cli` for a direct human
  write, a client name (`claude-code`, `cursor`, ...) for an assistant write. Visible in every
  read path (`search_context`, `get_project`, `evoctx show`). It's a label, not a sandbox: it
  tells you where a note came from, it doesn't stop a bad one from being read back.
- **Server instructions tell the model explicitly** that past notes are informational context
  from prior work, not commands from the current user — reducing (not eliminating) the odds a
  model treats a stored note's content as an instruction to follow.
- **Deletion exists.** If a note turns out to be bad, `evoctx delete <id>` removes it — CLI-only,
  human-only, deliberately not something an MCP tool call can do.

If you write or import content you don't fully trust, treat it the way you'd treat any other
untrusted input reaching an LLM context window — because that's exactly what it is.

## 3. Redaction: what's masked, what isn't, and when

Two independent layers, described in full in [README.md](README.md#redaction):

- **Write-time (unconditional):** common secret shapes (API key prefixes, cloud provider
  tokens, PEM private key blocks, `password=`-style assignments) are masked *before* they touch
  disk, regardless of entry point or active grant. This is regex-based and best-effort — it
  will miss secrets in an unrecognized format, a value split across fields, or one the model
  paraphrases rather than pastes verbatim. It is not a substitute for not pasting secrets into
  an AI assistant's context in the first place.
- **Read-time (per-grant, optional):** a grant's custom `redactions` list masks matching text in
  responses only — it never modifies what's stored, so a stricter grant activated later doesn't
  retroactively protect data written under a looser one. (Write-time redaction is what actually
  prevents that; read-time redaction is for narrowing what a *specific* less-trusted client sees.)

**Write-time masking applies to writes, so it does not reach back.** Project descriptions
were not scanned before 0.1.4 (note bodies and session text always were). If a description
written by an older version holds something secret-shaped, upgrading does not clean it —
migration 005 adds columns, it does not rewrite your text, and a sweep that edited existing
descriptions would silently mangle any prose that merely looked like a key. Rewriting the
description (`evoctx update-project NAME --field overview --file …`, or `evoctx init
--rescrape`) masks it on the way back in. `evoctx dump` shows you what is actually stored.

Neither layer is encryption. Redacted text is still recoverable by anyone with direct file
access to `store.db` for anything the write-time patterns didn't catch.

## 4. The store is unencrypted at rest

`store.db` is a plain SQLite file. Anyone who can read your filesystem — another local process,
a backup system, a stolen disk — can read it, full stop. Encryption at rest is on the roadmap,
not shipped. Two things worth doing now regardless:

- **Check `evoctx doctor`'s "cloud sync" line.** If your store folder resolves under OneDrive,
  iCloud Drive, Dropbox, or Google Drive, that unencrypted file is being silently uploaded
  off-device by default OS/app behavior — not something evoctx does, but something it sits
  inside of unless you relocate it. Set `CONTEXT_STORE` to a path outside any synced folder.
- **Treat the store like any other local secrets-adjacent file** — same disk-encryption (BitLocker/
  FileVault/LUKS) hygiene you'd apply to SSH keys or a password manager's local cache.

## 5. Client identity is self-reported, not authenticated

`CONTEXT_CLIENT` (an env var the user sets in their own MCP config) or the name a client sends
in the MCP `initialize` handshake — both are strings the connecting process chooses to send.
Nothing verifies them. Grant `client:` matching and audit-log attribution are for **organizing
your own setup and getting a readable record**, not for authenticating who's really on the
other end of the connection. A process willing to lie about its name defeats this instantly.

**Identity is not the only unverified client assertion.** `confirmed_by_user` on
`register_project()` is the same shape: the client asserts that a human read and approved the
description it wrote, nothing verifies that, and it durably stamps `description_reviewed_at` —
a column whose whole meaning is "a human approved this text". A client that always sends it
makes every description it writes read as human-approved, permanently. Authorship is never
laundered: `description_author` records the client either way, deliberately, so **when
provenance matters read the author, not the review stamp.** This compounds §2 — a description
carrying an approval stamp is trusted more by later sessions, which is exactly the
amplification §2 warns about.

**Per-client grants inherit this exactly, as of 0.1.4.** A client can now be given its own
grant (`evoctx grant activate read-only --client "cursor*"`), which makes the self-reported
name load-bearing for *authorization*, not only for attribution: a process that reports a
name matching a wider key gets the wider grant. That is not a new hole — one grant covering
every client was never any stronger — but it is a sharper one, because the setup invites you
to treat "cursor is read-only" as enforced. It isn't. It is enforced against a cooperative
client that reports itself honestly, which is what every mitigation in this document assumes.

Two properties of that resolution are deliberate and worth knowing: an expired per-client
grant **denies** rather than falling back to the machine-wide one (falling back would widen
access at the moment a restriction lapsed), and a client matching two equally specific keys
is **denied** rather than resolved by the order they appear in the file. Both fail closed,
including for keys that differ only in case — `Cursor` and `cursor` are one key, so a file
holding both resolves to a refusal instead of to whichever was written first. Removal takes
a key exactly as spelled for the same reason: a `deactivate` that matched case-insensitively
would delete whichever of that pair came first in the file, and if the survivor were the
wider of the two, the remediation would be the widening. The
`deactivate` kill switch clears per-client entries along with the machine-wide grant, so
pulling it leaves nothing active.

**No message about a lapsed grant, a narrow one, or an ambiguity between two globs will
tell you to remove an entry in order to restore access** — with one deliberate exception,
below. That is a constraint on evoctx's own output, not a claim about what you can do:
removing a
narrow entry drops its client to the machine-wide grant, and the server instructions tell an
assistant to relay these commands verbatim — so a denied assistant would be relaying a
request for the access it was just refused. Ambiguity between two globs is resolved by
*adding* an entry for that client, which outranks both. Ambiguity between two case-variants
of the same key has no command at all, because the entry that would outrank them is the one
already present twice; there evoctx says to edit `active_grant.json` by hand and names both
entries with their grants, so the removal is a decision you make with the trade-off in front
of you rather than one a printed command makes for you.

What this does **not** do is verify that a per-client grant is one the client can actually
use. A key whose grant carries a narrower `client` glob of its own is caught when the key
is a concrete name; when both are globs it is not, because deciding whether two globs
overlap is a different problem from deciding whether one matches a given name. That failure
denies rather than widens — but it is a denial you may not expect, so `evoctx doctor`
reports any installed client that cannot make a call.

**Project-less rows are scoped, as of 0.1.4.** A note or session with no project is readable
only by a grant that covers every project. Previously `allows_project(None)` passed
unconditionally, so anything project-less crossed every scope boundary. A note explicitly marked
**global** is different and still reaches scoped grants — it claims to apply to every project,
including theirs.

## 6. The audit log is an accountability record, not tamper-proof forensics

`audit_log` is append-only, enforced by SQL triggers that raise on `UPDATE`/`DELETE`. That stops
a well-behaved MCP client from tidying up its own trail through normal use. It does **not** stop
anyone with direct file access to `store.db` — SQLite has no ACL model, so dropping to the file
directly bypasses the triggers entirely. Audit answers "what did cooperative clients do,"
assuming they went through the MCP interface. It is not evidence in the forensic sense.

The one deliberate, human-only exception: `evoctx audit-prune --older-than 90d` reclaims space
by dropping and recreating the append-only triggers around a single scoped `DELETE`, then
restoring them immediately. It is never reachable from any MCP tool or from `policy.py` — the
append-only guarantee is about stopping a *client* from editing its own trail through the
protocol, not about the log being physically immutable forever. `evoctx doctor` reports the
current row count so growth stays visible instead of silent.

## 7. Known limitations, not vulnerabilities

- **Session cross-linking in one server process.** The MCP server tracks "the current session"
  as a single value per running process (`_current_session_id` in `server.py`). If one MCP
  client somehow drives two concurrent, interleaved conversations over a single server
  connection, notes from one could get linked to the other's session. Realistic MCP usage is
  one conversation per connection, so this is a latent edge case, not an observed problem —
  documented so it's not a surprise later.
- **No multi-user concept.** evoctx is built for one person, one machine (or one person's set
  of machines via manual sync/backup of `store.db`). There's no per-user access control because
  there's no concept of multiple users.

## Reporting a concern

This is a young, pre-1.0, single-maintainer project. If you find something not covered above,
open an issue. There's no bug bounty and no formal disclosure process yet — a plain issue or
a direct email to the address in `pyproject.toml` both work.
