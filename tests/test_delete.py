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
    """"Kept, unlinked" — and since 0.1.4 that is literally what happens.

    The notes used to keep pointing at the deleted name, which is a phantom by
    the definition this release uses: `doctor` would report a state the product
    itself created and blessed, permanently, with remediation advice that did
    not apply. Sessions were never cleared at all.
    """
    db.upsert_project("p1")
    note_id = db.add_note("a", "x", project="p1")
    session_id = db.start_session(project="p1")
    assert db.delete_project("p1") is True
    assert db.get_project("p1") is None

    assert db.get_note(note_id) is not None, "the note survives"
    assert db.get_note(note_id)["project"] is None, "and is genuinely unlinked"
    assert db.get_session(session_id)["project"] is None
    assert db.phantom_projects() == {}, "nothing is left pointing at a dead name"


def test_delete_project_with_notes_cascades(db):
    db.upsert_project("p1")
    db.add_note("a", "x", project="p1")
    db.add_note("b", "x", project="p1")
    assert db.delete_project("p1", with_notes=True) is True
    assert len(db.list_notes(project="p1")) == 0


def test_delete_project_missing_returns_false(db):
    assert db.delete_project("nope") is False
