"""Project roots — binding a project to the directories its code lives in.

Until 0.1.4 the store had no idea where any project was, so an assistant asked
to name a project had nothing to match its workspace against and could only
infer a name. These tests cover the storage half of that fix: the roots
themselves, path normalisation, and `evoctx init` recording a root without
rewriting a description to do it.
"""
import os
from pathlib import Path

import pytest
from conftest import found, run

from evoctx.db import (
    MIGRATIONS,
    Database,
    DuplicateRootBinding,
    NoSuchProject,
    RootNotADirectory,
    RootOwnedByAnotherProject,
    normalize_root,
    this_machine,
)

# The backup a migration takes is named for the version it is migrating TO, so
# every one of these assertions is really about "the latest schema" and not
# about 005 in particular. Derived, because hardcoding it made three tests fail
# on the release that added 006 — a false alarm about untouched code, which is
# the kind of test that gets its assertion loosened rather than read.
LATEST = len(MIGRATIONS)
BACKUP_NAME = f"store.db.bak-pre-migration-{LATEST:03d}"


# ── normalisation ─────────────────────────────────────────────────────────────

def test_normalize_root_is_absolute_and_resolved(tmp_path):
    nested = tmp_path / "a" / ".." / "a"
    nested.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "a").mkdir(exist_ok=True)
    assert normalize_root(nested) == normalize_root(tmp_path / "a")
    assert Path(normalize_root(nested)).is_absolute()


def test_normalize_root_trailing_separator_is_the_same_root(tmp_path):
    assert normalize_root(str(tmp_path) + os.sep) == normalize_root(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="case-insensitivity is a Windows property")
def test_normalize_root_folds_case_for_a_path_not_on_disk(tmp_path):
    """This is the case that actually needs `normcase`.

    For a directory that exists, `Path.resolve()` on Windows already returns
    the filesystem's canonical casing, so a test on a real path passes even
    without `normcase` and proves nothing. Only an unresolvable tail — a root
    recorded for a directory since moved or deleted, which the store must
    still match — leaves the casing as the caller typed it.
    """
    missing_upper = tmp_path / "NoSuchDir"
    missing_lower = tmp_path / "nosuchdir"
    assert not missing_upper.exists()
    assert normalize_root(missing_upper) == normalize_root(missing_lower)


def test_normalize_root_expands_user(monkeypatch, tmp_path):
    """`resolve()` does not expand `~` — left alone it produces a literal `~`
    directory under the cwd, and the root silently points nowhere."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert normalize_root("~") == normalize_root(tmp_path)
    assert "~" not in normalize_root("~/sub")


@pytest.mark.skipif(os.name != "nt", reason="UNC is a Windows path shape")
def test_normalize_root_keeps_unc_paths_intact(tmp_path):
    """A UNC path has no drive letter and starts with two separators, which is
    the shape most likely to be mangled by normalisation. It need not exist —
    `resolve()` leaves an unreachable host alone."""
    unc = r"\\server\share\project"
    out = normalize_root(unc)
    assert out.startswith("\\\\")
    assert "server" in out and "share" in out and "project" in out
    assert normalize_root(unc + "\\") == out          # trailing separator
    assert normalize_root(unc.upper()) == out         # case-folded like any other


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_normalize_root_resolves_symlinks(tmp_path):
    """Two paths to one directory must be one root, or a project bound via a
    symlink never matches work done through the real path."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert normalize_root(link) == normalize_root(real)


@pytest.mark.skipif(os.name == "nt",
                    reason="normalize_root casefolds on Windows by design")
def test_normalize_root_keeps_case_on_posix(tmp_path):
    """Casefolding unconditionally would merge two genuinely different
    directories on a case-sensitive filesystem.

    Deliberately does NOT create the directories. It used to, and macOS is
    the counter-example that broke it: APFS defaults to case-insensitive, so
    `Foo` and `foo` cannot coexist and the second `mkdir` raised
    `FileExistsError` — a test asserting posix behaviour, failing on a posix
    machine, for a reason that has nothing to do with the function. What is
    under test is whether `normalize_root` casefolds, which on posix is a
    pure string operation.
    """
    assert normalize_root(tmp_path / "Foo") != normalize_root(tmp_path / "foo")


# ── storage ───────────────────────────────────────────────────────────────────

def test_add_and_list_roots(db, tmp_path):
    db.upsert_project("p1")
    stored = db.add_project_root("p1", tmp_path)
    assert stored == normalize_root(tmp_path)

    roots = db.project_roots("p1")
    assert [r["root_path"] for r in roots] == [normalize_root(tmp_path)]
    assert roots[0]["machine"] == this_machine()


def test_a_project_may_have_several_roots(db, tmp_path):
    """A worktree, a second machine, a monorepo subdirectory — a column would
    have forced last-write-wins and dropped the earlier path silently."""
    db.upsert_project("p1")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    db.add_project_root("p1", a)
    db.add_project_root("p1", b)
    assert len(db.project_roots("p1")) == 2


def test_adding_the_same_root_twice_is_idempotent(db, tmp_path):
    db.upsert_project("p1")
    db.add_project_root("p1", tmp_path)
    db.add_project_root("p1", tmp_path)
    assert len(db.project_roots("p1")) == 1


def test_project_for_root_matches_regardless_of_input_spelling(db, tmp_path):
    db.upsert_project("p1")
    db.add_project_root("p1", tmp_path)
    assert db.project_for_root(str(tmp_path) + os.sep) == "p1"
    assert db.project_for_root(tmp_path / "nope") is None


def test_remove_project_root(db, tmp_path):
    db.upsert_project("p1")
    db.add_project_root("p1", tmp_path)
    assert db.remove_project_root("p1", tmp_path) is True
    assert db.project_roots("p1") == []
    assert db.remove_project_root("p1", tmp_path) is False


def test_get_project_reports_its_roots_with_their_machine(db, tmp_path):
    """Roots come back as full rows, not bare paths: a synced store holds
    directories that exist on other machines, and a caller asking 'does this
    project live anywhere here' must be able to tell those apart."""
    db.upsert_project("p1")
    db.add_project_root("p1", tmp_path)
    roots = found(db.get_project("p1"))["roots"]
    assert [r["root_path"] for r in roots] == [normalize_root(tmp_path)]
    assert roots[0]["machine"] == this_machine()


