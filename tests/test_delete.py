def test_delete_note(db):
    note_id = db.add_note("t", "x")
    assert db.delete_note(note_id) is True
    assert db.get_note(note_id) is None


def test_delete_note_missing_returns_false(db):
    assert db.delete_note("nope") is False


def test_delete_removes_from_fts(db):
    note_id = db.add_note("findable secret", "unique-marker-xyz")
    assert len(db.search("unique-marker-xyz")) == 1
    db.delete_note(note_id)
    assert len(db.search("unique-marker-xyz")) == 0


def test_delete_notes_by_project(db):
    db.add_note("a", "x", project="p1")
    db.add_note("b", "x", project="p1")
    db.add_note("c", "x", project="p2")
    n = db.delete_notes_by_project("p1")
    assert n == 2
    assert len(db.list_notes(project="p1")) == 0
    assert len(db.list_notes(project="p2")) == 1


def test_delete_notes_by_tag(db):
    db.add_note("a", "x", tags=["#secret"])
    db.add_note("b", "x", tags=["#secret", "#other"])
    db.add_note("c", "x", tags=["#other"])
    n = db.delete_notes_by_tag("#secret")
    assert n == 2
    remaining = db.list_notes()
    assert len(remaining) == 1
    assert remaining[0]["title"] == "c"


def test_delete_project_keeps_notes_by_default(db):
    db.upsert_project("p1")
    db.add_note("a", "x", project="p1")
    assert db.delete_project("p1") is True
    assert db.get_project("p1") is None
    assert len(db.list_notes(project="p1")) == 1  # note survives, just unlinked from a project record


def test_delete_project_with_notes_cascades(db):
    db.upsert_project("p1")
    db.add_note("a", "x", project="p1")
    db.add_note("b", "x", project="p1")
    assert db.delete_project("p1", with_notes=True) is True
    assert len(db.list_notes(project="p1")) == 0


def test_delete_project_missing_returns_false(db):
    assert db.delete_project("nope") is False
