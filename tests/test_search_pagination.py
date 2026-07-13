import time

from evoctx.grants import GrantStore
from evoctx.policy import PolicyEngine

SCOPED = """\
grants:
  - name: heavy-exclude
    client: "*"
    tools: [search_context, recent_sessions, list_grants]
    scope:
      projects: ["*"]
      exclude_tags: ["#personal"]
    expires: "+1d"
"""


def scoped_engine(db, tmp_path):
    engine = PolicyEngine(db, GrantStore(tmp_path))
    path = tmp_path / "grants.yaml"
    path.write_text(SCOPED, encoding="utf-8")
    mtime = time.time() + 2
    import os
    os.utime(path, (mtime, mtime))
    engine.grants.activate("heavy-exclude")
    return engine


def test_heavy_exclusion_still_fills_requested_limit(db, tmp_path):
    # 90% of matching notes are excluded — old fixed limit*3 over-fetch
    # would silently return fewer than `limit` even though enough visible
    # notes exist further down the result set.
    for i in range(27):
        db.add_note(f"note {i}", "shared keyword", tags=["#personal"])
    for i in range(5):
        db.add_note(f"visible {i}", "shared keyword")

    engine = scoped_engine(db, tmp_path)
    grant = engine.check("any", "search_context")
    results = engine.search(grant, "shared", project=None, limit=5)
    assert len(results) == 5
    assert all("#personal" not in r["tags"] for r in results)


def test_exhausted_store_returns_fewer_than_limit_without_hanging(db, tmp_path):
    for i in range(3):
        db.add_note(f"visible {i}", "shared keyword")
    for i in range(10):
        db.add_note(f"hidden {i}", "shared keyword", tags=["#personal"])

    engine = scoped_engine(db, tmp_path)
    grant = engine.check("any", "search_context")
    results = engine.search(grant, "shared", project=None, limit=20)
    assert len(results) == 3  # only 3 visible notes exist at all


def test_recent_sessions_heavy_exclusion_fills_limit(db, tmp_path):
    for i in range(15):
        db.start_session(project="private-project")
    for i in range(5):
        db.start_session(project="public-project")

    engine = scoped_engine(db, tmp_path)
    grant = engine.check("any", "recent_sessions")
    # grant scope is projects: ["*"] with only tag exclusion, so use a
    # project-scoped grant instead to test the same page-until-filled path
    grant.projects = ["public-project"]
    results = engine.recent_sessions(grant, project=None, limit=5)
    assert len(results) == 5
    assert all(s["project"] == "public-project" for s in results)


def test_db_search_offset_paginates(db):
    for i in range(5):
        db.add_note(f"note {i}", "shared keyword")
    page1 = db.search("shared", limit=2, offset=0)
    page2 = db.search("shared", limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})


def test_db_recent_sessions_offset_paginates(db):
    ids = [db.start_session(project="p1") for _ in range(5)]
    page1 = db.recent_sessions(project="p1", limit=2, offset=0)
    page2 = db.recent_sessions(project="p1", limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {s["id"] for s in page1}.isdisjoint({s["id"] for s in page2})