def test_project_roots_can_be_narrowed_to_one_machine(db, tmp_path):
    db.upsert_project("p1")
    db.add_project_root("p1", tmp_path, machine="other-box")
    assert len(db.project_roots("p1")) == 1                      # every machine
    assert db.project_roots("p1", machine=this_machine()) == []  # not here


# ── integrity: a root must never outlive or precede its project ───────────────

def test_a_root_cannot_be_added_for_a_project_that_does_not_exist(db, tmp_path):
    """A root pointing at no project is the stranded-namespace bug again, and
    worse than the note version: resolution would answer a directory with a
    name `get_project()` denies."""
    with pytest.raises(NoSuchProject, match="no project named"):
        db.add_project_root("never-created", tmp_path)
    assert db.project_roots() == []


def test_a_root_that_is_not_a_directory_is_refused(db, tmp_path):
    """`cmd_init` checked this; the db layer, documented as the function every
    front-end goes through, did not — so the registration tool would have
    bound typos. A bound typo is not inert: resolution answers with it."""
    db.upsert_project("p1")
    with pytest.raises(RootNotADirectory, match="not a directory"):
        db.add_project_root("p1", tmp_path / "no-such-dir")
    assert db.project_roots() == []


def test_a_root_that_is_a_file_is_refused(db, tmp_path):
    """`exists()` would have passed this. A project's code lives in a
    directory, and prefix resolution against a file matches nothing."""
    db.upsert_project("p1")
    f = tmp_path / "pyproject.toml"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(RootNotADirectory):
        db.add_project_root("p1", f)
    assert db.project_roots() == []


def test_another_machines_root_is_taken_on_trust(db, tmp_path):
    """The guard must not fire for a row describing a filesystem this process
    cannot see. A restore replaying a synced store's rows would otherwise be
    refused wholesale, for the crime of not running on that machine."""
    db.upsert_project("p1")
    db.add_project_root("p1", tmp_path / "only-on-the-laptop", machine="other-box")

    assert [r["machine"] for r in db.project_roots("p1")] == ["other-box"]


def test_register_project_refuses_a_missing_directory_and_creates_nothing(db, tmp_path):
    """The refusal must land before the project row, not after — otherwise a
    typo'd path leaves a project created and unbound, which is exactly the
    half-written state `register_project` is one transaction to prevent."""
    with pytest.raises(RootNotADirectory):
        db.register_project("newproj", tmp_path / "typo", overview="ov")

    assert db.get_project("newproj") is None
    assert db.project_roots() == []


def test_deleting_a_project_takes_its_roots_with_it(db, tmp_path):
    """Left behind, the roots would resolve a directory to a project that no
    longer exists — and make the directory un-registerable, since init would
    refuse it as owned."""
    db.upsert_project("p1")
    db.add_project_root("p1", tmp_path)
    db.delete_project("p1")

    assert db.project_roots() == []
    assert db.project_for_root(tmp_path) is None


def test_two_projects_cannot_claim_one_directory(db, tmp_path):
    """One directory, one project — on this machine. Allowing both would put
    resolution back to picking arbitrarily among candidates."""
    db.upsert_project("first")
    db.upsert_project("second")
    db.add_project_root("first", tmp_path)

    with pytest.raises(RootOwnedByAnotherProject) as exc:
        db.add_project_root("second", tmp_path)
    assert exc.value.owner == "first"
    assert db.project_for_root(tmp_path) == "first"


def test_the_same_path_on_another_machine_is_a_different_root(db, tmp_path):
    """A synced store spans machines, where one absolute path can legitimately
    be a different project — so uniqueness is per machine, and resolution only
    ever considers this machine's rows."""
    db.upsert_project("here")
    db.upsert_project("there")
    db.add_project_root("here", tmp_path)
    db.add_project_root("there", tmp_path, machine="some-other-box")

    assert db.project_for_root(tmp_path) == "here"
    assert db.project_for_root(tmp_path, machine="some-other-box") == "there"


def test_a_note_cannot_be_both_global_and_project_scoped(db):
    from evoctx.db import ConflictingNoteScope
    with pytest.raises(ConflictingNoteScope, match="both global and scoped"):
        db.add_note("t", "x", project="p1", is_global=True)


# ── description provenance ────────────────────────────────────────────────────

def test_upsert_records_author_and_clears_review_when_text_changes(db):
    db.upsert_project("p1", overview="first", author="claude-code")
    db.conn.execute("UPDATE projects SET description_reviewed_at = 'T1' WHERE name = 'p1'")
    db.conn.commit()

    db.upsert_project("p1", overview="rewritten", author="claude-code")
    proj = found(db.get_project("p1"))
    assert proj["description_author"] == "claude-code"
    assert proj["description_reviewed_at"] is None, \
        "a review approves specific words; once they change it is about text that is gone"


def test_a_human_rewrite_also_clears_the_review(db):
    """Clearing only on non-cli writes would leave a human's rewrite carrying
    the *previous* text's review — worse than the state it guards against."""
    db.upsert_project("p1", overview="assistant text", author="claude-code")
    db.conn.execute("UPDATE projects SET description_reviewed_at = 'T1' WHERE name = 'p1'")
    db.conn.commit()

    db.upsert_project("p1", overview="human text")  # author defaults to cli
    proj = found(db.get_project("p1"))
    assert proj["description_author"] == "cli"
    assert proj["description_reviewed_at"] is None


def test_an_unchanged_rewrite_keeps_the_review(db):
    """upsert bumps updated_at on every write, so invalidating on that would
    keep destroying reviews nothing invalidated."""
    db.upsert_project("p1", overview="same", conventions="c", author="claude-code")
    db.conn.execute("UPDATE projects SET description_reviewed_at = 'T1' WHERE name = 'p1'")
    db.conn.commit()

    db.upsert_project("p1", overview="same", conventions="c", author="claude-code")
    assert found(db.get_project("p1"))["description_reviewed_at"] == "T1"


