import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .audit import AUDIT_SCHEMA, migration_002_audit_log
from .secrets import redact_secrets
from .synonyms import expand_query


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
]


class SchemaTooNew(RuntimeError):
    """The store was created by a newer release than this code."""


class NoteTooLarge(ValueError):
    """Content exceeds MAX_NOTE_BYTES. Raised before the INSERT — nothing
    partial gets written."""


MAX_NOTE_BYTES = 256 * 1024  # 256KB — generous for real notes, not for accidental blobs


class Database:
    def __init__(self, store_dir: Path):
        store_dir.mkdir(parents=True, exist_ok=True)
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
        self._init()
        if fresh:
            self.conn.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
            self.conn.commit()
        else:
            self._migrate()

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
                updated_at TEXT NOT NULL
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
                updated_at     TEXT NOT NULL
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
        self.conn.executescript(AUDIT_SCHEMA)
        self.conn.commit()

    def _migrate(self):
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
        fts_query = expand_query(query)
        try:
            if project:
                rows = self.conn.execute("""
                    SELECT n.id, n.title,
                           snippet(notes_fts, 1, '', '', '...', 32) AS snippet,
                           n.project, n.tags, n.created_at, n.author_client
                    FROM notes_fts
                    JOIN notes n ON n.rowid = notes_fts.rowid
                    WHERE notes_fts MATCH ? AND n.project = ?
                    ORDER BY rank LIMIT ? OFFSET ?
                """, (fts_query, project, limit, offset)).fetchall()
            else:
                rows = self.conn.execute("""
                    SELECT n.id, n.title,
                           snippet(notes_fts, 1, '', '', '...', 32) AS snippet,
                           n.project, n.tags, n.created_at, n.author_client
                    FROM notes_fts
                    JOIN notes n ON n.rowid = notes_fts.rowid
                    WHERE notes_fts MATCH ?
                    ORDER BY rank LIMIT ? OFFSET ?
                """, (fts_query, limit, offset)).fetchall()
        except sqlite3.OperationalError:
            like = f"%{query}%"
            if project:
                rows = self.conn.execute("""
                    SELECT id, title, substr(content, 1, 200), project, tags, created_at, author_client
                    FROM notes WHERE (title LIKE ? OR content LIKE ?) AND project = ?
                    LIMIT ? OFFSET ?
                """, (like, like, project, limit, offset)).fetchall()
            else:
                rows = self.conn.execute("""
                    SELECT id, title, substr(content, 1, 200), project, tags, created_at, author_client
                    FROM notes WHERE title LIKE ? OR content LIKE ?
                    LIMIT ? OFFSET ?
                """, (like, like, limit, offset)).fetchall()

        return [
            {
                "id": r[0], "title": r[1], "snippet": r[2],
                "project": r[3], "tags": json.loads(r[4] or "[]"),
                "created_at": r[5], "author_client": r[6],
            }
            for r in rows
        ]

    # ── reads ─────────────────────────────────────────────────────────────────

    def get_note(self, note_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, title, content, project, tags, created_at, updated_at, author_client FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "title": row[1], "content": row[2],
            "project": row[3], "tags": json.loads(row[4] or "[]"),
            "created_at": row[5], "updated_at": row[6], "author_client": row[7],
        }

    def find_note_by_prefix(self, prefix: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id FROM notes WHERE id LIKE ?", (f"{prefix}%",)
        ).fetchone()
        return self.get_note(row[0]) if row else None

    def get_project(self, name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT name, overview, conventions, open_questions, updated_at FROM projects WHERE name = ?",
            (name,),
        ).fetchone()
        if not row:
            return None

        recent = self.conn.execute(
            "SELECT id, title, tags, created_at, author_client FROM notes WHERE project = ? ORDER BY created_at DESC LIMIT 5",
            (name,),
        ).fetchall()

        return {
            "name": row[0], "overview": row[1],
            "conventions": row[2], "open_questions": row[3],
            "updated_at": row[4],
            "recent_notes": [
                {"id": r[0], "title": r[1], "tags": json.loads(r[2] or "[]"),
                 "created_at": r[3], "author_client": r[4]}
                for r in recent
            ],
        }

    def list_projects(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT name FROM projects ORDER BY name").fetchall()]

    def list_notes(self, project: str | None = None) -> list[dict]:
        q = "SELECT id, title, project, tags, created_at, author_client FROM notes"
        params: list = []
        if project:
            q += " WHERE project = ?"
            params.append(project)
        q += " ORDER BY created_at DESC"
        return [
            {
                "id": r[0], "title": r[1], "project": r[2],
                "tags": json.loads(r[3] or "[]"), "created_at": r[4],
                "author_client": r[5],
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

    def latest_open_session(self, project: str | None = None) -> dict | None:
        q = "SELECT id FROM sessions WHERE ended_at IS NULL"
        params: list = []
        if project:
            q += " AND project = ?"
            params.append(project)
        q += " ORDER BY started_at DESC LIMIT 1"
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
        q += " ORDER BY s.started_at DESC LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)
        return [self._session_dict(r) for r in self.conn.execute(q, params).fetchall()]

    def _session_dict(self, row) -> dict:
        note_rows = self.conn.execute(
            "SELECT id, title FROM notes WHERE session_id = ? ORDER BY created_at",
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
    ) -> str:
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
            "INSERT INTO notes (id, title, content, project, tags, session_id, author_client, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (note_id, title, content, project, json.dumps(tags or []), session_id, author_client, now, now),
        )
        self.conn.commit()
        return note_id

    def export_all(self) -> dict:
        """Full dump for `evoctx export` — every project, every note (full
        content), every session. The portability story made checkable."""
        projects = [self.get_project(name) for name in self.list_projects()]
        notes = [
            self.get_note(r[0])
            for r in self.conn.execute("SELECT id FROM notes ORDER BY created_at").fetchall()
        ]
        sessions = self.recent_sessions(limit=1_000_000)
        return {"projects": projects, "notes": notes, "sessions": sessions}

    def upsert_project(
        self,
        name: str,
        overview: str = "",
        conventions: str = "",
        open_questions: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO projects (name, overview, conventions, open_questions, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                overview       = excluded.overview,
                conventions    = excluded.conventions,
                open_questions = excluded.open_questions,
                updated_at     = excluded.updated_at
            """,
            (name, overview, conventions, open_questions, _now()),
        )
        self.conn.commit()

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
        deleted_notes = self.delete_notes_by_project(name) if with_notes else 0
        cur = self.conn.execute("DELETE FROM projects WHERE name = ?", (name,))
        self.conn.commit()
        return cur.rowcount > 0 or deleted_notes > 0
