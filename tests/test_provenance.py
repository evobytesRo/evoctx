"""author_client provenance — which client wrote a note, surfaced in every
read path. Doesn't stop injection (nothing does), but makes the trust
boundary visible: a note written by an AI client is labeled as such,
not indistinguishable from a human-authored one."""
import sqlite3

from conftest import found

from evoctx.db import MIGRATIONS, Database


def test_add_note_stores_author_client(db):
    note_id = db.add_note("t", "x", author_client="claude-code")
    assert db.get_note(note_id)["author_client"] == "claude-code"


def test_add_note_defaults_to_none(db):
    note_id = db.add_note("t", "x")
    assert db.get_note(note_id)["author_client"] is None


def test_search_surfaces_author_client(db):
    db.add_note("findable", "unique-marker-abc", author_client="cursor")
    hits = db.search("unique-marker-abc")
    assert hits[0]["author_client"] == "cursor"


def test_list_notes_surfaces_author_client(db):
    db.add_note("t", "x", author_client="cli")
    assert db.list_notes()[0]["author_client"] == "cli"


def test_get_project_recent_notes_surface_author_client(db):
    db.upsert_project("p1")
    db.add_note("t", "x", project="p1", author_client="windsurf")
    proj = db.get_project("p1")
    assert proj["recent_notes"][0]["author_client"] == "windsurf"


def test_migration_003_adds_column_to_old_store(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "store.db"))
    conn.executescript("""
        CREATE TABLE notes (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
            project TEXT, tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, project TEXT,
            summary TEXT NOT NULL, created_at TEXT NOT NULL
        );
        INSERT INTO notes VALUES ('n1', 'old', 'body', NULL, '[]', 't', 't');
    """)
    conn.commit()
    conn.close()

    db = Database(tmp_path)  # runs 001, 002, 003
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(notes)").fetchall()]
    assert "author_client" in cols
    # pre-existing notes get NULL, not a fabricated attribution
    assert found(db.get_note("n1"))["author_client"] is None
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_policy_write_note_tags_client(db, tmp_path):
    from evoctx.grants import GrantStore
    from evoctx.policy import PolicyEngine
    engine = PolicyEngine(db, GrantStore(tmp_path))
    note_id = engine.t_write_note("claude-code", "t", "x", None, None, None)
    assert db.get_note(note_id)["author_client"] == "claude-code"