def test_a_conventions_edit_does_not_reattribute_the_overview(db):
    """`description_author` answers "who wrote the overview" — every surface
    says so. Editing your own conventions must not stamp `cli` ("a human
    typed it") onto an overview `init` scraped."""
    db.upsert_project("p", overview="Project at C:\\repo", conventions="",
                      author="init-scrape")
    db.upsert_project("p", overview="Project at C:\\repo",
                      conventions="human written conventions")  # author -> cli

    proj = found(db.get_project("p"))
    assert proj["description_author"] == "init-scrape", \
        "the overview is unchanged, so its author is unchanged"
    # but the review still goes: it approved the record, and the record moved
    assert proj["description_reviewed_at"] is None


def test_a_project_with_no_overview_has_no_author(db, tmp_path):
    """All three writers of `description_author` must agree. The same input
    through different entry points giving different answers is how a
    provenance field stops being trustworthy."""
    db.upsert_project("viaUpsert", conventions="just conventions", author="cli")
    assert found(db.get_project("viaUpsert"))["description_author"] is None

    db.register_project("viaRegister", tmp_path, overview="",
                        conventions="just conventions", author="init-scrape")
    assert found(db.get_project("viaRegister"))["description_author"] is None


def test_open_questions_only_edit_keeps_the_review(db):
    db.upsert_project("p1", overview="ov", conventions="c", author="claude-code")
    db.conn.execute("UPDATE projects SET description_reviewed_at = 'T1' WHERE name = 'p1'")
    db.conn.commit()

    db.upsert_project("p1", overview="ov", conventions="c", open_questions="new q",
                      author="claude-code")
    proj = found(db.get_project("p1"))
    assert proj["description_reviewed_at"] == "T1"
    assert proj["open_questions"] == "new q"


# ── global notes ──────────────────────────────────────────────────────────────

def test_is_global_distinguishes_deliberate_from_unset(db):
    """Both have project=None; without the flag they are indistinguishable,
    which is the same silence-conflated-with-intent defect as an invented
    project name."""
    deliberate = db.add_note("global note", "x", is_global=True)
    incidental = db.add_note("no project set", "x")

    assert found(db.get_note(deliberate))["is_global"] is True
    assert found(db.get_note(incidental))["is_global"] is False
    assert found(db.get_note(deliberate))["project"] is None, \
        "storage stays NULL — a sentinel name would change grant semantics"


def test_is_global_survives_the_unscoped_search_path(db):
    """The unscoped branches are the only ones that can return a NULL-project
    note at all, so they are the ones that must carry the flag."""
    db.add_note("findable global", "unique-marker-global", is_global=True)
    hits = db.search("unique-marker-global")
    assert hits[0]["is_global"] is True


def test_is_global_survives_the_like_fallback(db):
    """The fallback fires on a malformed FTS query and is a separate SELECT."""
    db.add_note("quoted", 'he said "marker here', is_global=True)
    hits = db.search('"marker')
    assert hits and hits[0]["is_global"] is True


# ── migration ─────────────────────────────────────────────────────────────────

def test_fresh_store_has_the_005_schema(tmp_path):
    """_init() creates the current schema and fresh stores jump straight to the
    latest user_version without running any migration — so a column added only
    in 005 would be missing from every new store."""
    db = Database(tmp_path)
    project_cols = {r[1] for r in db.conn.execute("PRAGMA table_info(projects)")}
    note_cols = {r[1] for r in db.conn.execute("PRAGMA table_info(notes)")}
    tables = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    assert {"description_author", "description_reviewed_at",
            "attention_dismissed_at"} <= project_cols
    assert "is_global" in note_cols
    assert "project_roots" in tables


def test_migration_005_upgrades_a_0_1_3_store(tmp_path):
    """A store created before 005 gains the schema without losing data.

    The fixture carries the FTS table and its insert trigger, not just the
    base tables: migration 004 already proved that a migration can disturb
    FTS, and a store without it lets 005 look fine while search is quietly
    empty. The search assertion below is the point of that.
    """
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    conn.executescript("""
        CREATE TABLE notes (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
            project TEXT, tags TEXT NOT NULL DEFAULT '[]', session_id TEXT,
            author_client TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE notes_fts USING fts5(
            title, content, content=notes, content_rowid=rowid,
            tokenize='porter unicode61'
        );
        CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
            INSERT INTO notes_fts(rowid, title, content)
            VALUES (new.rowid, new.title, new.content);
        END;
        CREATE TABLE projects (
            name TEXT PRIMARY KEY, overview TEXT NOT NULL DEFAULT '',
            conventions TEXT NOT NULL DEFAULT '', open_questions TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        INSERT INTO notes VALUES ('n1','kept','searchable body',NULL,'["#legacytag"]',NULL,NULL,'t','t');
        INSERT INTO projects VALUES ('p1','curated overview','conv','questions','t');
    """)
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    db = Database(tmp_path)  # runs 005, then 006
    proj = found(db.get_project("p1"))
    assert proj["overview"] == "curated overview"   # untouched by the migration
    assert proj["open_questions"] == "questions"
    assert proj["description_author"] is None       # unknown, not fabricated
    assert proj["roots"] == []                      # nothing invented
    assert found(db.get_note("n1"))["is_global"] is False
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST

    # the migration must not have disturbed the FTS index
    hits = db.search("searchable")
    assert [h["id"] for h in hits] == ["n1"]
    assert hits[0]["is_global"] is False

    # 006 rebuilt the index this fixture created two columns wide, and the
    # rebuild reached a note written long before tags were indexed
    assert [h["id"] for h in db.search("legacytag")] == ["n1"]


