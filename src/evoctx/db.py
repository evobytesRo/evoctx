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
from .synonyms import expand_query, expanded_tokens, plain_tokens


#: How many matching rows one search will look at. Lives here, beside the
#: query it bounds, because both layers that need it read the store: the
#: policy engine bounds its scoped scan by it, and the CLI pages by it. It
#: was briefly a private attribute of the policy engine, which meant the CLI
#: reaching up a layer for a constant about the database.
SEARCH_SCAN_LIMIT = 2000

#: How many matches make a rung of the widening ladder worth stopping at. Not
#: 1, and that is the whole point: measured against a real store, `ANAF
#: efactura submission status polling` ANDed to exactly one note — a meta-note
#: that happened to contain all five words — and that one hit was enough to
#: hide the four notes that actually answered the question. "It matched
#: something" is not "it answered the question", and a lone incidental hit is
#: the shape most likely to be the wrong one.
#:
#: Module level because two layers apply the same rule to different inputs:
#: `Database.choose_rung` to raw store counts, `PolicyEngine` to what a grant
#: lets the caller see.
ENOUGH_HITS = 5

#: A query FTS5 will always accept, used to ask whether the index still works.
#: Deliberately trivial: it has to fail for one reason only, the store being
#: unreadable.
_HEALTH_PROBE = "evoctxhealthprobe"


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


def _redact_description(overview: str, conventions: str, open_questions: str):
    """Mask secret shapes in a project's three free-text fields.

    `add_note` and the session writers have masked their text since 0.1.0, and
    SECURITY.md promises it happens "regardless of entry point". Project text
    was the entry point it did not happen at — which mattered little while
    `evoctx init` scraped it from a README, and matters now that
    `register_project` lets a model write three fields of free prose straight
    to disk. A model summarising a repo it has just read is exactly the caller
    most likely to quote a `.env` line into an overview.

    Applied here, at the row writers, so the CLI paths (`init`,
    `new-project`, `update-project`) are covered by the same change rather
    than by remembering to call it.
    """
    return (redact_secrets(overview or "")[0],
            redact_secrets(conventions or "")[0],
            redact_secrets(open_questions or "")[0])


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
    overview, conventions, open_questions = _redact_description(
        overview, conventions, open_questions)
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
    overview, conventions, _ = _redact_description(overview, conventions, "")
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


# One source for the search index and the triggers that feed it, so `_init()`
# (fresh stores) and migration 006 (existing stores) cannot drift. Migration
# 004 got away without this because it changed only the tokenizer and left the
# triggers' column lists alone; 006 changes the columns, so the two halves
# stop being independently correct.
#
# `tags` is a real column on `notes`, which is what lets `content=notes` read
# it during a rebuild without any backfill. It is indexed as the raw JSON
# (`["#session", "#evobytes-api"]`) and that is fine: unicode61 treats `[`,
# `"`, `#`, `,` and `-` as separators, so the terms that land in the index are
# `session`, `evobytes`, `api` — which is what someone searching for a tag
# types anyway, with or without the `#`.
NOTES_FTS_SCHEMA = """
    CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
        title, content, tags,
        content=notes, content_rowid=rowid,
        tokenize='porter unicode61'
    );

    CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
        INSERT INTO notes_fts(rowid, title, content, tags)
        VALUES (new.rowid, new.title, new.content, new.tags);
    END;
    CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
        INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
        VALUES ('delete', old.rowid, old.title, old.content, old.tags);
    END;
    CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
        INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
        VALUES ('delete', old.rowid, old.title, old.content, old.tags);
        INSERT INTO notes_fts(rowid, title, content, tags)
        VALUES (new.rowid, new.title, new.content, new.tags);
    END;
"""


