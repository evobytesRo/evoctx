import sqlite3

import pytest

from conftest import found

from evoctx.db import MIGRATIONS, AmbiguousPrefix, Database, SchemaTooNew


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
    assert found(db.find_note_by_prefix(note_id[:8]))["id"] == note_id
    assert db.find_note_by_prefix("zzzzzzzz") is None


def test_ambiguous_prefix_refuses_instead_of_picking_one(db):
    """An ambiguous prefix must raise, not resolve arbitrarily.

    Adding an ORDER BY would only make the arbitrary pick deterministic. The
    caller asked for one note and there is no basis for choosing among
    several, so refuse — `evoctx delete <prefix> --force` skips the
    confirmation prompt, and a delete is not undoable.
    """
    for i in range(3):
        db.add_note(f"note {i}", "x")
    db.conn.execute("UPDATE notes SET id = 'dupe-' || id")
    db.conn.commit()

    with pytest.raises(AmbiguousPrefix) as exc:
        db.find_note_by_prefix("dupe-")
    assert len(exc.value.ids) == 3
    assert exc.value.total == 3
    assert "matches 3 records" in str(exc.value)

    # an unambiguous prefix still resolves
    one = db.conn.execute("SELECT id FROM notes LIMIT 1").fetchone()[0]
    assert found(db.find_note_by_prefix(one))["id"] == one


def test_ambiguous_prefix_reports_the_true_count_not_the_shown_five(db):
    """Only 5 candidates are listed; the count must still be the real total,
    or the message states an undercount as fact."""
    for i in range(9):
        db.add_note(f"note {i}", "x")
    db.conn.execute("UPDATE notes SET id = 'dupe-' || id")
    db.conn.commit()

    with pytest.raises(AmbiguousPrefix) as exc:
        db.find_note_by_prefix("dupe-")
    assert len(exc.value.ids) == 5      # display is capped
    assert exc.value.total == 9         # the count is not
    assert "matches 9 records (showing 5)" in str(exc.value)


def test_prefix_lookup_escapes_like_metacharacters(db):
    """A typo'd '_' is a LIKE wildcard. Unescaped, 'a_c' would match 'abc...'
    too — and a widened match that happens to hit exactly one note resolves
    silently instead of reporting ambiguity."""
    db.add_note("wildcard bait", "x")
    db.conn.execute("UPDATE notes SET id = 'abc-1234'")
    db.conn.commit()

    assert found(db.find_note_by_prefix("abc"))["id"] == "abc-1234"
    assert db.find_note_by_prefix("a_c") is None    # '_' is literal, not a wildcard
    assert db.find_note_by_prefix("a%") is None     # so is '%'


def test_find_session_by_prefix(db):
    """end-session --id takes a prefix too, and now resolves it here.

    Covers resolution and refusal. It does NOT cover the 50-session window
    that cli.py used to apply — that would need 51 sessions and a CLI-level
    test; the window is gone structurally because the code that imposed it
    was deleted, not because this test watches for it.
    """
    first = db.start_session(project="a")
    second = db.start_session(project="b")

    assert found(db.find_session_by_prefix(first[:8]))["id"] == first
    assert db.find_session_by_prefix("zzzzzzzz") is None

    db.conn.execute("UPDATE sessions SET id = 'dupe-' || id")
    db.conn.commit()
    with pytest.raises(AmbiguousPrefix):
        db.find_session_by_prefix("dupe-")
    assert found(db.find_session_by_prefix("dupe-" + second))["id"] == "dupe-" + second


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
    assert found(db.get_note("n1"))["title"] == "old note"
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