def test_migrating_backs_the_store_up_first(tmp_path, capsys):
    """A migration is one-way: afterwards an older build refuses to open the
    store, which is correct but irreversible — and this store is built to be
    synced between machines that may not upgrade together. The copy uses
    SQLite's backup API, not a file copy, because in WAL mode the file alone
    is missing whatever is still in store.db-wal.
    """
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    conn.executescript("""
        CREATE TABLE notes (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
            project TEXT, tags TEXT NOT NULL DEFAULT '[]', session_id TEXT,
            author_client TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO notes VALUES ('n1','precious','body',NULL,'[]',NULL,NULL,'t','t');
    """)
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    Database(tmp_path)
    backup = tmp_path / BACKUP_NAME
    assert backup.exists(), "the pre-migration copy must exist"

    old = sqlite3.connect(backup)
    assert old.execute("PRAGMA user_version").fetchone()[0] == 4, \
        "the backup must be the PRE-migration schema, not a copy taken after"
    assert old.execute("SELECT title FROM notes").fetchone()[0] == "precious"
    old.close()

    assert "Pre-upgrade copy kept at" in capsys.readouterr().err


def test_the_backup_predates_the_schema_creation(tmp_path):
    """`_init()` creates any table the old store lacks and commits, so a
    backup taken after it is not a pre-upgrade copy at all — and
    `_create_project_roots` can raise after that commit, which would leave a
    written-to store with no backup."""
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    conn.executescript(
        "CREATE TABLE notes (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "content TEXT NOT NULL, project TEXT, tags TEXT NOT NULL DEFAULT '[]', "
        "session_id TEXT, author_client TEXT, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL);"
    )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    Database(tmp_path)

    old = sqlite3.connect(tmp_path / BACKUP_NAME)
    tables = {r[0] for r in old.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    old.close()
    assert "project_roots" not in tables, "the copy must predate _init()'s schema"
    assert "projects" not in tables


def test_an_unusable_existing_backup_is_not_trusted(tmp_path, capsys):
    """An interrupted backup leaves a file with the right name and no
    content. Reusing it would tell the user they hold a rollback of a one-way
    migration when they hold nothing — worse than reporting none."""
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    conn.executescript(
        "CREATE TABLE notes (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "content TEXT NOT NULL, project TEXT, tags TEXT NOT NULL DEFAULT '[]', "
        "session_id TEXT, author_client TEXT, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL);"
        "INSERT INTO notes VALUES ('n1','kept','b',NULL,'[]',NULL,NULL,'t','t');"
    )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    stale = tmp_path / BACKUP_NAME
    stale.write_bytes(b"")  # the interrupted-attempt shape

    Database(tmp_path)

    assert stale.stat().st_size > 0, "the useless file must have been replaced"
    old = sqlite3.connect(stale)
    assert old.execute("PRAGMA user_version").fetchone()[0] == 4
    assert old.execute("SELECT title FROM notes").fetchone()[0] == "kept"
    old.close()
    assert not (tmp_path / f"{BACKUP_NAME}.tmp").exists()


def test_a_fresh_store_is_not_backed_up(tmp_path):
    """Nothing to preserve, and a stray file in every new store would be noise."""
    Database(tmp_path)
    assert not list(tmp_path.glob("store.db.bak-*"))


def test_migration_005_on_an_old_store_that_has_no_projects_table(tmp_path):
    """The trap `_add_column_if_missing` exists for.

    `_init()` runs BEFORE `_migrate()` and creates the current schema for any
    table the old store lacks — so a store without `projects` gets one already
    carrying 005's columns, and a bare `ALTER TABLE ADD COLUMN` would then die
    with "duplicate column name", taking the whole store down on open.
    """
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    conn.executescript("""
        CREATE TABLE notes (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
            project TEXT, tags TEXT NOT NULL DEFAULT '[]', session_id TEXT,
            author_client TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO notes VALUES ('n1','kept','body',NULL,'[]',NULL,NULL,'t','t');
    """)
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    db = Database(tmp_path)  # must not raise
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(projects)")}
    assert "description_author" in cols
    assert found(db.get_note("n1"))["title"] == "kept"


# ── evoctx init ───────────────────────────────────────────────────────────────

def test_init_records_the_root(cli, monkeypatch, capsys, tmp_path):
    proj_dir = tmp_path / "myproject"
    proj_dir.mkdir()
    run(cli, monkeypatch, "init", "--dir", str(proj_dir))
    assert "Registered project: myproject" in capsys.readouterr().out
    assert cli.db.project_roots("myproject")[0]["root_path"] == normalize_root(proj_dir)


def test_init_rerun_links_the_root_without_touching_the_description(
    cli, monkeypatch, capsys, tmp_path
):
    """Recording where code lives must never cost a curated description.
    Before this, the only way past 'already registered' was --force, which
    rewrites overview and conventions and blanks open_questions."""
    proj_dir = tmp_path / "myproject"
    proj_dir.mkdir()
    cli.db.upsert_project("myproject", overview="hand written",
                          conventions="hand written conventions",
                          open_questions="still open")
    capsys.readouterr()

    run(cli, monkeypatch, "init", "--dir", str(proj_dir))
    out = capsys.readouterr().out
    assert "linked this directory" in out

    proj = found(cli.db.get_project("myproject"))
    assert proj["overview"] == "hand written"
    assert proj["conventions"] == "hand written conventions"
    assert proj["open_questions"] == "still open"
    assert [r["root_path"] for r in proj["roots"]] == [normalize_root(proj_dir)]


def test_init_refuses_a_directory_owned_by_another_project(
    cli, monkeypatch, capsys, tmp_path
):
    """One directory, one project — binding it twice would make resolution
    ambiguous later, so ask rather than guess."""
    proj_dir = tmp_path / "shared"
    proj_dir.mkdir()
    cli.db.upsert_project("first")
    cli.db.add_project_root("first", proj_dir)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "init", "--dir", str(proj_dir), "--name", "second")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "already registered to project 'first'" in out
    assert cli.db.get_project("second") is None, "nothing may be created on refusal"
    # the advice must name flags that exist, or the refusal is a dead end
    assert "--relink" in out
    assert "--name first" in out


def test_init_relink_moves_the_directory_after_confirmation(
    cli, monkeypatch, capsys, tmp_path
):
    proj_dir = tmp_path / "shared"
    proj_dir.mkdir()
    cli.db.upsert_project("first")
    cli.db.add_project_root("first", proj_dir)
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda: "y")
    run(cli, monkeypatch, "init", "--dir", str(proj_dir), "--name", "second", "--relink")
    out = capsys.readouterr().out
    assert "from project 'first' to 'second'" in out
    assert cli.db.project_for_root(proj_dir) == "second"
    assert cli.db.project_roots("first") == []


