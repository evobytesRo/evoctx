import json
import os
import platform
import sqlite3
import sys
import uuid
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .audit import AUDIT_SCHEMA, migration_002_audit_log
from .secrets import redact_secrets
from .synonyms import expand_query


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_absolute(path) -> Path:
    """Reject a relative path before it becomes a root.

    `resolve()` would happily settle it against the *process* cwd, which is
    right for the CLI (the user's shell is in the directory they mean) and
    wrong for the MCP server, whose cwd is whatever the client launched it
    with. A relative path from there binds a project to some unrelated
    directory, silently, and the UNIQUE index then makes it stick. Callers
    that legitimately mean "relative to the user's shell" resolve it
    themselves first.
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError(
            f"project root must be an absolute path, got '{path}' — "
            f"a relative path would resolve against whatever directory this "
            f"process happens to be in"
        )
    return p


def normalize_root(path) -> str:
    """The stored, comparable form of a project root.

    `resolve()` settles symlinks, `..`, and relative input; `normcase` settles
    case *only where the platform is case-insensitive* — lowercase on Windows,
    identity on POSIX. Both halves matter and neither is optional:

    - Without normcase, the same directory arrives as both `D:\\Projects\\x`
      and `d:\\Projects\\x` and reads as two different roots. That is not
      hypothetical; both spellings were observed in one process listing from
      one client on 2026-07-31.
    - Casefolding unconditionally would be worse on Linux, where `/a/Foo` and
      `/a/foo` are genuinely different directories and merging them would bind
      a project to the wrong one.

    `expanduser` first, because `resolve()` does not expand `~` — it would
    happily produce a literal `~` directory under the cwd.

    Compare normalized against normalized, always — a raw path string will not
    match a stored root on Windows.
    """
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def this_machine() -> str:
    """Identity for `project_roots.machine`.

    A store synced between machines collects roots that only exist on one of
    them, so each row records where it was true. Needed concretely by export:
    an importer applies only the roots matching the machine it is running on.

    Never empty. `platform.node()` returns `''` when the hostname cannot be
    determined, and an empty machine would make every machine-scoped filter
    fall back to "all machines" — so a directory recorded on another box would
    resolve as local. Failing open is the one thing machine-scoping exists to
    prevent.
    """
    return platform.node() or "unknown-host"


def require_local_directory(root: str, machine: str) -> None:
    """Refuse a root that is not a directory — but only on the machine it
    describes.

    A binding is a claim that a project's code lives *there*. Bound to a typo
    or a deleted folder the claim is still authoritative: the resolution
    ladder would answer with it, and the UNIQUE index makes it stick until
    someone unbinds it by hand. Cheaper to refuse at the bind.

    Machine-scoped, and that is the whole subtlety. A store synced between
    machines carries rows describing filesystems this process cannot see, and
    `add_project_root(..., machine="other-box")` exists precisely so a restore
    can replay them. Checking those would refuse every legitimate import for
    the crime of not running on that machine. So: check this machine's rows,
    take another machine's on trust — the only correct answer available here.

    Not a race-free guarantee and not meant as one; the directory can go away
    a millisecond later. `doctor`'s missing-roots check covers the after, this
    covers the typo.
    """
    if machine != this_machine():
        return
    if not Path(root).is_dir():
        raise RootNotADirectory(
            f"not a directory: {root} — a project root must be a directory "
            f"that exists on this machine ({this_machine()})"
        )


def _migration_001_sessions_reshape(conn: sqlite3.Connection) -> None:
    """Sessions become a work-session correlation entity (started_at/ended_at
    lifecycle, intent) instead of a summary bucket, and notes gain a
    session_id linking them to the session that produced them.

    The old sessions table was never written to by any released code, so it
    is dropped rather than converted."""
    conn.executescript("""
        DROP TABLE IF EXISTS sessions;
        CREATE TABLE sessions (
            id         TEXT PRIMARY KEY,
            project    TEXT,
            client     TEXT NOT NULL DEFAULT 'unknown',
            intent     TEXT,
            started_at TEXT NOT NULL,
            ended_at   TEXT,
            summary    TEXT
        );
        ALTER TABLE notes ADD COLUMN session_id TEXT;
    """)


def _migration_003_author_provenance(conn: sqlite3.Connection) -> None:
    """Which client wrote a note — 'cli' for direct human writes, the
    client name (e.g. 'claude-code') for MCP-tool writes, NULL for notes
    that predate this column (unknown, not misattributed). Lets responses
    mark AI-written notes as informational context, not instructions —
    a note is data the assistant produced, not a command to obey."""
    conn.execute("ALTER TABLE notes ADD COLUMN author_client TEXT")


def _migration_004_porter_stemming(conn: sqlite3.Connection) -> None:
    """Switch the FTS5 tokenizer to porter+unicode61 so word-form variants
    match (running/run, configured/configuring). Doesn't fix synonym gaps
    (auth/login) — that's synonyms.py's query-expansion job, a different
    axis. FTS5 tokenize= can't be ALTERed, so the virtual table is dropped
    and recreated, then rebuilt from the notes table it shadows; the AFTER
    INSERT/DELETE/UPDATE triggers reference it by name only and keep working
    unchanged once it exists again."""
    conn.executescript("""
        DROP TABLE IF EXISTS notes_fts;
        CREATE VIRTUAL TABLE notes_fts USING fts5(
            title, content,
            content=notes, content_rowid=rowid,
            tokenize='porter unicode61'
        );
        INSERT INTO notes_fts(notes_fts) VALUES ('rebuild');
    """)


# One source for the roots schema so `_init()` (fresh stores) and migration 005
# (existing stores) cannot drift.
#
# The uniqueness rule is the load-bearing part: **on a given machine, a
# directory belongs to at most one project.** Without it two projects can claim
# one directory and resolution has to pick, which is the arbitrary-pick-made-
# deterministic that `AmbiguousPrefix` rejects elsewhere in this file. Machine
# is part of the key because a synced store spans machines, where the same
# absolute path can legitimately be a different project.
PROJECT_ROOTS_TABLE = """
    CREATE TABLE IF NOT EXISTS project_roots (
        project    TEXT NOT NULL,
        root_path  TEXT NOT NULL,
        machine    TEXT NOT NULL,
        added_at   TEXT NOT NULL,
        PRIMARY KEY (project, root_path, machine)
    )
