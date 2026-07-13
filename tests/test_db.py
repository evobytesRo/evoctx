import sqlite3

import pytest

from evoctx.db import MIGRATIONS, Database, SchemaTooNew


def test_add_and_get_note(db):
    note_id = db.add_note("Title", "Body text", project="proj", tags=["#a", "#b"])
    note = db.get_note(note_id)
    assert note["title"] == "Title"
    assert note["content"] == "Body text"
    assert note["project"] == "proj"
    assert note["tags"] == ["#a", "#b"]


def test_get_note_missing_returns_none(db):
    assert db.get_note("nope") is None


def test_find_note_by_prefix(db):
    note_id = db.add_note("Prefixed", "x")
    assert db.find_note_by_prefix(note_id[:8])["id"] == note_id
    assert db.find_note_by_prefix("zzzzzzzz") is None


def test_fts_search_finds_content(db):
    db.add_note("Webhook retry logic", "queue depth check was wrong")
    results = db.search("webhook")
    assert len(results) == 1
    assert results[0]["title"] == "Webhook retry logic"


def test_search_project_scoping(db):
    db.add_note("Note A", "shared keyword", project="alpha")
    db.add_note("Note B", "shared keyword", project="beta")
    assert len(db.search("shared")) == 2
    scoped = db.search("shared", project="alpha")
    assert len(scoped) == 1
    assert scoped[0]["project"] == "alpha"


def test_search_bad_fts_syntax_falls_back_to_like(db):
    db.add_note("Dash-note", 'content with "quotes" and -dashes')
    # raw quotes/dashes are invalid FTS5 MATCH syntax; LIKE fallback must catch it
    results = db.search('"unbalanced')
    assert isinstance(results, list)


def test_project_upsert_and_get(db):
    db.upsert_project("p1", overview="first")
    db.upsert_project("p1", overview="second", conventions="conv")
    proj = db.get_project("p1")
    assert proj["overview"] == "second"
    assert proj["conventions"] == "conv"
    assert db.list_projects() == ["p1"]


def test_get_project_includes_recent_notes(db):
    db.upsert_project("p1")
    db.add_note("n1", "x", project="p1")
    proj = db.get_project("p1")
    assert [n["title"] for n in proj["recent_notes"]] == ["n1"]


def test_fresh_store_skips_migrations(tmp_path):
    db = Database(tmp_path)
    version = db.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(MIGRATIONS)


def test_migration_001_upgrades_old_store(tmp_path):
    # build a v0 store: old sessions shape, notes without session_id
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
        INSERT INTO notes VALUES ('n1', 'old note', 'body', NULL, '[]', 't', 't');
    """)
    conn.commit()
    conn.close()

    db = Database(tmp_path)  # runs migration 001

    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(sessions)").fetchall()]
    assert "started_at" in cols and "ended_at" in cols and "intent" in cols
    note_cols = [r[1] for r in db.conn.execute("PRAGMA table_info(notes)").fetchall()]
    assert "session_id" in note_cols
    # pre-existing data survives
    assert db.get_note("n1")["title"] == "old note"
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_migrations_do_not_rerun(tmp_path):
    Database(tmp_path)
    db2 = Database(tmp_path)  # reopen — must not raise or re-run
    assert db2.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_newer_store_refused(tmp_path):
    db = Database(tmp_path)
    db.conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 5}")
    db.conn.commit()
    db.conn.close()
    with pytest.raises(SchemaTooNew, match="newer release"):
        Database(tmp_path)