def test_init_relink_declined_changes_nothing(cli, monkeypatch, capsys, tmp_path):
    """--relink is destructive, so it asks — same _confirm/--force pair as
    `delete`. A typo'd --name would otherwise unlink a real project's root in
    one command and announce it afterwards."""
    proj_dir = tmp_path / "shared"
    proj_dir.mkdir()
    cli.db.upsert_project("first")
    cli.db.add_project_root("first", proj_dir)
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda: "n")
    run(cli, monkeypatch, "init", "--dir", str(proj_dir), "--name", "typoed", "--relink")
    assert "Cancelled" in capsys.readouterr().out
    assert cli.db.project_for_root(proj_dir) == "first"
    assert cli.db.get_project("typoed") is None


def test_relink_leaves_other_machines_bindings_alone(db, tmp_path):
    """A synced store holds rows describing other machines' filesystems.
    Unbinding here must not delete a laptop's binding from a desktop."""
    db.upsert_project("alpha")
    db.add_project_root("alpha", tmp_path)
    db.add_project_root("alpha", tmp_path, machine="other-box")

    db.remove_project_root("alpha", tmp_path)
    remaining = db.project_roots("alpha")
    assert [r["machine"] for r in remaining] == ["other-box"]


def test_register_project_writes_nothing_when_it_refuses(db, tmp_path):
    """A foreign owner is refused before anything is written."""
    db.upsert_project("owner")
    db.add_project_root("owner", tmp_path)

    with pytest.raises(RootOwnedByAnotherProject):
        db.register_project("newcomer", tmp_path, overview="ov")
    assert db.get_project("newcomer") is None
    assert db.project_for_root(tmp_path) == "owner"


def test_register_project_rolls_back_a_failure_after_the_project_insert(db, tmp_path):
    """The transaction has to be real, not just an early return.

    The test above passes even without one, because the ownership check runs
    before the first write — so it proves ordering, not atomicity. This forces
    a failure *between* the project insert and the root insert, which is the
    only thing that can distinguish `with self.conn:` from three loose
    statements. Without it, a mid-call failure leaves a project committed with
    no root and no description anyone approved.
    """
    import sqlite3

    class FailAtRootInsert:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            if "INSERT INTO project_roots" in sql:
                raise sqlite3.OperationalError("simulated failure mid-transaction")
            return self._inner.execute(sql, *a, **k)

        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, *e):
            return self._inner.__exit__(*e)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real = db.conn
    db.conn = FailAtRootInsert(real)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.register_project("halfway", tmp_path, overview="ov")
    finally:
        db.conn = real

    assert db.get_project("halfway") is None, "the project insert must have rolled back"
    assert db.project_roots() == []


def test_init_attributes_scraped_text_to_the_scraper_not_to_a_human(
    cli, monkeypatch, capsys, tmp_path
):
    """`cli` is the label meaning a person typed it. Nobody wrote
    "Project at D:\\... [Stack: react]" — attributing it to a human would make
    boilerplate indistinguishable from a curated description, which is the
    thing description provenance exists to prevent."""
    proj_dir = tmp_path / "scraped"
    proj_dir.mkdir()
    run(cli, monkeypatch, "init", "--dir", str(proj_dir))
    assert found(cli.db.get_project("scraped"))["description_author"] == "init-scrape"

    # and again through --force, which re-scrapes an existing project
    cli.db.upsert_project("scraped", overview="hand written")
    assert found(cli.db.get_project("scraped"))["description_author"] == "cli"
    run(cli, monkeypatch, "init", "--dir", str(proj_dir), "--force")
    assert found(cli.db.get_project("scraped"))["description_author"] == "init-scrape"


def test_init_force_is_one_transaction(db, tmp_path):
    """--force rewrites the description and binds the root; a failure between
    them would leave a re-scraped description bound to nothing."""
    import sqlite3

    db.upsert_project("p1", overview="hand written")

    class FailAtRootInsert:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            if "INSERT INTO project_roots" in sql:
                raise sqlite3.OperationalError("simulated")
            return self._inner.execute(sql, *a, **k)

        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, *e):
            return self._inner.__exit__(*e)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real = db.conn
    db.conn = FailAtRootInsert(real)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.register_project("p1", tmp_path, overview="rescraped",
                                overwrite_description=True)
    finally:
        db.conn = real

    assert found(db.get_project("p1"))["overview"] == "hand written", \
        "the description rewrite must roll back with the failed bind"


def test_force_keeps_open_questions(cli, monkeypatch, capsys, tmp_path):
    """--force replaces overview and conventions. It used to blank open
    questions too, via upsert_project's default; four places said so in
    prose. Pin the behaviour so the prose stays true."""
    proj_dir = tmp_path / "p"
    proj_dir.mkdir()
    cli.db.upsert_project("p", overview="old", conventions="old conv",
                          open_questions="still open")
    capsys.readouterr()

    run(cli, monkeypatch, "init", "--dir", str(proj_dir), "--name", "p", "--force")
    proj = found(cli.db.get_project("p"))
    assert proj["open_questions"] == "still open"
    assert proj["overview"] != "old"


def test_force_on_unchanged_text_keeps_author_and_review(db, tmp_path):
    """Two writers of description provenance must apply the same rule. The
    UPDATE path used to gate nothing, so re-running init --force in an
    unchanged repo destroyed a human's approval for text nobody touched."""
    db.upsert_project("p", overview="ov", conventions="c", author="init-scrape")
    db.conn.execute("UPDATE projects SET description_reviewed_at='T1' WHERE name='p'")
    db.conn.commit()

    db.register_project("p", tmp_path, overview="ov", conventions="c",
                        author="init-scrape", overwrite_description=True)
    proj = found(db.get_project("p"))
    assert proj["description_reviewed_at"] == "T1"
    assert proj["description_author"] == "init-scrape"


