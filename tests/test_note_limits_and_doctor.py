import pytest

from evoctx.db import MAX_NOTE_BYTES, Database, NoteTooLarge
from evoctx.grants import GrantStore
from evoctx.install import _human_size, doctor


def test_note_over_limit_rejected(db):
    huge = "x" * (MAX_NOTE_BYTES + 1)
    with pytest.raises(NoteTooLarge, match="over the"):
        db.add_note("t", huge)


def test_note_at_limit_accepted(db):
    exactly = "x" * MAX_NOTE_BYTES
    note_id = db.add_note("t", exactly)
    assert db.get_note(note_id) is not None


def test_note_under_limit_accepted(db):
    note_id = db.add_note("t", "short content")
    assert db.get_note(note_id) is not None


def test_rejected_note_writes_nothing(db):
    huge = "x" * (MAX_NOTE_BYTES + 1)
    with pytest.raises(NoteTooLarge):
        db.add_note("t", huge)
    assert db.list_notes() == []


# ── human_size ────────────────────────────────────────────────────────────────

def test_human_size_formatting():
    assert _human_size(500) == "500B"
    assert _human_size(2048) == "2.0KB"
    assert _human_size(5 * 1024 * 1024) == "5.0MB"


# ── doctor: workspace resolution ──────────────────────────────────────────────
# Called directly rather than through doctor(), which resolves against the test
# runner's own cwd — the repo — and would answer a different question.

def test_doctor_reports_which_columns_the_index_covers(tmp_path):
    from evoctx.install import _check_fts_index

    db = Database(tmp_path / "store")
    checks: list = []
    _check_fts_index(checks, db)
    name, ok, detail = checks[0]
    assert (name, ok) == ("fts5 search", True)
    assert "title, content, tags" in detail


def test_doctor_fails_when_the_index_does_not_cover_tags(tmp_path):
    """The branch that matters, planted rather than reasoned about.

    A store at v6 whose index lacks `tags` answers every tag query with
    silence — and silence reads as "nothing was written about that". Left
    inline in `doctor()`, this branch shipped never having been executed.
    """
    db = Database(tmp_path / "store")
    db.conn.executescript(
        "DROP TRIGGER notes_ai; DROP TRIGGER notes_ad; DROP TRIGGER notes_au;"
        "DROP TABLE notes_fts;"
        "CREATE VIRTUAL TABLE notes_fts USING fts5("
        "  title, content, content=notes, content_rowid=rowid,"
        "  tokenize='porter unicode61');"
    )

    from evoctx.install import _check_fts_index
    checks: list = []
    _check_fts_index(checks, db)
    name, ok, detail = checks[0]
    assert (name, ok) == ("fts5 search", False)
    assert "but not tags" in detail
    # the remedy must be one that can actually work — `doctor` has already run
    # every migration by the time it gets here
    assert "re-opening the store will NOT fix it" in detail
    assert "store.db.bak-pre-migration-006" in detail


def test_doctor_fails_when_the_index_is_gone_entirely(tmp_path):
    db = Database(tmp_path / "store")
    db.conn.executescript(
        "DROP TRIGGER notes_ai; DROP TRIGGER notes_ad; DROP TRIGGER notes_au;"
        "DROP TABLE notes_fts;"
    )

    from evoctx.install import _check_fts_index
    checks: list = []
    _check_fts_index(checks, db)
    assert checks[0][:2] == ("fts5 search", False)
    assert "FTS5 missing" in checks[0][2]


def test_doctor_workspace_reports_the_resolved_project(tmp_path):
    from evoctx.install import _check_workspace

    db = Database(tmp_path / "store")
    repo = tmp_path / "myrepo"
    repo.mkdir()
    db.register_project("myrepo", repo)

    checks: list = []
    _check_workspace(checks, db, repo)
    name, ok, detail = checks[0]
    assert (name, ok) == ("workspace", True)
    assert "myrepo" in detail


def test_doctor_workspace_fails_when_other_directories_resolve_but_this_one_does_not(tmp_path):
    """The inconsistent case, and the only one worth an exit code: the feature
    is in use and this directory is outside it."""
    from evoctx.install import _check_workspace

    db = Database(tmp_path / "store")
    bound, stranger = tmp_path / "bound", tmp_path / "unregistered"
    bound.mkdir(); stranger.mkdir()
    db.register_project("bound", bound)

    checks: list = []
    _check_workspace(checks, db, stranger)
    name, ok, detail = checks[0]
    assert (name, ok) == ("workspace", False)
    assert "evoctx init" in detail


def test_doctor_workspace_passes_when_nothing_is_bound_anywhere(tmp_path):
    """`doctor` exits non-zero on any FAIL, and every store has zero roots the
    moment it upgrades. Failing on "unresolved" alone would turn the health
    check red for every user on upgrade day, and permanently for anyone whose
    projects are topics rather than directories."""
    from evoctx.install import _check_workspace

    db = Database(tmp_path / "store")
    stranger = tmp_path / "unregistered"
    stranger.mkdir()

    checks: list = []
    _check_workspace(checks, db, stranger)
    name, ok, detail = checks[0]
    assert (name, ok) == ("workspace", True)
    assert "evoctx init" in detail  # still says how to bind it