def _migration_006_index_tags(conn: sqlite3.Connection) -> None:
    """Put `tags` in the search index. Until now it held title and content
    only, so a tag was write-only as far as search was concerned: on this
    author's store 51 notes carried `#gotcha` in tags and 39 in their text,
    which means 12 notes were tagged for a topic and unreachable by searching
    for it. Tagging is the one act of curation this store asks for, and it did
    nothing.

    The triggers are dropped, not left standing. An external-content FTS table
    is only consistent if the 'delete' command replays exactly the values that
    were indexed, so a trigger writing the old two-column tuple against a
    three-column table is not a cosmetic mismatch — it corrupts the index for
    every note edited or deleted afterwards.
    """
    conn.executescript("""
        DROP TRIGGER IF EXISTS notes_ai;
        DROP TRIGGER IF EXISTS notes_ad;
        DROP TRIGGER IF EXISTS notes_au;
        DROP TABLE IF EXISTS notes_fts;
    """ + NOTES_FTS_SCHEMA + """
        INSERT INTO notes_fts(notes_fts) VALUES ('rebuild');
    """)


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
    _migration_006_index_tags,
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
        """ + NOTES_FTS_SCHEMA + """
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

    # Relative weights for bm25(title, content, tags). A note whose TITLE names
    # the thing is the note you wanted; tags are curated by hand and so beat
    # incidental prose, but they are labels rather than statements and lose to
    # a title. Weights only reorder matches — nothing is included or excluded
    # by them — so the risk of getting the numbers slightly wrong is bounded.
    _BM25_WEIGHTS = (10.0, 1.0, 4.0)

    @staticmethod
    def widening_ladder(query: str) -> list[str]:
        """Progressively wider MATCH queries: try each until one hits.

        FTS5's operator between two bare words is **AND**, which is the single
        biggest reason a search comes back empty. A natural four-word question
        demands all four terms in one note, so it finds nothing and the caller
        concludes the store is empty rather than that the query was narrow.

        Rung 1 keeps AND — a note carrying every term is genuinely the best
        answer and must not be diluted while it exists. Rung 2 drops to OR,
        which cannot bury that note either: bm25 scores a document matching
        every term above one matching a single term, so widening changes what
        is *reachable*, not what ranks first. Rung 3 makes each term a prefix,
        for the half-remembered word ("newslet" -> "newsletter"). FTS5 has no
        infix operator, so `*term` remains impossible and the LIKE fallback is
        not a substitute — it is an error path, not a search mode.

        A query with deliberate syntax in it gets no ladder at all: someone
        who wrote quotes, parens or an operator asked a precise question, and
        widening it silently would answer a different one.
        """
        tokens = plain_tokens(query)
        if tokens is None:
            return [query]
        # Rung 1 goes through expand_query rather than joining the tokens here,
        # so the AND rung stays the one shipped, tested query builder and the
        # ladder cannot quietly disagree with it.
        rungs = [expand_query(query)]
        if len(tokens) > 1:
            rungs.append(" OR ".join(expanded_tokens(tokens)))
        # No synonym expansion on the prefix rung. `(token OR synonym)*` is not
        # what it looks like — the `*` binds to the closing paren, not to every
        # alternative — and expanding first would need parens per alternative
        # for a rung that is already the loosest thing this store will do.
        prefixed = " OR ".join(f"{t}*" for t in tokens)
        if prefixed not in rungs:
            rungs.append(prefixed)
        return rungs

    #: Alias of the module constant, read by `choose_rung`. The policy engine
    #: imports `ENOUGH_HITS` rather than reaching in here, the same way it and
    #: the CLI import `SEARCH_SCAN_LIMIT`.
    _ENOUGH_HITS = ENOUGH_HITS

    @staticmethod
    def _rejected_the_query(exc: sqlite3.OperationalError) -> bool:
        """Did SQLite refuse the QUERY, as opposed to failing to read?

        `sqlite_errorcode` is the discriminator the stdlib already exposes, and
        it does in one comparison what two rounds of error-string matching
        could not: every FTS5 rejection is `SQLITE_ERROR` (1) — "fts5: syntax
        error", "unterminated string", "unknown special query", "no such
        column", "expected integer", "parser stack overflow" alike — while
        `SQLITE_BUSY` is 5 and `SQLITE_IOERR` is 10. Neither can then reach the
        LIKE path however the store happens to behave a moment later.

        `getattr` with a default because the attribute is 3.11+, and this
        package supports 3.10. There it degrades to the retry-and-probe below,
        which is what shipped before this check existed — never worse.
        """
        return getattr(exc, "sqlite_errorcode", 1) == 1

    def _index_is_readable(self) -> bool:
        """Can the FTS index answer a query it is guaranteed to accept?

        Asked only after the real statement has failed TWICE, to separate "the
        store is unwell" from "FTS5 rejected that query". Only the second may
        fall through to the LIKE substring path.

        Deliberately not a list of error strings. Two review rounds went into
        discovering that SQLite words a rejected query at least six ways —
        "fts5: syntax error", "unterminated string", "unknown special query",
        "no such column", "expected integer, got …", "fts5: parser stack
        overflow" — and the set is open-ended, so any list is one that will be
        incomplete again. Whether the store answers is a question with one
        answer, and asking it directly cannot go stale.

        It is not sufficient on its own, which is why the retry above exists:
        a probe is a LATER observation, and a fault that has cleared by the
        time it runs reads as a healthy store and blames the query.
        """
        try:
            self.conn.execute(
                "SELECT 1 FROM notes_fts WHERE notes_fts MATCH ? LIMIT 1",
                (_HEALTH_PROBE,),
            ).fetchall()
            return True
        except sqlite3.Error:
            return False

    def _rung_count(self, rung: str, project: str | None) -> int:
        """How many notes one rung matches, under the same project predicate
        the real query uses. Split out so the retry runs the identical
        statement rather than something that stands in for it."""
        if project:
            return self.conn.execute("""
                SELECT COUNT(*) FROM notes_fts
                JOIN notes n ON n.rowid = notes_fts.rowid
                WHERE notes_fts MATCH ? AND (n.project = ? OR n.is_global = 1)
            """, (rung, project)).fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM notes_fts WHERE notes_fts MATCH ?",
            (rung,),
        ).fetchone()[0]

    def _match_page(self, fts_query: str, project: str | None,
                    limit: int, offset: int) -> list:
        """One ranked page from the index. Split out so the retry above runs
        the identical statement rather than something that stands in for it."""
        wt, wc, wtag = self._BM25_WEIGHTS
        if project:
            return self.conn.execute(f"""
                SELECT n.id, n.title,
                       snippet(notes_fts, 1, '', '', '...', 32) AS snippet,
                       n.project, n.tags, n.created_at, n.author_client, n.is_global
                FROM notes_fts
                JOIN notes n ON n.rowid = notes_fts.rowid
                WHERE notes_fts MATCH ? AND (n.project = ? OR n.is_global = 1)
                ORDER BY bm25(notes_fts, {wt}, {wc}, {wtag}), n.rowid DESC
                LIMIT ? OFFSET ?
            """, (fts_query, project, limit, offset)).fetchall()
        return self.conn.execute(f"""
            SELECT n.id, n.title,
                   snippet(notes_fts, 1, '', '', '...', 32) AS snippet,
                   n.project, n.tags, n.created_at, n.author_client, n.is_global
            FROM notes_fts
            JOIN notes n ON n.rowid = notes_fts.rowid
            WHERE notes_fts MATCH ?
            ORDER BY bm25(notes_fts, {wt}, {wc}, {wtag}), n.rowid DESC
            LIMIT ? OFFSET ?
        """, (fts_query, limit, offset)).fetchall()

    def _like_page(self, query: str, project: str | None,
                   limit: int, offset: int) -> list:
        """The error path: a literal substring scan, for a query FTS5 will not
        parse at all. `tags` is included to match what the index now covers.

        Reached only from `search`'s double-failure branch. It is not a search
        mode, and a caller wanting substring matching should not go looking for
        a query that gets it here — that is a query FTS5 refuses, and what it
        does next is not a contract."""
        like = f"%{query}%"
        if project:
            return self.conn.execute("""
                SELECT id, title, substr(content, 1, 200), project, tags, created_at, author_client, is_global
                FROM notes WHERE (title LIKE ? OR content LIKE ? OR tags LIKE ?)
                  AND (project = ? OR is_global = 1)
                ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
            """, (like, like, like, project, limit, offset)).fetchall()
        return self.conn.execute("""
            SELECT id, title, substr(content, 1, 200), project, tags, created_at, author_client, is_global
            FROM notes WHERE title LIKE ? OR content LIKE ? OR tags LIKE ?
            ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
        """, (like, like, like, limit, offset)).fetchall()

    def choose_rung(self, query: str, project: str | None = None) -> str:
        """The narrowest rung that returns enough, else the fullest one.

        **Choose a rung ONCE per result set and pass it to every page of it.**
        Choosing again per page is unsound: another window writing a note
        between two pages can change the counts, so the second page comes from
        a different rung and therefore a different result set — a note that
        was on page 1 vanishes and `total` moves under the caller. A store
        explicitly built for several assistant windows cannot treat concurrent
        writes as a corner case.

        This is the store-level chooser, used by `db.search` when no rung is
        supplied — the CLI's path, and every direct call in the tests. The
        policy engine does NOT use it: it has to count what the grant lets the
        caller see, so it walks the same ladder itself in
        `PolicyEngine._widen_until_enough` and hands the chosen rung back to
        the caller as a page token. The rule is the same in both; the inputs
        are raw store counts here and grant-filtered ones there.

        The count carries the same project predicate as the real query,
        because a rung that matches five notes in projects the caller did not
        ask about has, for this caller, matched nothing.

        Widening does not cost precision, because it does not reorder anything
        by itself: rung 2 is rung 1's terms joined with OR, so its result set
        is a superset, and bm25 accumulates a contribution per matched term —
        the note carrying every term still comes first. What widening changes
        is what is *reachable*. The honest limit: bm25 is a score, not a
        guarantee, so a very short note matching one term with a title hit can
        in principle outrank a long note matching all of them. That is a
        ranking nit; the failure this release exists to fix is recall.

        Grant-level filtering (excluded tags, project scope) sits above this
        layer and is not counted here, so a rung whose every hit is later
        redacted away still satisfies the threshold. Accepted: pushing the
        grant down into the store would invert the layering the whole policy
        engine is built on, and the case needs a grant that hides exactly the
        notes one rung found and nothing the next would.
        """
        rungs = self.widening_ladder(query)
        counts = []
        for rung in rungs:
            try:
                n = self._rung_count(rung, project)
            except sqlite3.OperationalError:
                # Same classification as `search`, same order and same reasons:
                # retry the identical statement first, because a rejected query
                # is deterministic and a transient fault is not; only then ask
                # whether the store answers at all.
                try:
                    n = self._rung_count(rung, project)
                except sqlite3.OperationalError as e:
                    if not self._rejected_the_query(e) or not self._index_is_readable():
                        raise
                    # FTS5 refused this rung. Hand back the narrowest one so
                    # `search()` meets the same refusal and takes its LIKE
                    # fallback on its own terms; deciding that here would make
                    # the fallback depend on which of two callers ran. If the
                    # rung that failed was NOT the narrowest, `search()` simply
                    # runs rung 1 successfully and no fallback happens, which
                    # is right — one unparseable rung is not a reason to
                    # abandon a rung that works.
                    return rungs[0]
            if n >= self._ENOUGH_HITS:
                # Narrowest rung that fills a page wins immediately — no
                # reason to count the wider ones, and no reason to run them.
                return rung
            counts.append(n)
        # Nothing reached the threshold. Take the rung that found the most,
        # ties going to the narrower one, since `max` keeps the first maximum
        # and the rungs are already ordered narrow to wide. Widest-with-any-
        # hits would be wrong: rung 3 drops synonym expansion, so it is not a
        # superset of rung 2 and regularly finds fewer.
        best = max(range(len(rungs)), key=lambda i: counts[i])
        return rungs[best]

    def search(self, query: str, project: str | None = None, limit: int = 10,
               offset: int = 0, rung: str | None = None) -> list[dict]:
        # Synonym expansion and the widening ladder apply to the FTS5 MATCH
        # path only — an OR-group like "(auth OR login)" is valid MATCH syntax
        # but would search for that literal parenthesized text under the LIKE
        # fallback, so the fallback below deliberately re-derives `like`
        # from the original, unexpanded `query`.
        # Every ordering below is total, and deliberately so. bm25 ties are not
        # hypothetical — same-shape notes score identically — and an order that
        # is not total makes the same query return its results in a different
        # sequence from one call to the next. `PolicyEngine.search` slices a
        # page out of one ordered pass, so a caller asking for offset 10 after
        # offset 0 is making a SECOND call: without a total order those two
        # calls can disagree about which note was tenth, and a note is repeated
        # or skipped between them.
        #
        # Don't "optimise" the tiebreaker back out when you see the plan: the
        # rowid term does cost FTS5's rank pushdown. EXPLAIN QUERY PLAN goes
        # from `INDEX 32:M2` (internal rank sort, early exit at LIMIT) to
        # `INDEX 0:M2` + `USE TEMP B-TREE FOR ORDER BY` (sort all matches).
        # That is a real cost and an accepted one — these are personal stores,
        # and a correct page beats a fast one that can drop a note.
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
        # OUTSIDE the try, deliberately. `choose_rung` re-raises a store
        # failure, and inside the try that re-raise was caught two lines later
        # and turned into the LIKE fallback — a silent substring search over a
        # locked or broken store, returning plausible rows while three shipped
        # documents promise there is no substring search in any mode.
        #
        # `rung` is supplied by a caller that already chose one for this query;
        # `PolicyEngine.search` does that when the caller is paging, so every
        # page of one result set reads the same rung.
        fts_query = rung if rung is not None else self.choose_rung(query, project)
        try:
            rows = self._match_page(fts_query, project, limit, offset)
        except sqlite3.OperationalError:
            # Try the SAME statement once more before concluding anything. A
            # rejected query is deterministic and fails identically; a
            # transient fault is not. `SQLITE_BUSY` under WAL surfaces only
            # after `busy_timeout` has already elapsed, and `SQLITE_IOERR` on
            # Windows is routinely an antivirus or a syncing folder — both are
            # typically gone a moment later, and judging them on a LATER
            # observation classified them as bad queries and answered with a
            # substring search. Retrying also means a cleared hiccup returns
            # the real results rather than an error.
            try:
                rows = self._match_page(fts_query, project, limit, offset)
            except sqlite3.OperationalError as e:
                # Failed twice. Two independent conditions have to hold before
                # an ordinary search is allowed to become a substring search:
                # SQLite must have called it a query error, and the index must
                # still read. Either alone has been wrong — the error code is
                # unavailable on 3.10, and the probe is a later observation
                # that a cleared fault fools.
                if not self._rejected_the_query(e) or not self._index_is_readable():
                    raise
                rows = self._like_page(query, project, limit, offset)

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

    def open_sessions(self, exclude_id: str | None = None, limit: int = 5) -> list[dict]:
        """Sessions nobody closed, newest first — what `end_session` warns about.

        Deliberately machine-wide rather than scoped to a project: the point of
        the warning is that a session you are NOT working in is still open, and
        the one most worth hearing about is the one in the repo next door.
        Grant scope still applies, but it is applied above this layer, where
        every other session read is filtered.

        `exclude_id` is the session just closed. Excluded by id and not by
        "ended_at IS NULL", because the caller closes and then asks, and a
        transaction that has already committed would otherwise have to be
        reasoned about to know why the row is absent.
        """
        rows = self.conn.execute(
            "SELECT id, project, client, intent, started_at FROM sessions "
            "WHERE ended_at IS NULL AND id IS NOT ? "
            # Same tiebreaker, same reason as latest_open_session: coarse
            # clock resolution makes started_at ties ordinary, not rare.
            "ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (exclude_id, limit),
        ).fetchall()
        return [
            {"id": r[0], "project": r[1], "client": r[2],
             "intent": r[3], "started_at": r[4]}
            for r in rows
        ]

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
        overview, conventions, open_questions = _redact_description(
            overview, conventions, open_questions)
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
                         overwrite_description: bool = False,
                         reviewed: bool = False) -> str:
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
            wrote_description = not exists or overwrite_description
            if not exists:
                _insert_project_row(self.conn, name, overview, conventions,
                                    open_questions, author)
            elif overwrite_description:
                _update_description(self.conn, name, overview, conventions, author)
            if reviewed and wrote_description:
                # Only for text THIS call wrote. Linking a directory to an
                # existing project writes no description, so stamping a review
                # there would mark someone else's words as approved — the
                # laundering the column exists to prevent, arrived at from the
                # other side. Set after the write, inside the same
                # transaction, because both writers clear it.
                self.conn.execute(
                    "UPDATE projects SET description_reviewed_at = ? WHERE name = ?",
                    (_now(), name),
                )
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

    def merge_project(self, wrong: str, right: str) -> dict:
        """Reattach everything filed under `wrong` to `right`, then drop it.

        The remediation half of this release. Refusing invented names stops new
        phantoms; it does nothing for the notes and sessions already filed
        under one, which are invisible under the real project and reachable
        only by searching for the invented name — if you happen to know it.

        **Notes and sessions move together, and there is no flag to split
        them.** A session records what a stretch of work was for and its notes
        record what came out of it; moving one without the other leaves a
        session whose notes are elsewhere, which is a worse state than the one
        being fixed and cannot be undone by running this again. A flag for it
        would be a tool for stranding sessions.

        `wrong` need not exist as a project row — that is the normal case. A
        phantom is a *name on notes* that was never registered, so requiring it
        to exist would refuse exactly the situation this exists for. `right`
        must exist, because merging into a second invented name just moves the
        problem.

        Roots are not moved. A phantom has none (nothing can bind a root to a
        project that does not exist), and if `wrong` is a real project being
        folded into another, its directories are a claim about where code
        lives that the user should re-make deliberately with `init`, not have
        silently transferred.
        """
        if wrong == right:
            raise ValueError("a project cannot be merged into itself")
        if not self.project_exists(right):
            raise NoSuchProject(
                f"no project named '{right}' — merge into a project that "
                f"exists, or create it first"
            )
        with self.conn:
            notes = self.conn.execute(
                "UPDATE notes SET project = ? WHERE project = ?", (right, wrong)
            ).rowcount
            sessions = self.conn.execute(
                "UPDATE sessions SET project = ? WHERE project = ?", (right, wrong)
            ).rowcount
            # Only if it was a real project. A phantom has no row to delete and
            # no roots; `delete_project` here would be a no-op with a
            # misleading name.
            self.conn.execute("DELETE FROM project_roots WHERE project = ?", (wrong,))
            removed = self.conn.execute(
                "DELETE FROM projects WHERE name = ?", (wrong,)
            ).rowcount > 0
        return {"notes": notes, "sessions": sessions, "project_removed": removed}

    def count_for_project(self, name: str) -> dict:
        """How many notes and sessions carry this exact project name.

        A count, not a fetch. `list_notes(project=…)` loads and json-decodes
        every note plus every global one so a caller can filter them back out,
        and `recent_sessions` fires one query per session to attach its notes —
        N+1 to produce a number. This runs before an irreversible action, which
        is the one place a wrong or capped count matters most.
        """
        notes = self.conn.execute(
            "SELECT COUNT(*) FROM notes WHERE project = ?", (name,)).fetchone()[0]
        sessions = self.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE project = ?", (name,)).fetchone()[0]
        return {"notes": notes, "sessions": sessions}

    def phantom_projects(self) -> dict[str, dict]:
        """Names that notes or sessions are filed under, with no project row.

        The damage already done, countable. Deliberately covers sessions as
        well as notes: `start_session` accepted an invented name for just as
        long as `write_note` did, and a session missed by the count is one the
        merge would silently leave behind.

        `by_client` counts the ones an **assistant** filed — the residue of the
        bug step 4 fixed. The rest were written by a human through the CLI
        under a name they chose, which this release explicitly keeps working:
        grants and the refusal govern the MCP path, not what you may do with
        your own store. Both are reported; only `by_client` is a fault, because
        only it is something the user did not choose. A NULL `author_client`
        predates the column, so it is counted as not-assistant — an unknown
        origin is not evidence of one — and `sessions.client` defaults to
        `'unknown'`, which gets the same treatment for the same reason.
        """
        rows = self.conn.execute("""
            SELECT name, SUM(notes), SUM(sessions), SUM(by_client) FROM (
                SELECT project AS name, COUNT(*) AS notes, 0 AS sessions,
                       SUM(CASE WHEN author_client IS NOT NULL
                                 AND author_client != 'cli'
                                THEN 1 ELSE 0 END) AS by_client
                  FROM notes
                 WHERE project IS NOT NULL
                   AND project NOT IN (
                       SELECT name FROM projects WHERE name IS NOT NULL)
                 GROUP BY project
                UNION ALL
                SELECT project AS name, 0 AS notes, COUNT(*) AS sessions,
                       SUM(CASE WHEN client NOT IN ('cli', 'unknown')
                                THEN 1 ELSE 0 END) AS by_client
                  FROM sessions
                 WHERE project IS NOT NULL
                   AND project NOT IN (
                       SELECT name FROM projects WHERE name IS NOT NULL)
                 GROUP BY project
            ) GROUP BY name ORDER BY name
        """).fetchall()
        return {r[0]: {"notes": r[1], "sessions": r[2], "by_client": r[3]}
                for r in rows}

    def unscoped_notes(self) -> int:
        """Notes with no project and no global flag.

        Since 0.1.4 these are readable only by a grant covering every project
        (`_project_readable`). Migration 005 adds `is_global` with DEFAULT 0
        and does not backfill, so every note written before it is in this set,
        and there is no command that marks an existing note global. Without a
        count, nothing in the product would ever tell a user which of their
        notes stopped reaching a scoped client — which is the same shape as
        the phantom problem this release exists to fix.
        """
        return self.conn.execute(
            "SELECT COUNT(*) FROM notes WHERE project IS NULL AND is_global = 0"
        ).fetchone()[0]

    def projects_without_roots(self, machine: str | None = None) -> list[str]:
        """Projects with no directory bound on this machine.

        Not a fault on its own — a topic-only project never has one, and every
        project predates roots on upgrade day. It is reported so a user can see
        which projects resolution will never match, not so they can be nagged.
        """
        machine = machine or this_machine()
        # Templates are `projects` rows prefixed with `_` by design and can
        # never hold a directory, so telling the user to run `init` in one is
        # advice that cannot be followed.
        return [r[0] for r in self.conn.execute(
            r"SELECT name FROM projects WHERE name NOT LIKE '\_%' ESCAPE '\' "
            "AND name NOT IN "
            "(SELECT project FROM project_roots WHERE machine = ?) ORDER BY name",
            (machine,),
        ).fetchall()]

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
            else:
                # Actually unlink them. The CLI has always promised "kept,
                # unlinked" and the notes kept pointing at the deleted name
                # instead — which is a phantom by the definition this release
                # uses, so `doctor` would report a state the product created
                # and blessed, permanently, with remediation advice that does
                # not apply.
                self.conn.execute(
                    "UPDATE notes SET project = NULL WHERE project = ?", (name,))
            # Sessions are unconditional: `--with-notes` deletes notes, never
            # sessions, so branching this left every session an assistant ever
            # opened under the name pointing at a project that no longer
            # exists — the same phantom, reached through the other branch.
            self.conn.execute(
                "UPDATE sessions SET project = NULL WHERE project = ?", (name,))
            self.conn.execute("DELETE FROM project_roots WHERE project = ?", (name,))
            cur = self.conn.execute("DELETE FROM projects WHERE name = ?", (name,))
        return cur.rowcount > 0 or deleted_notes > 0
