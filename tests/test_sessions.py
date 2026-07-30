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


def _force_same_start_time(db, *session_ids, when="2026-01-01T00:00:00+00:00"):
    """Pin several sessions to one started_at.

    Not artificial: datetime.now() on Windows is coarse enough that two
    sessions started back to back collide on the identical ISO string about
    3% of the time. This makes that case happen every run instead of once in
    thirty, so the ordering is actually asserted rather than sampled.
    """
    for sid in session_ids:
        db.conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (when, sid))
    db.conn.commit()


def test_latest_open_session_breaks_start_time_ties_by_insertion_order(db):
    """Ties must resolve to the session started LAST.

    Without a tiebreaker SQLite returns tied rows in scan order, so LIMIT 1
    hands back the OLDER one — measured wrong on 62 of 62 ties. That is not
    cosmetic: `evoctx end-session` with no --id closes this session, so the
    summary lands on the wrong session and leaves the intended one open.
    """
    first = db.start_session(project="a")
    second = db.start_session(project="a")
    _force_same_start_time(db, first, second)

    latest = db.latest_open_session()
    assert latest is not None
    assert latest["id"] == second

    # and the paged listing agrees — PolicyEngine._fetch_filtered pages through
    # this, and an order that isn't total can repeat or skip rows across pages
    assert [s["id"] for s in db.recent_sessions()] == [second, first]
    assert [s["id"] for s in db.recent_sessions(limit=1)] == [second]
    assert [s["id"] for s in db.recent_sessions(limit=1, offset=1)] == [first]


def test_latest_open_session(db):
    assert db.latest_open_session() is None
    first = db.start_session(project="a")
    second = db.start_session(project="b")
    assert db.latest_open_session()["id"] == second
    assert db.latest_open_session(project="a")["id"] == first
    db.end_session(second, "done")
    assert db.latest_open_session()["id"] == first