def test_doctor_workspace_fails_on_a_context_project_that_does_not_exist(tmp_path, monkeypatch):
    """Nobody sets CONTEXT_PROJECT by accident. A human typed a name and it is
    wrong, so every tool call in the store is unresolvable until someone fixes
    it — and `doctor` is the one command a user runs to find exactly that.
    Reporting green would hide the typo in the last place left."""
    from evoctx.install import _check_workspace

    monkeypatch.setenv("CONTEXT_PROJECT", "evobytez-mcp")
    db = Database(tmp_path / "store")
    here = tmp_path / "anywhere"
    here.mkdir()

    checks: list = []
    _check_workspace(checks, db, here)
    name, ok, detail = checks[0]
    assert (name, ok) == ("workspace", False)   # even with nothing bound
    assert "evobytez-mcp" in detail


def test_doctor_workspace_says_a_default_is_not_a_constraint(tmp_path):
    from evoctx.install import _check_workspace

    db = Database(tmp_path / "store")
    here, sibling = tmp_path / "ws" / "api", tmp_path / "ws" / "admin"
    here.mkdir(parents=True); sibling.mkdir(parents=True)
    db.register_project("api", here)
    db.register_project("admin", sibling)

    checks: list = []
    _check_workspace(checks, db, here)
    _name, ok, detail = checks[0]
    assert ok
    assert "not a constraint" in detail
    assert "admin" in detail


# ── doctor: which workspace folders declare the server ────────────────────────

def _workspace_with(tmp_path, *folders: str) -> None:
    entries = ", ".join(f'{{"path": "{f}"}}' for f in folders)
    (tmp_path / "evo.code-workspace").write_text(
        '{"folders": [' + entries + ']}', encoding="utf-8")


def _declare_evoctx(folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".mcp.json").write_text(
        '{"mcpServers": {"evoctx": {"command": "evoctx"}}}', encoding="utf-8")


def test_doctor_workspace_mcp_is_silent_without_a_code_workspace(tmp_path):
    """A single-folder window has no ambiguity to report, and `client configs`
    already covers this directory."""
    from evoctx.install import _check_workspace_configs

    here = tmp_path / "solo"
    _declare_evoctx(here)
    checks: list = []
    _check_workspace_configs(checks, here)
    assert checks == []


def test_doctor_lists_every_workspace_folder_declaring_the_server(tmp_path):
    """Which of them the client honours is the client's behaviour, not ours —
    so report them rather than guess, because the failure this prevents is a
    user believing one config is live while another is."""
    from evoctx.install import _check_workspace_configs

    a, b = tmp_path / "api", tmp_path / "admin"
    _declare_evoctx(a)
    _declare_evoctx(b)
    _workspace_with(tmp_path, "api", "admin")

    checks: list = []
    _check_workspace_configs(checks, a)
    name, ok, detail = checks[0]
    assert (name, ok) == ("workspace mcp", True)
    assert str(a) in detail and str(b) in detail
    assert "client's call" in detail


def test_doctor_flags_a_workspace_where_nothing_declares_the_server(tmp_path):
    from evoctx.install import _check_workspace_configs

    a, b = tmp_path / "api", tmp_path / "admin"
    a.mkdir(); b.mkdir()
    _workspace_with(tmp_path, "api", "admin")

    checks: list = []
    _check_workspace_configs(checks, a)
    name, ok, _detail = checks[0]
    assert (name, ok) == ("workspace mcp", False)


# ── doctor: size + staleness + cloud sync ──────────────────────────────────────

def test_doctor_reports_note_count_and_size(tmp_path):
    db = Database(tmp_path)
    db.add_note("t", "some content")
    GrantStore(tmp_path)
    checks = {c: (ok, detail) for c, ok, detail in doctor(tmp_path, tmp_path)}
    ok, detail = checks["database"]
    assert ok
    assert "1 notes" in detail
    assert "B" in detail  # some byte-unit suffix present


def test_doctor_staleness_no_activity(tmp_path):
    Database(tmp_path)
    GrantStore(tmp_path)
    checks = {c: (ok, detail) for c, ok, detail in doctor(tmp_path, tmp_path)}
    ok, detail = checks["last activity"]
    assert ok is True
    assert "no notes" in detail


def test_doctor_staleness_recent_activity(tmp_path):
    db = Database(tmp_path)
    db.add_note("t", "x")
    GrantStore(tmp_path)
    checks = {c: (ok, detail) for c, ok, detail in doctor(tmp_path, tmp_path)}
    ok, detail = checks["last activity"]
    assert ok is True
    assert "0 day" in detail


def test_doctor_cloud_sync_clean_path(tmp_path):
    Database(tmp_path)
    GrantStore(tmp_path)
    checks = {c: (ok, detail) for c, ok, detail in doctor(tmp_path, tmp_path)}
    ok, detail = checks["cloud sync"]
    assert ok is True


def test_doctor_cloud_sync_flags_onedrive(tmp_path):
    store = tmp_path / "OneDrive" / "evoctx-store"
    Database(store)
    GrantStore(store)
    checks = {c: (ok, detail) for c, ok, detail in doctor(store, tmp_path)}
    ok, detail = checks["cloud sync"]
    assert ok is False
    assert "CONTEXT_STORE" in detail
