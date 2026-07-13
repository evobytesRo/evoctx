from evoctx.db import MIGRATIONS, Database
from evoctx.synonyms import expand_query


# ── expand_query unit ──────────────────────────────────────────────────────────

def test_known_word_expands_to_or_group():
    assert expand_query("auth") == "(auth OR authentication OR login OR signin)"


def test_unknown_word_passes_through():
    assert expand_query("banana") == "banana"


def test_multiple_terms_explicit_and_joined():
    result = expand_query("auth bug")
    assert result == (
        "(auth OR authentication OR login OR signin) AND "
        "(bug OR error OR issue OR failure OR problem)"
    )


def test_quoted_phrase_bails_out_untouched():
    assert expand_query('"exact phrase"') == '"exact phrase"'


def test_fts_operators_bail_out_untouched():
    assert expand_query("auth NOT bug") == "auth NOT bug"


def test_prefix_syntax_bails_out_untouched():
    assert expand_query("aut*") == "aut*"


def test_mixed_case_matches_lowercase_group():
    assert expand_query("AUTH") == "(auth OR authentication OR login OR signin)"


def test_empty_query():
    assert expand_query("") == ""


def test_all_unknown_words_still_and_joined():
    assert expand_query("banana kumquat") == "banana AND kumquat"


# ── the actual red-team example, end to end through db.search() ──────────────

def test_auth_bug_finds_login_failure_note(db):
    db.add_note("Login failure root cause", "users could not sign in due to expired tokens")
    hits = db.search("auth bug")
    assert len(hits) == 1
    assert hits[0]["title"] == "Login failure root cause"


def test_synonym_expansion_does_not_break_project_scoping(db):
    db.add_note("Login failure", "x", project="p1")
    db.add_note("Auth issue elsewhere", "x", project="p2")
    hits = db.search("auth", project="p1")
    assert len(hits) == 1
    assert hits[0]["title"] == "Login failure"


def test_advanced_syntax_still_works_unexpanded(db):
    db.add_note("Exact phrase note", "an exact phrase here")
    hits = db.search('"exact phrase"')
    assert len(hits) == 1


# ── porter stemming ────────────────────────────────────────────────────────────

def test_porter_stemming_matches_word_forms(db):
    db.add_note("Configuring the server", "steps for configuring the deploy pipeline")
    hits = db.search("configuration")  # different word FORM, not a synonym
    assert len(hits) == 1


def test_migration_004_rebuilds_fts_with_porter(tmp_path):
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "store.db"))
    conn.executescript("""
        CREATE TABLE notes (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
            project TEXT, tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE notes_fts USING fts5(
            title, content, content=notes, content_rowid=rowid
        );
        CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
            INSERT INTO notes_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
        END;
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, project TEXT,
            summary TEXT NOT NULL, created_at TEXT NOT NULL
        );
        INSERT INTO notes VALUES ('n1', 'Configuring things', 'about configuring', NULL, '[]', 't', 't');
    """)
    conn.commit()
    conn.close()

    db = Database(tmp_path)  # runs 001-004, including the FTS rebuild
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    # pre-existing note is still searchable after the FTS table was dropped+rebuilt
    hits = db.search("configuration")
    assert len(hits) == 1
    assert hits[0]["id"] == "n1"
