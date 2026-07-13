def test_start_session_creates_open_session(db):
    sid = db.start_session(project="p1", intent="fix the webhook", client="test-client")
    s = db.get_session(sid)
    assert s["open"] is True
    assert s["ended_at"] is None
    assert s["intent"] == "fix the webhook"
    assert s["client"] == "test-client"


def test_end_session_closes_and_stores_summary(db):
    sid = db.start_session(project="p1")
    assert db.end_session(sid, "did the thing") is True
    s = db.get_session(sid)
    assert s["open"] is False
    assert s["summary"] == "did the thing"
    assert s["ended_at"] is not None


def test_end_session_twice_fails(db):
    sid = db.start_session()
    assert db.end_session(sid, "first") is True
    assert db.end_session(sid, "second") is False
    assert db.get_session(sid)["summary"] == "first"


def test_notes_link_to_session(db):
    sid = db.start_session(project="p1")
    db.add_note("session note", "x", project="p1", session_id=sid)
    db.add_note("free note", "y", project="p1")
    s = db.get_session(sid)
    assert [n["title"] for n in s["notes"]] == ["session note"]


def test_recent_sessions_newest_first_with_open_flag(db):
    old = db.start_session(project="p1", intent="old work")
    db.end_session(old, "done")
    crashed = db.start_session(project="p1", intent="interrupted work")
    # never ended — simulates a crashed session

    sessions = db.recent_sessions(project="p1")
    assert sessions[0]["id"] == crashed
    assert sessions[0]["open"] is True
    assert sessions[0]["summary"] is None
    assert sessions[1]["open"] is False


def test_recent_sessions_project_scoping_and_limit(db):
    for i in range(3):
        db.start_session(project="a")
    db.start_session(project="b")
    assert len(db.recent_sessions(project="a")) == 3
    assert len(db.recent_sessions(project="a", limit=2)) == 2
    assert len(db.recent_sessions()) == 4


def test_latest_open_session(db):
    assert db.latest_open_session() is None
    first = db.start_session(project="a")
    second = db.start_session(project="b")
    assert db.latest_open_session()["id"] == second
    assert db.latest_open_session(project="a")["id"] == first
    db.end_session(second, "done")
    assert db.latest_open_session()["id"] == first