"""

# Separate from the table so the duplicate check can run between them: the
# index cannot be built over data that already violates it.
PROJECT_ROOTS_INDEX = """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_project_roots_path
        ON project_roots(root_path, machine)
"""


class DuplicateRootBinding(RuntimeError):
    """The store already binds one directory to several projects, so the
    uniqueness rule 0.1.4 introduces cannot be applied to it."""


class RootOwnedByAnotherProject(ValueError):
    """This directory is already bound to a different project on this machine."""

    def __init__(self, path: str, owner: str):
        self.path = path
        self.owner = owner
        super().__init__(f"'{path}' is already registered to project '{owner}'")


def _insert_root(conn: sqlite3.Connection, project: str, root: str, machine: str) -> None:
    """The one roots INSERT. Does not commit — the caller owns the transaction.

    DO NOTHING targets the primary key only, so re-binding the same directory
    to the same project is idempotent while a *cross-project* conflict still
    raises — that one violates the `(root_path, machine)` UNIQUE index, which
    this clause deliberately does not cover. `OR IGNORE` would have swallowed
    both and returned as though it had bound.

    The raise is translated to `RootOwnedByAnotherProject` here rather than
    left as a bare `IntegrityError`, because the only way to reach it is a
    second writer taking the directory between the caller's check and this
    insert — the two-process case this store is built for — and that path
    should end in the same explanation as the checked one.
    """
    try:
        conn.execute(
            "INSERT INTO project_roots (project, root_path, machine, added_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (project, root_path, machine) DO NOTHING",
            (project, root, machine, _now()),
        )
    except sqlite3.IntegrityError:
        owner = conn.execute(
            "SELECT project FROM project_roots WHERE root_path = ? AND machine = ?",
            (root, machine),
        ).fetchone()
        raise RootOwnedByAnotherProject(root, owner[0] if owner else "another project")


def _insert_project_row(conn: sqlite3.Connection, name: str, overview: str,
                        conventions: str, open_questions: str, author: str) -> None:
    """The one new-project INSERT. Does not commit.

    `author` only where there is an overview to attribute. This is the third
    writer of `description_author` — with `upsert_project` and
    `_update_description` — and all three follow `overview` alone, because
    that is what the field means everywhere it is surfaced ("who wrote the
    overview"). Two of them applying `overview or conventions` and one
    applying `overview` would give the same input two different answers.
    """
    conn.execute(
        "INSERT INTO projects (name, overview, conventions, open_questions, "
        "updated_at, description_author) VALUES (?, ?, ?, ?, ?, ?)",
        (name, overview, conventions, open_questions, _now(),
         author if overview else None),
    )


def _update_description(conn: sqlite3.Connection, name: str, overview: str,
                        conventions: str, author: str) -> None:
    """Replace a project's description. Does not commit.

    The provenance rule is conditional on the text actually changing, and it
    has to be the *same* rule `upsert_project` applies or the store has two
    writers that disagree: re-running `evoctx init --force` in an unchanged
    repo would otherwise clear a human's approval and reattribute the author,
    for text nobody touched. `IS NOT` is null-safe.
    """
    conn.execute(
        """
        UPDATE projects SET
            description_author = CASE
                WHEN ? IS NOT overview
                THEN ? ELSE description_author END,
            description_reviewed_at = CASE
                WHEN ? IS NOT overview OR ? IS NOT conventions
                THEN NULL ELSE description_reviewed_at END,
            overview = ?, conventions = ?, updated_at = ?
        WHERE name = ?
        """,
        # The two rules deliberately differ, see `upsert_project`.
        # And the same author guard as the INSERT path: no text, no author —
        # without it, blanking a description would stamp an author onto words
        # nobody wrote.
        (overview, author if overview else None,
         overview, conventions,
         overview, conventions, _now(), name),
    )


def _create_project_roots(conn: sqlite3.Connection) -> None:
    """Create the roots table and its uniqueness index, refusing loudly if the
    existing data already violates it.

    A `CREATE UNIQUE INDEX` over conflicting rows raises `IntegrityError`, and
    this runs from `_init()` on **every** open — so without the check a store
    holding a double-binding would simply stop opening, with a bare SQLite
    error and no way to tell what was wrong. That is the failure `SchemaTooNew`
    exists to prevent for the analogous case.

    No released version ever wrote this table, so in practice this only catches
    a store built by an in-development revision of this branch.
    """
    conn.execute(PROJECT_ROOTS_TABLE)
    dupes = conn.execute(
        "SELECT root_path, machine, COUNT(*) FROM project_roots "
        "GROUP BY root_path, machine HAVING COUNT(*) > 1"
    ).fetchall()
    if dupes:
        listed = "; ".join(f"{d[0]} (on {d[1]})" for d in dupes[:5])
        raise DuplicateRootBinding(
            f"{len(dupes)} directory binding(s) claim more than one project, which "
            f"this version does not allow: {listed}. One directory belongs to one "
            # ASCII: this is raised from Database.__init__, which cli.py runs at
            # import time, before main() reconfigures the streams to UTF-8.
            f"project per machine. Until this is resolved every evoctx command "
            f"will fail, including doctor. Inspect with:\n"
            f"  sqlite3 <store>/store.db \"SELECT project, root_path, machine FROM "
            f"project_roots ORDER BY root_path;\"\n"
            f"then delete the rows you don't want:\n"
            f"  sqlite3 <store>/store.db \"DELETE FROM project_roots WHERE project='<name>' "
            f"AND root_path='<path>';\""
        )
    conn.execute(PROJECT_ROOTS_INDEX)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """ALTER TABLE ADD COLUMN, but idempotent.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, and this store's `_init()` can
    create a table with the newest schema before the migration that adds those
    same columns has run — see `_migration_005_project_roots` for how.

    Not safe on an arbitrary table: `PRAGMA table_info` on a missing table
    returns an empty list rather than raising, so the guard passes and the
    ALTER then fails with "no such table". Every caller must know the table
    exists — `notes` is guaranteed by the `fresh` check, `projects` by
    `_init()`.
    """
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migration_005_project_roots(conn: sqlite3.Connection) -> None:
    """Bind projects to directories, and record provenance for descriptions.

    Until now the store had no idea where any project's code lives, so an
    assistant asked to name a project had nothing to match its workspace
    against and could only infer a name — which is where invented project
    names came from. `project_roots` is that missing fact.

    A table rather than a column on `projects`: one project legitimately has
    several roots (a worktree, a work machine and a home one, a monorepo
    subdirectory), and a column would force last-write-wins and silently drop
    the earlier path. Path is a strong hint, never the identity — name stays
    the key, so a synced store, a clone, and a project with no directory at
    all all keep working.

    The three `projects` columns and `notes.is_global` ride along because
    each is provenance for something that was previously unrecorded: who
    wrote a description, whether a human ever reviewed it, whether the user
    declined to complete a project, and whether a note is deliberately
    cross-project rather than merely missing a project.
    """
    _create_project_roots(conn)
    # Add-if-missing, not a bare ALTER, because `_init()` runs BEFORE
    # `_migrate()` and creates the *current* schema for any table the old
    # store happens to lack. An old store without a `projects` table gets one
    # from `_init()` already carrying these columns, and a bare ALTER would
    # then fail with "duplicate column name" — taking the whole store down on
    # open. Migrations 001-004 never hit this only because their tables always
    # pre-existed.
    _add_column_if_missing(conn, "projects", "description_author", "TEXT")
    _add_column_if_missing(conn, "projects", "description_reviewed_at", "TEXT")
    _add_column_if_missing(conn, "projects", "attention_dismissed_at", "TEXT")
    _add_column_if_missing(conn, "notes", "is_global", "INTEGER NOT NULL DEFAULT 0")


# Ordered schema migrations. Each entry runs once, in order, tracked via
# PRAGMA user_version. _init() creates the CURRENT schema for fresh stores
# (which then skip straight to the latest version); migrations only ever
# run on stores created by an older release. Append, never reorder or edit
# shipped entries.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migration_001_sessions_reshape,
    migration_002_audit_log,
    _migration_003_author_provenance,
    _migration_004_porter_stemming,
    _migration_005_project_roots,
]


class SchemaTooNew(RuntimeError):
    """The store was created by a newer release than this code."""


class AmbiguousPrefix(ValueError):
    """An id prefix matches more than one record. Carries the candidates so
    the caller can show them instead of just refusing."""

    def __init__(self, prefix: str, ids: list[str], total: int | None = None):
        self.prefix = prefix
        self.ids = ids                      # up to 5, for display
        self.total = len(ids) if total is None else total
        shown = ", ".join(i[:12] for i in ids)
        rest = f" (showing {len(ids)})" if self.total > len(ids) else ""
        super().__init__(
            f"'{prefix}' matches {self.total} records{rest}: {shown}"
        )


class NoteTooLarge(ValueError):
    """Content exceeds MAX_NOTE_BYTES. Raised before the INSERT — nothing
    partial gets written."""


class ConflictingNoteScope(ValueError):
    """A note claimed to be both global and scoped to a project.

    Its own class rather than a bare ValueError so callers can catch this
    specifically — the sibling guard in `add_project_root` gets
    `RootOwnedByAnotherProject` for the same reason.
    """


class NoSuchProject(ValueError):
    """A root was offered for a project that does not exist."""


class RootNotADirectory(ValueError):
    """A root was offered for a path that is not a directory on this machine."""


MAX_NOTE_BYTES = 256 * 1024  # 256KB — generous for real notes, not for accidental blobs

# `get_project`'s recent-note preview. Two separate bounds, not one over the
# union: global notes are shared across the store and never age out, so a
# single limit lets them displace everything a project has.
RECENT_OWN_NOTES = 5
RECENT_GLOBAL_NOTES = 3


class Database:
    def __init__(self, store_dir: Path):
        store_dir.mkdir(parents=True, exist_ok=True)
        self.store_dir = store_dir
        self.conn = sqlite3.connect(str(store_dir / "store.db"), check_same_thread=False)
        # WAL: readers don't block writers and vice versa — the CLI, the MCP
        # server, and (via evoctx serve's execv) a second process can all
        # open this file without "database is locked" on the first write.
        # busy_timeout: for the writer-vs-writer case WAL doesn't cover,
        # retry for 5s instead of failing immediately.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            self.conn.close()
            raise SchemaTooNew(
                f"Store schema is version {version}, but this build only knows "
                f"{len(MIGRATIONS)}. The store was migrated by a newer release — "
                f"upgrade the package (pip install -U evoctx) instead of "
                f"running old code against it."
            )
        fresh = not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes'"
        ).fetchone()
        # Close before propagating, matching the SchemaTooNew branch above: an
        # open handle keeps store.db-wal locked on Windows, so a store that
        # refuses to open would also refuse to be repaired.
        try:
            # BEFORE _init(), not inside _migrate(). `_init()` creates any
            # table the old store lacks and commits, so a backup taken after
            # it is not a pre-upgrade copy — and `_create_project_roots` can
            # raise *after* that commit, which would leave a written-to store
            # with no backup at all.
            backup = None
            if not fresh and version < len(MIGRATIONS):
                backup = self._backup_before_migrating(version, len(MIGRATIONS))
            self._init()
            if fresh:
                self.conn.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
                self.conn.commit()
            else:
                self._migrate()
                if backup:
                    # ASCII only. cli.py builds its Database at import time,
                    # before main() reconfigures the streams to UTF-8, so this
                    # is the one line of a run that escapes that fix — and it
                    # is the line naming the only rollback copy of a one-way
                    # migration, so it is the worst one to render as mojibake
                    # in a console codepage or a captured stderr log.
                    print(f"evoctx: migrated store v{version} -> v{len(MIGRATIONS)}. "
                          f"Pre-upgrade copy kept at {backup.name}. Older evoctx "
                          f"builds cannot open the upgraded store.",
                          file=sys.stderr)
        except Exception:
            self.conn.close()
            raise

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id       TEXT PRIMARY KEY,
                title    TEXT NOT NULL,
                content  TEXT NOT NULL,
                project  TEXT,
                tags     TEXT NOT NULL DEFAULT '[]',
                session_id TEXT,
                author_client TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                -- last, matching where ALTER TABLE appends it, so a store
                -- migrated from v4 lines up with a fresh one. Not a general
                -- guarantee: a store migrated from pre-001 already differs
                -- earlier, because 001 and 003 appended session_id and
                -- author_client after updated_at. Nothing depends on the
                -- order — every INSERT here names its columns — so this is
                -- tidiness, not a contract.
                is_global INTEGER NOT NULL DEFAULT 0
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                title, content,
                content=notes, content_rowid=rowid,
                tokenize='porter unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
                INSERT INTO notes_fts(rowid, title, content)
                VALUES (new.rowid, new.title, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, title, content)
                VALUES ('delete', old.rowid, old.title, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, title, content)
                VALUES ('delete', old.rowid, old.title, old.content);
                INSERT INTO notes_fts(rowid, title, content)
                VALUES (new.rowid, new.title, new.content);
            END;

            CREATE TABLE IF NOT EXISTS projects (
                name           TEXT PRIMARY KEY,
                overview       TEXT NOT NULL DEFAULT '',
                conventions    TEXT NOT NULL DEFAULT '',
                open_questions TEXT NOT NULL DEFAULT '',
                updated_at     TEXT NOT NULL,
                description_author      TEXT,
                description_reviewed_at TEXT,
                attention_dismissed_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id         TEXT PRIMARY KEY,
                project    TEXT,
                client     TEXT NOT NULL DEFAULT 'unknown',
                intent     TEXT,
                started_at TEXT NOT NULL,
                ended_at   TEXT,
                summary    TEXT
            );
        """)
        _create_project_roots(self.conn)
        self.conn.executescript(AUDIT_SCHEMA)
        self.conn.commit()

    @staticmethod
    def _usable_backup(path: Path, expect_version: int) -> bool:
        """Is this file actually a restorable copy at the expected schema?

        Existence is not enough. An interrupted backup — Ctrl-C, a full disk,
        antivirus quarantine — leaves a file with the right name and no
        content, which is exactly the case the "reuse an earlier attempt"
        path is meant to serve. Trusting it would tell the user they hold a
        rollback of a one-way migration when they hold nothing, which is
        worse than reporting no backup at all.
        """
        if not path.exists() or path.stat().st_size == 0:
            return False
        try:
            with closing(sqlite3.connect(str(path))) as c:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    return False
                return c.execute("PRAGMA user_version").fetchone()[0] == expect_version
        except sqlite3.Error:
            return False

    def _backup_before_migrating(self, from_version: int, to_version: int) -> Path | None:
        """Copy the store before any migration touches it.

        Migrations are one-way: once a store is at version N, a build that
        only knows N-1 refuses to open it (`SchemaTooNew`). That is correct —
        running old code against a newer schema is worse — but it means an
        upgrade is irreversible, and this store is explicitly built to be
        synced between machines that may not upgrade at the same time.

        Uses SQLite's backup API rather than copying the file: in WAL mode a
        plain copy of `store.db` misses everything still in `store.db-wal`.

        Best-effort. A store that cannot be backed up (no disk space, a
        read-only directory) should still open — losing the ability to use
        evoctx is a worse outcome than an un-backed-up upgrade, so this warns
        and continues rather than raising.
        """
        dest = self.store_dir / f"store.db.bak-pre-migration-{to_version:03d}"
        if self._usable_backup(dest, from_version):
            return dest  # a completed earlier attempt at the same upgrade
        # Write to a temp name and rename only once it verifies, so a failure
        # can never leave a plausible-looking file that isn't restorable.
        tmp = dest.with_name(dest.name + ".tmp")
        try:
            tmp.unlink(missing_ok=True)
            with closing(sqlite3.connect(str(tmp))) as target:
                self.conn.backup(target)
            if not self._usable_backup(tmp, from_version):
                raise sqlite3.DatabaseError("backup did not verify")
            os.replace(tmp, dest)
            return dest
        except Exception as e:
            tmp.unlink(missing_ok=True)
            print(f"evoctx: could not back up the store before migrating "
                  f"v{from_version} -> v{to_version} ({e}). Continuing; the "
                  f"upgrade is one-way, so export first if that matters: "
                  f"evoctx export --format json backup.json",
                  file=sys.stderr)
            return None

    def _migrate(self):
        # The backup is taken by __init__ before _init() runs — see there for
        # why it cannot live here.
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, migration in enumerate(MIGRATIONS[version:], start=version + 1):
            migration(self.conn)
            self.conn.execute(f"PRAGMA user_version = {i}")
            self.conn.commit()

    # ── search ────────────────────────────────────────────────────────────────

    def search(self, query: str, project: str | None = None, limit: int = 10, offset: int = 0) -> list[dict]:
        # Synonym expansion only applies to the FTS5 MATCH path — an
        # OR-group like "(auth OR login)" is valid MATCH syntax but would
        # search for that literal parenthesized text under the LIKE
        # fallback, so the fallback below deliberately re-derives `like`
        # from the original, unexpanded `query`.
        # Every ordering below is total, and deliberately so: _fetch_filtered
        # in policy.py pages this with LIMIT/OFFSET, and a non-total order
        # under paging can repeat or skip a row between one page and the next.
        # bm25 ties are not hypothetical — same-shape notes score identically.
        #
        # Don't "optimise" the tiebreaker back out when you see the plan: the
        # rowid term does cost FTS5's rank pushdown. EXPLAIN QUERY PLAN goes
        # from `INDEX 32:M2` (internal rank sort, early exit at LIMIT+OFFSET)
        # to `INDEX 0:M2` + `USE TEMP B-TREE FOR ORDER BY` (sort all matches,
        # re-run per page). That is a real cost and an accepted one — these
        # are personal stores, and a correct page beats a fast one that can
        # drop a note.
        # A project-scoped search includes global notes. "Deliberately spans
        # every project" is a claim about scope, so a global note that never
        # appears under any project would mean nothing — and the server tells
        # clients exactly that, so the query has to honour it.
        #
        # They compete on equal terms here, in BOTH branches. Search answers
        # "what matches", and a global note that matches better deserves its
        # place; biasing one branch and not the other would make the same
        # query order differently depending on whether FTS could parse it.
        # `get_project`'s recent_notes is the opposite case — it answers
        # "what is in this project", so there the two sets are bounded
        # separately and globals cannot displace the project's own.
        fts_query = expand_query(query)
        try:
            if project:
                rows = self.conn.execute("""
                    SELECT n.id, n.title,
                           snippet(notes_fts, 1, '', '', '...', 32) AS snippet,
                           n.project, n.tags, n.created_at, n.author_client, n.is_global
                    FROM notes_fts
                    JOIN notes n ON n.rowid = notes_fts.rowid
                    WHERE notes_fts MATCH ? AND (n.project = ? OR n.is_global = 1)
                    ORDER BY rank, n.rowid DESC LIMIT ? OFFSET ?
                """, (fts_query, project, limit, offset)).fetchall()
            else:
                rows = self.conn.execute("""
                    SELECT n.id, n.title,
                           snippet(notes_fts, 1, '', '', '...', 32) AS snippet,
                           n.project, n.tags, n.created_at, n.author_client, n.is_global
                    FROM notes_fts
                    JOIN notes n ON n.rowid = notes_fts.rowid
                    WHERE notes_fts MATCH ?
                    ORDER BY rank, n.rowid DESC LIMIT ? OFFSET ?
                """, (fts_query, limit, offset)).fetchall()
        except sqlite3.OperationalError:
            like = f"%{query}%"
            if project:
                rows = self.conn.execute("""
                    SELECT id, title, substr(content, 1, 200), project, tags, created_at, author_client, is_global
                    FROM notes WHERE (title LIKE ? OR content LIKE ?)
                      AND (project = ? OR is_global = 1)
                    ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
                """, (like, like, project, limit, offset)).fetchall()
            else:
                rows = self.conn.execute("""
                    SELECT id, title, substr(content, 1, 200), project, tags, created_at, author_client, is_global
                    FROM notes WHERE title LIKE ? OR content LIKE ?
                    ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
                """, (like, like, limit, offset)).fetchall()

        return [
            {
                "id": r[0], "title": r[1], "snippet": r[2],
                "project": r[3], "tags": json.loads(r[4] or "[]"),
                "created_at": r[5], "author_client": r[6],
                # deliberately cross-project, as opposed to merely having no
                # project set — `project` stays null either way, so without
                # this the two are indistinguishable on read
                "is_global": bool(r[7]),
            }
            for r in rows
        ]

    # ── reads ─────────────────────────────────────────────────────────────────

    def get_note(self, note_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, title, content, project, tags, created_at, updated_at, author_client, is_global "
            "FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "title": row[1], "content": row[2],
            "project": row[3], "tags": json.loads(row[4] or "[]"),
            "created_at": row[5], "updated_at": row[6], "author_client": row[7],
            "is_global": bool(row[8]),
        }

    # tables whose TEXT id may be addressed by prefix. Named explicitly
    # because _id_by_prefix interpolates the table name into SQL.
    _PREFIX_TABLES = ("notes", "sessions")

    def _id_by_prefix(self, table: str, prefix: str) -> str | None:
        """Resolve a full id from a prefix, or raise AmbiguousPrefix.

        Adding an ORDER BY and taking the first row would have been the wrong
        fix. These are truncated queries, but ordering one only makes the
        arbitrary pick *deterministic*, not correct: the caller asked for one
        record and there is no basis for choosing among several, so refuse —
        the way git refuses an ambiguous rev. It matters because
        `evoctx delete <prefix> --force` skips the confirmation prompt, and a
        delete is not undoable.
        """
        if table not in self._PREFIX_TABLES:
            raise ValueError(f"prefix lookup not supported for table {table!r}")
        # LIKE metacharacters in a typo'd prefix would otherwise widen the
        # match silently: 'a_c3' matches 'abc3...' as well as 'a_c3...', and a
        # widened match that happens to hit exactly one row deletes it without
        # ever reporting ambiguity.
        pattern = (prefix.replace("\\", r"\\")
                         .replace("%", r"\%")
                         .replace("_", r"\_")) + "%"
        rows = self.conn.execute(
            f"SELECT id FROM {table} WHERE id LIKE ? ESCAPE '\\' ORDER BY id LIMIT 5",
            (pattern,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE id LIKE ? ESCAPE '\\'",
                (pattern,),
            ).fetchone()[0]
            raise AmbiguousPrefix(prefix, [r[0] for r in rows], total)
        return rows[0][0]

    def find_note_by_prefix(self, prefix: str) -> dict | None:
        note_id = self._id_by_prefix("notes", prefix)
        return self.get_note(note_id) if note_id else None

    def find_session_by_prefix(self, prefix: str) -> dict | None:
        session_id = self._id_by_prefix("sessions", prefix)
        return self.get_session(session_id) if session_id else None

    def get_project(self, name: str, with_roots: bool = True) -> dict | None:
        row = self.conn.execute(
            "SELECT name, overview, conventions, open_questions, updated_at, "
            "description_author, description_reviewed_at, attention_dismissed_at "
            "FROM projects WHERE name = ?",
            (name,),
        ).fetchone()
        if not row:
            return None

        # Two bounded queries, never one merged LIMIT. Global notes apply to
        # this project so they belong here — but they are shared across the
        # whole store and never age out, so a single `LIMIT 5` over the union
        # lets five global notes push out every one of the project's own.
        # `get_project` is the session-start read; returning a project record
        # whose recent notes say nothing about that project is worse than
        # omitting globals entirely.
        # rowid DESC tiebreaker: a created_at tie would otherwise decide which
        # note makes the cut nondeterministically.
        # rowid is selected so the Python sort below can use the SAME
        # tiebreaker the queries do. Sorting on `id` instead would order ties
        # by a random uuid4, so which notes survive the LIMIT (decided by
        # rowid) and the order they print in would follow different rules.
        _RECENT = ("SELECT id, title, tags, created_at, author_client, is_global, rowid "
                   "FROM notes WHERE {} ORDER BY created_at DESC, rowid DESC LIMIT ?")
        recent = self.conn.execute(
            _RECENT.format("project = ?"), (name, RECENT_OWN_NOTES)
        ).fetchall()
        recent += self.conn.execute(
            _RECENT.format("is_global = 1"), (RECENT_GLOBAL_NOTES,)
        ).fetchall()
        # bound first, then order: the two queries protect the project's own
        # notes from being displaced, and this restores the recency the key
        # name promises. Appending the second block unsorted would leave
        # element 6 newer than element 1, which a reader taking position for
        # recency would get wrong.
        recent.sort(key=lambda r: (r[3], r[6]), reverse=True)  # created_at, rowid

        return {
            "name": row[0], "overview": row[1],
            "conventions": row[2], "open_questions": row[3],
            "updated_at": row[4],
            # provenance: who wrote this description, whether a human ever
            # approved it, and whether the user declined to complete it
            "description_author": row[5],
            "description_reviewed_at": row[6],
            "attention_dismissed_at": row[7],
            # full rows, not bare paths: a synced store holds roots that
            # describe other machines' filesystems, and a caller asking "does
            # this project live anywhere here" must be able to tell them apart
            # rather than treating a laptop's directory as present locally.
            #
            # `with_roots=False` OMITS the key rather than returning [], so
            # "not fetched" cannot be mistaken for "genuinely has none" — a
            # caller skipping the query for speed and then testing
            # `if not proj["roots"]` would otherwise get a confident wrong
            # answer, which is the failure mode this whole release is about.
            **({"roots": self.project_roots(name)} if with_roots else {}),
            "recent_notes": [
                # is_global carried here too: this is the one list that mixes
                # a project's own notes with cross-project ones, so without it
                # a reader cannot tell which is which — the conflation the
                # flag exists to remove
                {"id": r[0], "title": r[1], "tags": json.loads(r[2] or "[]"),
                 "created_at": r[3], "author_client": r[4], "is_global": bool(r[5])}
                for r in recent
            ],
        }

    def list_projects(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT name FROM projects ORDER BY name").fetchall()]

    def project_exists(self, name: str) -> bool:
        """Does this name exist, without paying for the record?

        `get_project()` would answer it too, at the cost of the two
        recent-notes queries and a roots lookup. Resolution asks this once per
        call and the coming refusal path asks it on every write, so the cheap
        form is the one to reach for when the answer is only yes-or-no.
        """
        return self.conn.execute(
            "SELECT 1 FROM projects WHERE name = ?", (name,)
        ).fetchone() is not None

    def list_notes(self, project: str | None = None) -> list[dict]:
        q = "SELECT id, title, project, tags, created_at, author_client, is_global FROM notes"
        params: list = []
        if project:
            # same rule as search() and get_project(): a note that spans every
            # project is in this one. Two adjacent commands answering "what is
            # in X" with different sets would be a defect whichever answer is
            # right.
            q += " WHERE (project = ? OR is_global = 1)"
            params.append(project)
        q += " ORDER BY created_at DESC, rowid DESC"
        return [
            {
                "id": r[0], "title": r[1], "project": r[2],
                "tags": json.loads(r[3] or "[]"), "created_at": r[4],
                "author_client": r[5], "is_global": bool(r[6]),
            }
            for r in self.conn.execute(q, params).fetchall()
        ]

    # ── sessions ──────────────────────────────────────────────────────────────

    def start_session(
        self,
        project: str | None = None,
        intent: str | None = None,
        client: str = "unknown",
    ) -> str:
        if intent:
            intent, _ = redact_secrets(intent)
        session_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO sessions (id, project, client, intent, started_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, project, client, intent, _now()),
        )
        self.conn.commit()
        return session_id

    def end_session(self, session_id: str, summary: str) -> bool:
        summary, _ = redact_secrets(summary)
        cur = self.conn.execute(
            "UPDATE sessions SET ended_at = ?, summary = ? WHERE id = ? AND ended_at IS NULL",
            (_now(), summary, session_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def latest_open_session(self, project: str | Sequence[str] | None = None,
                            include_unassigned: bool = False) -> dict | None:
        """The newest session nobody closed, optionally narrowed to projects.

        Several names, not one, because a workspace is not one project. The
        Stop hook asks about the workspace it fired in, and that workspace is
        routinely several sibling repos — the assistant is explicitly told to
        name whichever one the work targets. Narrowed to the launching folder
        alone, the hook goes silent for a session opened under any of the
        others, which is worse than the machine-wide nagging it replaced: a
        wrong reminder is noise, a missing one loses the session.

        `include_unassigned` is off by default, because the two callers want
        opposite things. `evoctx end-session --project X` means that project
        and nothing else; guessing a project-less session was meant would
        close the wrong one, and closing is not undoable. The hook only
        reminds, and a session started with no project is the one most likely
        to be forgotten.

        No names — empty sequence or None — means every project, which is the
        pre-0.1.4 behaviour and the right degradation for a workspace that
        resolves to nothing. Filtering to nothing instead would silence the
        reminder precisely where the store knows least.
        """
        names = [project] if isinstance(project, str) else list(project or [])
        names = [n for n in names if n]
        q = "SELECT id FROM sessions WHERE ended_at IS NULL"
        params: list = []
        if names:
            placeholders = ", ".join("?" * len(names))
            q += (f" AND (project IN ({placeholders}) OR project IS NULL)"
                  if include_unassigned else f" AND project IN ({placeholders})")
            params.extend(names)
        # Every tiebreaker in this file assumes rowid ascends with insertion
        # time. That survives deletes (a reused max rowid still exceeds every
        # live row) and is planner-independent. `VACUUM` is what breaks it:
        # these tables have TEXT primary keys, so their rowids are implicit,
        # and VACUUM may renumber those — which would also desync notes_fts,
        # keyed on content_rowid=rowid. Nothing here ever VACUUMs; a user
        # running it by hand on store.db is the exposure.
        #
        # rowid DESC is the tiebreaker, not decoration: started_at comes from
        # datetime.now(), whose resolution on Windows is coarse enough that two
        # sessions started back to back land on the identical ISO string ~3% of
        # the time. With no tiebreaker SQLite returns them in scan order, so
        # LIMIT 1 hands back the OLDER session — measured wrong on 62 of 62
        # ties. `evoctx end-session` defaults to this session, so the summary
        # would land on the wrong one and leave the intended one open.
        q += " ORDER BY started_at DESC, rowid DESC LIMIT 1"
        row = self.conn.execute(q, params).fetchone()
        return self.get_session(row[0]) if row else None

    def get_session(self, session_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, project, client, intent, started_at, ended_at, summary FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return self._session_dict(row) if row else None

    def recent_sessions(self, project: str | None = None, limit: int = 10, offset: int = 0) -> list[dict]:
        q = """
            SELECT s.id, s.project, s.client, s.intent, s.started_at, s.ended_at, s.summary
            FROM sessions s
        """
        params: list = []
        if project:
            q += " WHERE s.project = ?"
            params.append(project)
        # same tiebreaker, and it matters more here: this query is paged
        # through by PolicyEngine._fetch_filtered, and an order that isn't
        # total can repeat or skip a row between one page and the next
        q += " ORDER BY s.started_at DESC, s.rowid DESC LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)
        return [self._session_dict(r) for r in self.conn.execute(q, params).fetchall()]

    def _session_dict(self, row) -> dict:
        note_rows = self.conn.execute(
            "SELECT id, title FROM notes WHERE session_id = ? ORDER BY created_at, rowid",
            (row[0],),
        ).fetchall()
        return {
            "id": row[0], "project": row[1], "client": row[2], "intent": row[3],
            "started_at": row[4], "ended_at": row[5], "summary": row[6],
            # open = never closed with end_session. If it isn't the currently
            # running session, it likely crashed or was abandoned — its notes
            # are the only record of what happened
            "open": row[5] is None,
            "notes": [{"id": n[0], "title": n[1]} for n in note_rows],
        }

    # ── writes ────────────────────────────────────────────────────────────────

    def add_note(
        self,
        title: str,
        content: str,
        project: str | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        author_client: str | None = None,
        is_global: bool = False,
    ) -> str:
        if is_global and project:
            # "deliberately cross-project" and "belongs to this project" are
            # contradictory claims; storing both would leave every reader to
            # invent its own precedence rule.
            raise ConflictingNoteScope(
                f"a note cannot be both global and scoped to project '{project}'"
            )
        size = len(content.encode("utf-8"))
        if size > MAX_NOTE_BYTES:
            raise NoteTooLarge(
                f"Note content is {size:,} bytes, over the {MAX_NOTE_BYTES:,} byte limit. "
                f"Split it into multiple notes."
            )
        # Built-in secret patterns are masked here, unconditionally — this is
        # the one place every write path (CLI, MCP tool, sync/import) funnels
        # through, so a leaked key never sits raw on disk regardless of entry
        # point or which grant happens to be active.
        title, _ = redact_secrets(title)
        content, _ = redact_secrets(content)
        note_id = str(uuid.uuid4())
        now = _now()
        self.conn.execute(
            "INSERT INTO notes (id, title, content, project, tags, session_id, author_client, "
            "is_global, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (note_id, title, content, project, json.dumps(tags or []), session_id,
             author_client, int(is_global), now, now),
        )
        self.conn.commit()
        return note_id

    def export_all(self) -> dict:
        """Full dump for `evoctx export` — every project, every note (full
        content), every session. The portability story made checkable.

        Note for whoever writes the importer: sessions come out **newest
        first** (recent_sessions' order), while `latest_open_session()` and
        `recent_sessions()` break `started_at` ties with `rowid DESC` — i.e.
        they assume rowid ascends with insertion time. An importer that
        inserts sessions in this dump's order would invert that, and ties
        would silently resolve to the *older* session again. Insert
        oldest-first, or sort on `started_at` before inserting.
        """
        # roots live in exactly one place in the dump — the top-level key —
        # so an importer has one source for the fact rather than two that can
        # disagree. get_project() carries them for readers; the export
        # deliberately does not repeat them per project.
        projects = []
        for name in self.list_projects():
            proj = self.get_project(name, with_roots=False)
            if proj is not None:
                projects.append(proj)
        notes = [
            self.get_note(r[0])
            for r in self.conn.execute(
                "SELECT id FROM notes ORDER BY created_at, rowid"
            ).fetchall()
        ]
        sessions = self.recent_sessions(limit=1_000_000)
        # Roots carry their `machine`, so a future importer can apply only the
        # ones true on the machine it is restoring to and keep the rest as
        # inert history. Dropping them would put every project back to
        # root-less on restore — the exact state this release exists to leave.
        # Note this makes an export describe your filesystem layout.
        return {
            "projects": projects,
            "notes": notes,
            "sessions": sessions,
            "project_roots": self.project_roots(),
        }

    def upsert_project(
        self,
        name: str,
        overview: str = "",
        conventions: str = "",
        open_questions: str = "",
        author: str = "cli",
    ) -> None:
        """Write a project's description.

        `author` defaults to "cli" because every caller in this codebase is a
        CLI command; it is hardcoded rather than read from CONTEXT_CLIENT so a
        leaked env var can't make a human's own edit look like an assistant's.

        Two provenance rules ride on this write, both conditional on the text
        actually changing (`IS NOT` is null-safe, so a re-write of identical
        content is not a change). They deliberately watch **different fields**:

        - `description_author` follows `overview` **only**. It answers "who
          wrote the overview", which is what every surface says it means, so a
          conventions-only edit must not touch it — otherwise editing your own
          conventions stamps `cli` ("a human typed it") onto an overview that
          `init` scraped, which is the exact conflation this release removes.
        - `description_reviewed_at` is cleared when **either** changes, and
          whatever the author. A review approves the record as a whole, so any
          change to it invalidates the approval; and clearing on non-cli
          writes only would leave a human's rewrite carrying the previous
          text's review.
        """
        self.conn.execute(
            """
            INSERT INTO projects (name, overview, conventions, open_questions, updated_at,
                                  description_author)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                overview       = excluded.overview,
                conventions    = excluded.conventions,
                open_questions = excluded.open_questions,
                description_author = CASE
                    WHEN excluded.overview IS NOT projects.overview
                    THEN excluded.description_author
                    ELSE projects.description_author END,
                description_reviewed_at = CASE
                    WHEN excluded.overview IS NOT projects.overview
                      OR excluded.conventions IS NOT projects.conventions
                    THEN NULL
                    ELSE projects.description_reviewed_at END,
                updated_at     = excluded.updated_at
            """,
            # no overview, no author: a project created with only conventions
            # has nobody to credit for an overview that does not exist
            (name, overview, conventions, open_questions, _now(),
             author if overview else None),
        )
        self.conn.commit()

    # ── project roots ─────────────────────────────────────────────────────────

    def add_project_root(self, project: str, path, machine: str | None = None) -> str:
        """Bind a directory to a project, touching nothing else.

        Deliberately separate from `upsert_project`: recording where code
        lives must never be a reason to rewrite a description someone
        curated. Returns the normalized path actually stored.

        Three guards, all here rather than in a caller, because this is the
        function every front-end goes through — the CLI today, the MCP
        registration tool next:

        - **the project must exist.** A root pointing at no project is the
          same stranded-namespace bug as a note filed under an invented name,
          except worse: resolution would answer with it authoritatively.
        - **the path must be a directory here** (`require_local_directory`),
          unless the row deliberately describes another machine.
        - **the directory must not belong to another project on this machine.**
          Enforced by a UNIQUE index too, but caught here so the caller gets
          the owner's name instead of an IntegrityError.
        """
        root = normalize_root(require_absolute(path))
        machine = machine or this_machine()
        require_local_directory(root, machine)
        if not self.conn.execute(
            "SELECT 1 FROM projects WHERE name = ?", (project,)
        ).fetchone():
            raise NoSuchProject(
                f"no project named '{project}' — create it before binding a directory"
            )
        owner = self.project_for_root(root, machine)
        if owner and owner != project:
            raise RootOwnedByAnotherProject(root, owner)
        _insert_root(self.conn, project, root, machine)
        self.conn.commit()
        return root

    def register_project(self, name: str, path, overview: str = "",
                         conventions: str = "", open_questions: str = "",
                         author: str = "cli", relink: bool = False,
                         overwrite_description: bool = False) -> str:
        """Create-or-link a project and bind a directory, atomically.

        The one function both front-ends go through — `evoctx init` today, the
        MCP registration tool next. It exists because the correct order is
        check → create → bind, and a caller doing that by hand leaves a
        project committed with no root when the bind refuses. Here the whole
        thing is one transaction, so a refusal leaves the store untouched.

        Linking is the main case, not the edge: every project that predates
        roots has none, so "this name exists, it just has no path" is what
        most calls are. An existing project keeps its description — recording
        where code lives must never cost words someone wrote — unless the
        caller explicitly asks for `overwrite_description`, which is what
        `evoctx init --force` means. That has to happen inside this
        transaction too, or a failure leaves a re-scraped description bound to
        nothing.
        """
        root = normalize_root(require_absolute(path))
        machine = this_machine()
        # before the transaction opens: a refusal here has nothing to roll back
        require_local_directory(root, machine)
        with self.conn:  # one transaction: refuse and nothing was written
            owner = self.conn.execute(
                "SELECT project FROM project_roots WHERE root_path = ? AND machine = ?",
                (root, machine),
            ).fetchone()
            if owner and owner[0] != name:
                if not relink:
                    raise RootOwnedByAnotherProject(root, owner[0])
                self.conn.execute(
                    "DELETE FROM project_roots WHERE root_path = ? AND machine = ?",
                    (root, machine),
                )
            exists = self.conn.execute(
                "SELECT 1 FROM projects WHERE name = ?", (name,)
            ).fetchone()
            if not exists:
                _insert_project_row(self.conn, name, overview, conventions,
                                    open_questions, author)
            elif overwrite_description:
                _update_description(self.conn, name, overview, conventions, author)
            _insert_root(self.conn, name, root, machine)
        return root

    def remove_project_root(self, project: str, path, machine: str | None = None) -> bool:
        """Unbind a directory on ONE machine.

        Machine-scoped for the same reason binding is: a synced store holds
        rows describing other machines' filesystems, and unbinding here must
        not silently delete a laptop's binding from a desktop. Pass `machine`
        explicitly only to target another machine's row deliberately.
        """
        cur = self.conn.execute(
            "DELETE FROM project_roots WHERE project = ? AND root_path = ? AND machine = ?",
            (project, normalize_root(require_absolute(path)), machine or this_machine()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def project_roots(self, project: str | None = None,
                      machine: str | None = None) -> list[dict]:
        """Roots, optionally narrowed to one project and/or one machine.

        `machine=None` means *every* machine — right for export and for
        showing a user their whole store, wrong for "does this project have a
        directory here". Callers that mean the latter must say so; that
        distinction is the whole point of the column.
        """
        q = "SELECT project, root_path, machine, added_at FROM project_roots"
        clauses, params = [], []
        # `is not None`, not truthiness: an empty string is a filter that
        # matches nothing, whereas falling through to no-WHERE returns
        # everything. Getting that backwards makes the filter fail open, which
        # for the machine filter means another machine's directory resolving
        # as local.
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if machine is not None:
            clauses.append("machine = ?")
            params.append(machine)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY project, root_path"
        return [
            {"project": r[0], "root_path": r[1], "machine": r[2], "added_at": r[3]}
            for r in self.conn.execute(q, params).fetchall()
        ]

    def project_for_root(self, path, machine: str | None = None) -> str | None:
        """Which project claims exactly this directory on this machine.

        Exact match, not prefix — prefix resolution needs the longest-match
        rule and belongs to the resolution ladder, not here. This exists so
        registration can detect "that directory already belongs to something
        else" and ask.

        At most one row can match: the UNIQUE index on (root_path, machine)
        makes a double binding unrepresentable, so there is never a pick to
        make. Filtering by machine is what keeps a synced store honest — a
        path recorded on another machine describes that machine's filesystem,
        not this one's.
        """
        row = self.conn.execute(
            "SELECT project FROM project_roots WHERE root_path = ? AND machine = ?",
            (normalize_root(require_absolute(path)), machine or this_machine()),
        ).fetchone()
        return row[0] if row else None

    # ── deletes (CLI-only — never exposed as an MCP tool) ───────────────────────

    def delete_note(self, note_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_notes_by_project(self, project: str) -> int:
        cur = self.conn.execute("DELETE FROM notes WHERE project = ?", (project,))
        self.conn.commit()
        return cur.rowcount

    def delete_notes_by_tag(self, tag: str) -> int:
        # tags is a JSON array column; json_each un-nests it for the match
        cur = self.conn.execute(
            "DELETE FROM notes WHERE id IN ("
            "  SELECT n.id FROM notes n, json_each(n.tags) t WHERE t.value = ?"
            ")",
            (tag,),
        )
        self.conn.commit()
        return cur.rowcount

    def delete_project(self, name: str, with_notes: bool = False) -> bool:
        # One transaction across all three deletes. Previously the notes
        # delete committed on its own, so a crash before the rest left the
        # notes gone and the project intact — a half-delete of a destructive
        # operation. `register_project` sets the standard.
        #
        # Roots go with the project. Leaving them would strand rows pointing at
        # a project that no longer exists — resolution would then answer a
        # directory with a name `get_project()` denies, which is exactly the
        # phantom this release exists to remove, reached through a delete
        # instead of a guess. It also makes the directory un-registerable:
        # `init` would refuse it as owned by the deleted project.
        with self.conn:
            deleted_notes = 0
            if with_notes:
                deleted_notes = self.conn.execute(
                    "DELETE FROM notes WHERE project = ?", (name,)
                ).rowcount
            self.conn.execute("DELETE FROM project_roots WHERE project = ?", (name,))
            cur = self.conn.execute("DELETE FROM projects WHERE name = ?", (name,))
        return cur.rowcount > 0 or deleted_notes > 0