def _two_projects_one_dir(cli, tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    cli.db.upsert_project("alpha")
    cli.db.add_project_root("alpha", d)
    cli.db.upsert_project("beta", overview="CURATED", conventions="curated conv")
    return d


def test_relink_force_still_prompts(cli, monkeypatch, capsys, tmp_path):
    """`--force`/`--rescrape` is the description switch, NOT a confirmation
    bypass. Conflating them let `--relink --force` move a directory AND
    overwrite a curated description with the user having asked for neither.

    Answering "n" here proves --force did not skip the question.
    """
    d = _two_projects_one_dir(cli, tmp_path)
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda: "n")
    run(cli, monkeypatch, "init", "--dir", str(d), "--name", "beta",
        "--relink", "--force")

    assert "Cancelled" in capsys.readouterr().out
    assert cli.db.project_for_root(d) == "alpha", "nothing may move on a refusal"
    assert found(cli.db.get_project("beta"))["overview"] == "CURATED"


def test_relink_yes_skips_the_prompt_and_keeps_the_description(
    cli, monkeypatch, capsys, tmp_path
):
    """--yes bypasses the confirmation and nothing else.

    `input` is patched to raise: if the prompt were still reached the test
    would error, so this proves the bypass rather than assuming it.
    """
    d = _two_projects_one_dir(cli, tmp_path)
    capsys.readouterr()

    def _no_prompt_expected():
        raise AssertionError("--yes must not reach the confirmation prompt")

    monkeypatch.setattr("builtins.input", _no_prompt_expected)
    run(cli, monkeypatch, "init", "--dir", str(d), "--name", "beta", "--relink", "--yes")

    out = capsys.readouterr().out
    assert "from project 'alpha' to 'beta'" in out, "the losing project must be named"
    assert cli.db.project_for_root(d) == "beta"
    proj = found(cli.db.get_project("beta"))
    assert proj["overview"] == "CURATED", "relinking must not touch the description"
    assert proj["conventions"] == "curated conv"


def test_rescrape_alone_does_replace_the_description(cli, monkeypatch, capsys, tmp_path):
    """The other half of the split: --rescrape means what it says, with no
    prompt, because replacing a description is what the user asked for."""
    d = tmp_path / "p"
    d.mkdir()
    cli.db.upsert_project("p", overview="CURATED")
    capsys.readouterr()

    run(cli, monkeypatch, "init", "--dir", str(d), "--name", "p", "--rescrape")
    assert found(cli.db.get_project("p"))["overview"] != "CURATED"


def test_global_notes_cannot_crowd_out_a_projects_own_recent_notes(db):
    """`recent_notes` mixes the project's notes with global ones, and globals
    are shared across the store and never age out — so one merged LIMIT lets
    five global notes push out every note the project has. get_project is the
    session-start read; returning a project record that says nothing about the
    project is worse than omitting globals."""
    db.upsert_project("api")
    db.add_note("api one", "x", project="api")
    db.add_note("api two", "x", project="api")
    for i in range(6):
        db.add_note(f"global {i}", "x", is_global=True)

    titles = [n["title"] for n in found(db.get_project("api"))["recent_notes"]]
    assert "api one" in titles and "api two" in titles
    assert any(t.startswith("global") for t in titles), "globals still appear"
    # and a reader can tell which is which
    by_title = {n["title"]: n for n in found(db.get_project("api"))["recent_notes"]}
    assert by_title["api one"]["is_global"] is False
    assert by_title["global 5"]["is_global"] is True


def test_list_and_search_agree_about_what_is_in_a_project(db):
    """Two adjacent commands answering 'what is in X' with different sets is a
    defect whichever answer is right."""
    db.upsert_project("api")
    db.add_note("api note", "shared keyword", project="api")
    db.add_note("global note", "shared keyword", is_global=True)

    listed = {n["title"] for n in db.list_notes(project="api")}
    searched = {h["title"] for h in db.search("shared", project="api")}
    assert listed == searched == {"api note", "global note"}


def test_delete_by_project_counts_only_what_it_will_delete(cli, monkeypatch, capsys):
    """`list_notes(project=X)` includes globals because they apply to X, but
    `delete --project X` removes only notes filed under X. Counting the wider
    set would make a destructive prompt ask permission for work it will not
    do, and would defeat the nothing-to-delete short-circuit."""
    cli.db.upsert_project("api")
    for i in range(3):
        cli.db.add_note(f"global {i}", "x", is_global=True)
    capsys.readouterr()

    # a project with no notes of its own must still short-circuit
    run(cli, monkeypatch, "delete", "--project", "api", "--force")
    out = capsys.readouterr().out
    assert "No notes in project 'api'." in out
    assert len(cli.db.list_notes()) == 3, "no global note may be deleted"

    # and the prompt itself must count only api's own — no --force here, or
    # _confirm returns early and never prints the number under test
    cli.db.add_note("api own", "x", project="api")
    monkeypatch.setattr("builtins.input", lambda: "y")
    run(cli, monkeypatch, "delete", "--project", "api")
    out = capsys.readouterr().out
    assert "Delete 1 note(s)" in out, "the prompt must count only api's own"
    assert "Deleted 1 note(s)." in out
    assert len(cli.db.list_notes()) == 3, "the globals survive"


def test_a_global_note_reads_differently_from_the_global_project(cli, monkeypatch, capsys):
    """`evoctx sync` creates a real project named `global`. A bare "global"
    marker would make a note filed under it indistinguishable from a
    cross-project note — the same conflation, rebuilt on the display side."""
    cli.db.upsert_project("global")
    cli.db.add_note("filed under the global project", "x", project="global")
    cli.db.add_note("spans every project", "x", is_global=True)
    capsys.readouterr()

    run(cli, monkeypatch, "list")
    lines = capsys.readouterr().out.splitlines()
    scoped = next(l for l in lines if "filed under the global project" in l)
    spanning = next(l for l in lines if "spans every project" in l)
    assert "[global]" in spanning
    assert "[global]" not in scoped


