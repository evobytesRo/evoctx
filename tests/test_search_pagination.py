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


def test_paging_tied_ranks_covers_every_note_exactly_once(db):
    """Identical notes score identical bm25 rank, and the ordering has to stay
    total anyway.

    `ORDER BY rank` alone is not a total order, and paging a non-total order
    with LIMIT/OFFSET is free to repeat a row on one page and skip another —
    which for a memory store means a note that exists and is never returned.
    Ten notes with the same title and body guarantee the tie.
    """
    ids = [db.add_note("same title", "identical body text") for _ in range(10)]

    seen: list[str] = []
    for offset in range(0, 10, 2):
        page = db.search("identical", limit=2, offset=offset)
        seen.extend(r["id"] for r in page)

    assert len(seen) == 10, "paging dropped or short-changed a page"
    assert len(set(seen)) == 10, "paging returned the same note on two pages"
    # newest-first is the contract, and asserting it is what makes this test
    # discriminate: `ORDER BY rank` alone returned ties in scan order, i.e.
    # OLDEST first. Set equality alone would pass against the old query too,
    # because SQLite's scan order happens to be stable here — the defect was
    # that it is unspecified, not that it was observably broken.
    assert seen == ids[::-1], "tied results must come back newest-first"


def test_like_fallback_paging_is_ordered(db):
    """The LIKE fallback fires when the FTS query is malformed — an unbalanced
    quote is enough. It had no ORDER BY at all, and paging an unordered query
    is undefined rather than merely unstable."""
    # the body carries the quote too: the fallback re-derives its LIKE pattern
    # from the raw query, so the quote has to be matchable literally or the
    # fallback runs and legitimately finds nothing, testing no ordering at all
    ids = [db.add_note("same title", 'he said "identical here') for _ in range(6)]

    seen: list[str] = []
    for offset in range(0, 6, 2):
        # the unbalanced quote is what forces sqlite3.OperationalError on MATCH
        page = db.search('"identical', limit=2, offset=offset)
        seen.extend(r["id"] for r in page)

    assert len(set(seen)) == 6
    assert seen == ids[::-1], "the fallback must page newest-first, not in scan order"


def test_db_recent_sessions_offset_paginates(db):
    ids = [db.start_session(project="p1") for _ in range(5)]
    page1 = db.recent_sessions(project="p1", limit=2, offset=0)
    page2 = db.recent_sessions(project="p1", limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {s["id"] for s in page1}.isdisjoint({s["id"] for s in page2})