def test_a_scoped_grant_cannot_write_a_global_note(db, tmp_path):
    """A global note lands in EVERY project's scoped read, so claiming global
    scope requires holding it.

    check_write_project cannot catch this: a global note carries project=None
    and allows_project(None) is True by design, so project-less notes stay
    visible to scoped grants. Right for reads, wrong as a write loophole.
    """
    import os
    import time

    from evoctx.grants import GrantStore
    from evoctx.policy import PolicyDenied, PolicyEngine

    # construct first: GrantStore writes the default grants.yaml and activates
    # "everything" on first run, so overwriting before that raises
    engine = PolicyEngine(db, GrantStore(tmp_path))
    y = tmp_path / "grants.yaml"
    y.write_text(
        'grants:\n'
        '  - name: narrow\n'
        '    client: "*"\n'
        '    tools: [write_note]\n'
        '    scope: {projects: ["alpha"]}\n'
        '    expires: "+1d"\n'
        '  - name: wide\n'
        '    client: "*"\n'
        '    tools: [write_note]\n'
        '    scope: {projects: ["*"]}\n'
        '    expires: "+1d"\n',
        encoding="utf-8",
    )
    stamp = time.time() + 2  # 1s mtime granularity would hide the change
    os.utime(y, (stamp, stamp))

    engine.grants.activate("narrow")
    with pytest.raises(PolicyDenied, match="NOT saved"):
        engine.t_write_note("c", "t", "x", None, None, None, is_global=True)
    assert db.list_notes() == [], "nothing may be written on a denial"

    # A project-less note is no longer a way through either: since 0.1.4 step 4
    # a write with no project and no global flag is refused when nothing
    # resolves, because filing it nowhere is the silence-conflated-with-intent
    # defect this release removes. Scoped to `alpha`, writing under that name
    # is the path that remains open.
    db.upsert_project("alpha")
    engine.t_write_note("c", "plain", "x", "alpha", None, None)
    assert len(db.list_notes()) == 1

    # and a grant that holds every project may write a global one
    engine.grants.activate("wide")
    engine.t_write_note("c", "spans all", "x", None, None, None, is_global=True)
    assert len(db.list_notes()) == 2


def test_a_cross_project_root_conflict_raises_even_without_the_precheck(db, tmp_path):
    """The insert itself must refuse, not just the Python pre-check.

    The pre-check can be raced by a second writer — the two-process case this
    store is built for — and that path has to end in the same explanation, not
    a bare IntegrityError.
    """
    from evoctx.db import _insert_root, normalize_root, this_machine
    db.upsert_project("first")
    db.upsert_project("second")
    db.add_project_root("first", tmp_path)

    with pytest.raises(RootOwnedByAnotherProject) as exc:
        _insert_root(db.conn, "second", normalize_root(tmp_path), this_machine())
    assert exc.value.owner == "first"


def test_empty_projects_scope_is_refused_rather_than_read_as_everything(tmp_path):
    """`projects: []` reads as "no projects" and used to parse as full access,
    because `or ["*"]` cannot tell empty from missing. A scope failing open is
    bad on its own; it now also decides who may write global notes."""
    from evoctx.grants import GrantError, GrantStore
    (tmp_path / "grants.yaml").write_text(
        'grants:\n  - name: ambiguous\n    tools: [get_by_id]\n'
        '    scope: {projects: []}\n    expires: "+1d"\n',
        encoding="utf-8",
    )
    with pytest.raises(GrantError, match="ambiguous"):
        GrantStore(tmp_path).grants()


def test_doctor_still_checks_note_scope_on_a_synced_store(db, tmp_path):
    """The machine-rename branch used to `return`, skipping every check after
    it — including one that has nothing to do with roots. A synced store's
    first run on a new box lands exactly there."""
    from evoctx.install import _check_project_roots

    db.upsert_project("p")
    db.conn.execute(
        "INSERT INTO project_roots (project, root_path, machine, added_at) "
        "VALUES ('p', 'c:\\\\elsewhere', 'other-laptop', 't')")
    db.add_note("conflicted", "x", project="p")
    db.conn.execute("UPDATE notes SET is_global = 1 WHERE title = 'conflicted'")
    db.conn.commit()

    checks: list = []
    _check_project_roots(checks, db)
    names = [c[0] for c in checks]
    assert "project roots" in names
    assert "note scope" in names, "the roots branch must not swallow it"
    assert dict((c[0], c[1]) for c in checks)["note scope"] is False


def test_an_empty_projects_scope_in_any_spelling_is_refused(tmp_path):
    """A bare `projects:` parses to None, not [], and failed open the same
    way — and it is the spelling a human is likelier to type."""
    from evoctx.grants import GrantError, GrantStore

    for i, spelling in enumerate(("projects: []", "projects:", "projects: null")):
        d = tmp_path / f"case{i}"
        d.mkdir()
        (d / "grants.yaml").write_text(
            "grants:\n"
            "  - name: g\n"
            "    tools: [get_by_id]\n"
            "    scope:\n"
            f"      {spelling}\n"
            '    expires: "+1d"\n',
            encoding="utf-8",
        )
        with pytest.raises(GrantError, match="ambiguous"):
            GrantStore(d).grants()


def test_allows_all_projects_recognises_every_match_all_glob():
    from evoctx.grants import Grant
    assert Grant("g", projects=["*"]).allows_all_projects()
    assert Grant("g", projects=["**"]).allows_all_projects()
    assert Grant("g", projects=["alpha", "*"]).allows_all_projects()
    assert not Grant("g", projects=["proj-*"]).allows_all_projects()
    assert not Grant("g", projects=["alpha"]).allows_all_projects()


def test_recent_notes_are_actually_in_recency_order(db):
    """The key is named recent_notes and the CLI header says "Recent notes",
    so position has to mean recency. Appending the globals block unsorted
    would leave element 6 newer than element 1."""
    db.upsert_project("api")
    db.add_note("oldest api", "x", project="api")
    db.add_note("a global", "x", is_global=True)
    db.add_note("newest api", "x", project="api")

    recent = found(db.get_project("api"))["recent_notes"]
    stamps = [n["created_at"] for n in recent]
    assert stamps == sorted(stamps, reverse=True)


def test_get_project_omits_roots_when_it_did_not_fetch_them(db, tmp_path):
    """"not fetched" must not look like "genuinely none" — a caller skipping
    the query for speed and then testing `if not proj["roots"]` would
    otherwise get a confident wrong answer."""
    db.upsert_project("p")
    db.add_project_root("p", tmp_path)
    assert "roots" not in found(db.get_project("p", with_roots=False))
    assert found(db.get_project("p"))["roots"]


def test_blanking_a_description_records_no_author(db, tmp_path):
    """The UPDATE path must apply the same author guard as the INSERT path, or
    an empty description gets stamped with an author nobody earned."""
    db.upsert_project("p", overview="something", author="claude-code")
    db.register_project("p", tmp_path, overview="", conventions="",
                        author="claude-code", overwrite_description=True)
    assert found(db.get_project("p"))["description_author"] is None


def test_rescrape_clears_a_review_when_the_text_changes(db, tmp_path):
    """The clearing half of the extracted rule, on the register_project path."""
    db.upsert_project("p", overview="old", author="init-scrape")
    db.conn.execute("UPDATE projects SET description_reviewed_at='T1' WHERE name='p'")
    db.conn.commit()

    db.register_project("p", tmp_path, overview="new text", author="init-scrape",
                        overwrite_description=True)
    assert found(db.get_project("p"))["description_reviewed_at"] is None


def test_a_global_note_surfaces_in_a_project_scoped_search(db):
    """'Spans every project' has to mean something. A global note that never
    appears under any project would be findable only by an unscoped search —
    and the server tells clients otherwise."""
    db.upsert_project("api")
    db.add_note("api thing", "shared keyword", project="api")
    db.add_note("never use raw SQL", "shared keyword", is_global=True)

    titles = [h["title"] for h in db.search("shared", project="api")]
    assert "never use raw SQL" in titles
    assert "api thing" in titles

    recent = [n["title"] for n in found(db.get_project("api"))["recent_notes"]]
    assert "never use raw SQL" in recent


def test_a_root_must_be_absolute(db, tmp_path):
    """A relative path resolves against the process cwd — right for the CLI,
    wrong for the MCP server, whose cwd is whatever the client launched it
    with."""
    db.upsert_project("p")
    with pytest.raises(ValueError, match="absolute"):
        db.add_project_root("p", "relative/path")
    with pytest.raises(ValueError, match="absolute"):
        db.register_project("p2", "relative/path")


def test_unbind_removes_a_binding(cli, monkeypatch, capsys, tmp_path):
    """init is the only thing that creates a binding, so without this a root
    recorded one directory too deep is permanent."""
    d = tmp_path / "wrong"
    d.mkdir()
    run(cli, monkeypatch, "init", "--dir", str(d))
    capsys.readouterr()

    run(cli, monkeypatch, "unbind", "--dir", str(d))
    out = capsys.readouterr().out
    assert "Unbound" in out
    assert cli.db.project_for_root(d) is None
    assert cli.db.get_project("wrong") is not None, "the project must survive"


def test_add_global_from_the_cli(cli, monkeypatch, capsys):
    """Parity: the MCP write_note tool can mark a note global, so a human must
    be able to as well — otherwise only the assistant can express it."""
    run(cli, monkeypatch, "add", "spans everything", "--global", "--text", "x")
    run(cli, monkeypatch, "add", "no project chosen", "--text", "y")
    capsys.readouterr()

    notes = {n["title"]: n for n in cli.db.list_notes()}
    assert notes["spans everything"]["is_global"] is True
    assert notes["no project chosen"]["is_global"] is False

    # and the two must not read identically to a human. Assert the
    # association, not the bare word: `evoctx sync` creates a real project
    # named "global", so "global" in out would pass on any synced store.
    run(cli, monkeypatch, "list")
    lines = capsys.readouterr().out.splitlines()
    global_line = next(l for l in lines if "spans everything" in l)
    plain_line = next(l for l in lines if "no project chosen" in l)
    assert "global" in global_line
    assert "global" not in plain_line


def test_add_global_with_a_project_is_refused(cli, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "add", "t", "--global", "--project", "p1", "--text", "x")
    assert exc.value.code == 1
    assert "both global and scoped" in capsys.readouterr().out


def test_duplicate_root_bindings_refuse_to_open_with_an_explanation(tmp_path):
    """A store whose data already violates the uniqueness rule must say so.
    Without the check, `CREATE UNIQUE INDEX` raises a bare IntegrityError on
    open and the store simply stops working with no way to tell why."""
    import sqlite3
    db = Database(tmp_path)
    db.upsert_project("a")
    db.upsert_project("b")
    db.conn.execute("DROP INDEX idx_project_roots_path")
    for proj in ("a", "b"):
        db.conn.execute(
            "INSERT INTO project_roots (project, root_path, machine, added_at) "
            "VALUES (?, ?, ?, ?)", (proj, "c:\\shared", this_machine(), "t"))
    db.conn.commit()
    db.conn.close()

    with pytest.raises(DuplicateRootBinding) as exc:
        Database(tmp_path)
    assert "c:\\shared" in str(exc.value)
    assert "one project per machine" in str(exc.value)


def test_delete_project_warns_about_root_collateral(cli, monkeypatch, capsys, tmp_path):
    proj_dir = tmp_path / "doomed"
    proj_dir.mkdir()
    run(cli, monkeypatch, "init", "--dir", str(proj_dir))
    capsys.readouterr()

    run(cli, monkeypatch, "delete-project", "doomed", "--force")
    out = capsys.readouterr().out
    assert "1 directory binding(s) will be removed" in out
    assert cli.db.project_roots() == []


def test_init_after_delete_project_can_reuse_the_directory(cli, monkeypatch, capsys, tmp_path):
    """The most likely real sequence: register, delete the project, register
    again in the same directory. A stranded root would make that a hard
    refusal pointing at a project that no longer exists."""
    proj_dir = tmp_path / "myproject"
    proj_dir.mkdir()
    run(cli, monkeypatch, "init", "--dir", str(proj_dir))
    cli.db.delete_project("myproject")
    capsys.readouterr()

    run(cli, monkeypatch, "init", "--dir", str(proj_dir))
    assert "Registered project: myproject" in capsys.readouterr().out
    assert cli.db.project_for_root(proj_dir) == "myproject"
