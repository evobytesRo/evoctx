"""`search_context` reports how many notes matched, not just how many it returned.

A bare list cannot say "there are 40 of these and you are looking at 10", so an
assistant had no way to tell a complete answer from the first page of one, and no
reason to ask for the rest. The count is computed from the SAME filtered sequence the
page comes from — a raw store-level COUNT would advertise notes the caller's grant
then withholds.
"""
import importlib
import os
import time

import pytest

from evoctx.grants import GrantStore
from evoctx.policy import PolicyEngine

SCOPED = """\
grants:
  - name: hide-personal
    client: "*"
    tools: [search_context, list_grants]
    scope:
      projects: ["*"]
      exclude_tags: ["#personal"]
    expires: "+1d"
"""


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_STORE", str(tmp_path))
    monkeypatch.setenv("CONTEXT_CLIENT", "pytest")
    import evoctx.server as server_mod
    importlib.reload(server_mod)
    return server_mod


def scoped_engine(db, tmp_path):
    engine = PolicyEngine(db, GrantStore(tmp_path))
    path = tmp_path / "grants.yaml"
    path.write_text(SCOPED, encoding="utf-8")
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))
    engine.grants.activate("hide-personal")
    return engine


def test_total_counts_every_match_not_just_the_page(server):
    server.db.upsert_project("p1")
    for i in range(25):
        server.write_note(f"note {i}", "shared keyword", ctx=None, project="p1")

    out = server.search_context("shared", ctx=None, limit=10)
    assert len(out["results"]) == 10
    assert out["total"] == 25
    assert out["has_more"] is True


def test_has_more_is_false_once_the_page_reaches_the_end(server):
    server.db.upsert_project("p1")
    for i in range(4):
        server.write_note(f"note {i}", "shared keyword", ctx=None, project="p1")

    out = server.search_context("shared", ctx=None, limit=10)
    assert out["total"] == 4
    assert out["has_more"] is False


def test_offset_pages_without_repeating_or_skipping(server):
    server.db.upsert_project("p1")
    for i in range(12):
        server.write_note(f"note {i}", "shared keyword", ctx=None, project="p1")

    seen = []
    for offset in (0, 5, 10):
        page = server.search_context("shared", ctx=None, limit=5, offset=offset)
        assert page["total"] == 12
        seen.extend(r["id"] for r in page["results"])

    assert len(seen) == 12
    assert len(set(seen)) == 12


def test_the_last_page_reports_no_more(server):
    server.db.upsert_project("p1")
    for i in range(7):
        server.write_note(f"note {i}", "shared keyword", ctx=None, project="p1")

    assert server.search_context("shared", ctx=None, limit=5, offset=5)["has_more"] is False


def test_an_offset_past_the_end_is_empty_not_an_error(server):
    server.db.upsert_project("p1")
    server.write_note("only one", "shared keyword", ctx=None, project="p1")

    out = server.search_context("shared", ctx=None, limit=10, offset=99)
    assert out["results"] == []
    assert out["total"] == 1
    assert out["has_more"] is False


def test_total_never_advertises_a_note_the_grant_withholds(db, tmp_path):
    """The reason the count is not a raw store COUNT. Saying "40 matched" and
    handing back 6 tells the assistant to go looking for 34 notes it will never
    be shown, and reads as a bug in paging rather than as a grant doing its job.
    """
    for i in range(20):
        db.add_note(f"private {i}", "shared keyword", tags=["#personal"])
    for i in range(6):
        db.add_note(f"visible {i}", "shared keyword")

    engine = scoped_engine(db, tmp_path)
    grant = engine.check("any", "search_context")
    out = engine.search(grant, "shared", project=None, limit=10)

    assert out["total"] == 6
    assert len(out["results"]) == 6
    assert out["has_more"] is False


def test_a_denied_project_returns_a_zero_envelope_not_a_bare_list(db, tmp_path):
    """The early-return path has to have the same shape as the normal one, or
    the caller's `out["results"]` raises instead of being empty."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    path = tmp_path / "grants.yaml"
    path.write_text(
        "grants:\n  - name: only-p1\n    client: \"*\"\n"
        "    tools: [search_context]\n    scope:\n      projects: [\"p1\"]\n"
        "    expires: \"+1d\"\n",
        encoding="utf-8",
    )
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))
    engine.grants.activate("only-p1")
    grant = engine.check("any", "search_context")

    out = engine.search(grant, "anything", project="somewhere-else", limit=10)
    # Every key the success path carries, `page_token` included — the tool
    # docstring names them without qualification, and a response that is
    # sometimes one shape and sometimes another is one a caller stops trusting.
    assert out == {"results": [], "total": 0, "offset": 0, "limit": 10,
                   "has_more": False, "page_token": None}


def test_results_are_ranked_by_relevance_not_by_date(server):
    """Stated as a test because it is the assumption a caller is most likely to
    make and it is wrong: the newest note is not at the top."""
    server.db.upsert_project("p1")
    server.write_note("webhooks", "the note that is actually about webhooks",
                      ctx=None, project="p1")
    for i in range(3):
        server.write_note(f"unrelated {i}",
                          "a long body mentioning webhooks once, in passing",
                          ctx=None, project="p1")

    out = server.search_context("webhooks", ctx=None)
    assert out["results"][0]["title"] == "webhooks", \
        "newest-first would have put an 'unrelated' note here"


# ── the scan ceiling ──────────────────────────────────────────────────────────

def test_an_exact_count_at_the_ceiling_is_not_reported_as_a_floor(db, tmp_path, monkeypatch):
    """Exactly `_MAX_SCAN` matches is an exhaustive, exact answer. Detecting
    the cap by "I fetched as many as I asked for" gets this wrong, which is why
    the query asks for one row more than it will use."""
    monkeypatch.setattr(PolicyEngine, "_MAX_SCAN", 10)
    for i in range(10):
        db.add_note(f"note {i}", "shared keyword")

    engine = PolicyEngine(db, GrantStore(tmp_path))
    out = engine.search(engine.check("any", "search_context"), "shared", None, 5)
    assert out["total"] == 10
    assert "total_is_lower_bound" not in out


def test_past_the_ceiling_the_total_is_flagged_as_a_floor(db, tmp_path, monkeypatch):
    monkeypatch.setattr(PolicyEngine, "_MAX_SCAN", 10)
    for i in range(14):
        db.add_note(f"note {i}", "shared keyword")

    engine = PolicyEngine(db, GrantStore(tmp_path))
    out = engine.search(engine.check("any", "search_context"), "shared", None, 5)
    assert out["total"] == 10
    assert out["total_is_lower_bound"] is True


def test_has_more_never_points_at_a_page_that_does_not_exist(db, tmp_path, monkeypatch):
    """`has_more` means "reachable by paging" and nothing else.

    Matches past the scan ceiling are NOT reachable — `offset` is applied to
    what the scan collected. Reporting them through `has_more` sends a caller
    obeying "if has_more, ask for the rest" round a loop that returns an empty
    page every time, for ever. `total_is_lower_bound` is the field that says
    the story does not end here, and the tool docstring answers it with
    "narrow the query".
    """
    monkeypatch.setattr(PolicyEngine, "_MAX_SCAN", 10)
    for i in range(14):
        db.add_note(f"note {i}", "shared keyword")

    engine = PolicyEngine(db, GrantStore(tmp_path))
    grant = engine.check("any", "search_context")

    at_end = engine.search(grant, "shared", None, limit=10, offset=0)
    assert len(at_end["results"]) == 10
    assert at_end["total_is_lower_bound"] is True
    assert at_end["has_more"] is False, "there is more, but not by paging"

    for offset in (10, 11, 2000):
        beyond = engine.search(grant, "shared", None, limit=10, offset=offset)
        assert beyond["results"] == []
        assert beyond["has_more"] is False, f"offset {offset} still says keep paging"


def test_a_scoped_grant_cannot_zero_out_a_note_it_can_read(db, tmp_path):
    """The ladder has to count what the CALLER can see.

    Counting raw store hits picks a rung on notes the grant hides, and then
    reports `total: 0` while the next rung held a note this caller was
    entitled to. It needs no exotic setup — a grant scoped to one project, on
    a store that has more than one.
    """
    db.upsert_project("p1")
    db.upsert_project("other")
    for i in range(6):
        db.add_note(f"alpha beta together {i}", "x", project="other")
    mine = db.add_note("alpha only", "y", project="p1")

    engine = PolicyEngine(db, GrantStore(tmp_path))
    path = tmp_path / "grants.yaml"
    path.write_text(
        "grants:\n  - name: only-p1\n    client: \"*\"\n"
        "    tools: [search_context]\n    scope:\n      projects: [\"p1\"]\n"
        "    expires: \"+1d\"\n",
        encoding="utf-8",
    )
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))
    engine.grants.activate("only-p1")

    out = engine.search(engine.check("any", "search_context"), "alpha beta",
                        None, limit=10)
    assert out["total"] == 1
    assert [r["id"] for r in out["results"]] == [mine]


# ── degenerate arguments ──────────────────────────────────────────────────────

def test_a_zero_limit_does_not_tell_the_caller_to_keep_paging(server):
    """`has_more=True` with an empty page is an instruction to loop forever."""
    server.db.upsert_project("p1")
    for i in range(5):
        server.write_note(f"note {i}", "shared keyword", ctx=None, project="p1")

    out = server.search_context("shared", ctx=None, limit=0)
    assert out["results"] == []
    assert out["total"] == 5
    assert out["has_more"] is False


def test_negative_limit_and_offset_are_clamped_not_echoed(server):
    server.db.upsert_project("p1")
    for i in range(5):
        server.write_note(f"note {i}", "shared keyword", ctx=None, project="p1")

    out = server.search_context("shared", ctx=None, limit=-5, offset=-7)
    assert out["offset"] == 0 and out["limit"] == 0
    assert out["has_more"] is False


def test_the_denied_envelope_clamps_the_same_way_the_success_path_does(db, tmp_path):
    """Two shapes for one field is how a caller learns to trust neither."""
    engine = PolicyEngine(db, GrantStore(tmp_path))
    path = tmp_path / "grants.yaml"
    path.write_text(
        "grants:\n  - name: only-p1\n    client: \"*\"\n"
        "    tools: [search_context]\n    scope:\n      projects: [\"p1\"]\n"
        "    expires: \"+1d\"\n",
        encoding="utf-8",
    )
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))
    engine.grants.activate("only-p1")
    grant = engine.check("any", "search_context")

    out = engine.search(grant, "anything", project="elsewhere", limit=-1, offset=-7)
    assert out["offset"] == 0 and out["limit"] == 0


# ── one rung per query, not one per page ──────────────────────────────────────

def test_a_search_reads_the_store_once_when_the_first_rung_suffices(db, tmp_path,
                                                                    monkeypatch):
    """One page is one snapshot.

    The implementation this replaced paged the store ~20 times to build the
    total, which is how a single concurrent insert put a note in the response
    twice. Asserted at the PolicyEngine layer, since that is where the ladder
    is now walked.
    """
    for i in range(40):
        db.add_note(f"note {i}", "shared keyword")

    calls = []
    real = db.search
    monkeypatch.setattr(db, "search",
                        lambda *a, **kw: (calls.append(kw.get("rung")), real(*a, **kw))[1])

    engine = PolicyEngine(db, GrantStore(tmp_path))
    engine.search(engine.check("any", "search_context"), "shared", None, limit=5)
    assert len(calls) == 1, f"the store was read {len(calls)} times for one page"


def test_the_ladder_reads_each_rung_at_most_once(db, tmp_path, monkeypatch):
    """Widening costs at most one read per rung, never one per page."""
    db.add_note("alpha note", "alpha body")
    db.add_note("beta note", "beta body")

    calls = []
    real = db.search
    monkeypatch.setattr(db, "search",
                        lambda *a, **kw: (calls.append(kw.get("rung")), real(*a, **kw))[1])

    engine = PolicyEngine(db, GrantStore(tmp_path))
    out = engine.search(engine.check("any", "search_context"), "alpha beta",
                        None, limit=10)
    assert out["total"] == 2, "the ladder did not widen"
    assert len(calls) == len(set(calls)) <= 3


def test_one_response_never_contains_the_same_note_twice(db, tmp_path):
    """Paging the store to build the total took ~20 snapshots, so a single
    concurrent insert shifted every later row and put one note in the response
    twice.

    The property is now STRUCTURAL: one rung's rows are read once and returned,
    so a duplicate is impossible under any single-pass implementation and no
    fixture can make this fail. Kept as a statement of the invariant, with a
    concurrent writer attached to keep it honest about what it exercises —
    which is not "the write landed between two reads", because there is only
    one read.
    """
    for i in range(250):
        db.add_note(f"note {i}", "shared keyword")

    engine = PolicyEngine(db, GrantStore(tmp_path))
    real = db.search
    writes = [0]

    def writing_search(*a, **kw):
        rows = real(*a, **kw)
        if writes[0] < 5:                      # another window, mid-read
            writes[0] += 1
            db.add_note(f"late arrival {writes[0]}", "shared keyword")
        return rows

    db.search = writing_search                 # type: ignore[method-assign]
    try:
        out = engine.search(engine.check("any", "search_context"), "shared",
                            None, limit=300)
    finally:
        db.search = real                       # type: ignore[method-assign]

    ids = [r["id"] for r in out["results"]]
    assert len(ids) == len(set(ids)), "a note appeared twice in one response"
    assert out["total"] == len(ids)


# ── pages of one result set agree with each other ─────────────────────────────

def test_a_page_token_stops_page_two_narrowing_under_the_caller(db, tmp_path):
    """Each call re-walks the ladder, so notes written between page 1 and page
    2 can push a narrower rung over the threshold — and page 2 then comes from
    a strictly SMALLER set, silently dropping notes page 1 had shown.

    The token pins the interpretation, not the corpus. Notes written in between
    still appear, and a note DELETED in between still goes — nothing here is a
    snapshot, and paging is positional, so a paged walk of a store being
    written to samples the matches rather than visiting every one. What the
    token removes is the one drift the caller cannot see coming: the query
    being re-read more narrowly, so page two is a slice of a smaller set.
    Asserted below under inserts only, which is the shape it bounds.
    """
    alpha_only = db.add_note("alpha note", "alpha body")
    db.add_note("beta note", "beta body")

    engine = PolicyEngine(db, GrantStore(tmp_path))
    grant = engine.check("any", "search_context")

    page1 = engine.search(grant, "alpha beta", None, limit=1, offset=0)
    assert page1["total"] == 2
    token = page1["page_token"]

    # another window writes six notes carrying BOTH terms
    for i in range(6):
        db.add_note(f"alpha beta together {i}", "both terms")

    without = engine.search(grant, "alpha beta", None, limit=50, offset=0)
    assert without["total"] == 6, "the fixture no longer changes the rung"
    assert alpha_only not in {r["id"] for r in without["results"]}, \
        "re-walking dropped a note page 1 had already shown"

    with_token = engine.search(grant, "alpha beta", None, limit=50, offset=0,
                               page_token=token)
    assert with_token["page_token"] == token
    assert alpha_only in {r["id"] for r in with_token["results"]}
    # Under inserts only. A deletion still removes a note, token or not.
    assert with_token["total"] >= page1["total"]


def test_a_page_token_from_another_query_is_ignored(db, tmp_path):
    """The token is an FTS expression. Accepting one this query's own ladder
    did not produce would let a caller hand the store an arbitrary MATCH."""
    mine = db.add_note("alpha note", "alpha body")
    db.add_note("zeta note", "zeta body")

    engine = PolicyEngine(db, GrantStore(tmp_path))
    grant = engine.check("any", "search_context")

    out = engine.search(grant, "alpha", None, limit=10, page_token="zeta")
    assert [r["id"] for r in out["results"]] == [mine]
    assert out["page_token"] in db.widening_ladder("alpha")


@pytest.mark.parametrize("token", ["", "  ", "*", '"unclosed', "(alpha", "NOT"])
def test_a_malformed_page_token_cannot_reach_the_store(db, tmp_path, token):
    """Asserted on the token that was USED, not on the results.

    Every one of these tokens, unguarded, reaches MATCH, gets rejected, and
    lands in the LIKE fallback — which for this fixture returns the same one
    note. So a results assertion is green either way and proves nothing, while
    the behaviour it fails to catch is a caller-triggered substring search:
    exactly what three documents say cannot happen.
    """
    db.add_note("alpha note", "alpha body")
    engine = PolicyEngine(db, GrantStore(tmp_path))
    out = engine.search(engine.check("any", "search_context"), "alpha", None,
                        limit=10, page_token=token)
    assert out["page_token"] in db.widening_ladder("alpha"), \
        f"{token!r} was passed through to the store"


def test_a_page_token_cannot_smuggle_a_substring_search(db, tmp_path):
    """The planted offender for the guard above: a note reachable ONLY by the
    LIKE fallback's substring matching. If a token ever reaches MATCH unguarded,
    this note appears in a search that should not find it."""
    db.add_note("alpha note", "alpha body")
    hidden = db.add_note("Newsletter", "unrelated body")

    engine = PolicyEngine(db, GrantStore(tmp_path))
    out = engine.search(engine.check("any", "search_context"), "letter", None,
                        limit=10, page_token='"unclosed')
    assert hidden not in {r["id"] for r in out["results"]}
    assert out["results"] == []


# ── the capped short-circuit must not stop on rows the caller cannot see ──────

def test_a_capped_rung_of_hidden_notes_does_not_stop_the_ladder(db, tmp_path, monkeypatch):
    """`capped` counts RAW rows. Stopping the walk on it alone reintroduces
    the pre-grant-count defect through the other branch: a rung whose every
    match is hidden ends the search, and the caller is told there is nothing
    while a wider rung has not been tried at all.

    Asserted as "the walk continued", not as "the note was found". The honest
    limit is that a visible note ranked below `_MAX_SCAN` hidden ones is out
    of reach whatever the ladder does — the scan stops there — and a test that
    demanded otherwise would be asserting a property this design does not have.
    """
    monkeypatch.setattr(PolicyEngine, "_MAX_SCAN", 10)
    db.upsert_project("p1")
    db.upsert_project("other")
    for i in range(12):
        db.add_note(f"alpha beta together {i}", "x", project="other")
    db.add_note("alpha only", "y", project="p1")

    engine = PolicyEngine(db, GrantStore(tmp_path))
    path = tmp_path / "grants.yaml"
    path.write_text(
        "grants:\n  - name: only-p1\n    client: \"*\"\n"
        "    tools: [search_context]\n    scope:\n      projects: [\"p1\"]\n"
        "    expires: \"+1d\"\n",
        encoding="utf-8",
    )
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))
    engine.grants.activate("only-p1")

    tried = []
    real = db.search
    monkeypatch.setattr(db, "search",
                        lambda *a, **kw: (tried.append(kw.get("rung")), real(*a, **kw))[1])

    engine.search(engine.check("any", "search_context"), "alpha beta", None, limit=10)
    assert tried == db.widening_ladder("alpha beta"), \
        "the walk stopped on a capped rung whose every hit was hidden"


def test_a_later_capped_rung_does_not_mark_an_earlier_exact_answer_as_a_floor(
        db, tmp_path, monkeypatch):
    """`total_is_lower_bound` has to describe the rung actually returned.

    Rung 2 finds an exact, complete, uncapped answer; rung 3 is then tried,
    hits the ceiling, and finds nothing visible. Without the guard its `capped`
    leaks onto rung 2's result and an exhaustive count reports itself as a
    floor — the same "flag describes something the caller was not given" the
    ceiling work exists to remove.
    """
    monkeypatch.setattr(PolicyEngine, "_MAX_SCAN", 5)
    db.upsert_project("p1")
    db.upsert_project("other")
    # rung 2 (`alpha OR zeta`) matches only this note — `alphabet` is a
    # different term — and 1 row is under the ceiling, so it is exact.
    # The term sits in the BODY of a long note so that under rung 3 this ranks
    # below the title matches and falls outside the ceiling entirely.
    db.add_note("note", "alpha appears here " + "filler words " * 40, project="p1")
    # rung 3 (`alpha* OR zeta*`) matches these too, all hidden, over the cap,
    # and all ranked above the note above because the term is in their titles.
    for i in range(9):
        db.add_note(f"alphabet entry {i}", "x", project="other")

    engine = PolicyEngine(db, GrantStore(tmp_path))
    path = tmp_path / "grants.yaml"
    path.write_text(
        "grants:\n  - name: only-p1\n    client: \"*\"\n"
        "    tools: [search_context]\n    scope:\n      projects: [\"p1\"]\n"
        "    expires: \"+1d\"\n",
        encoding="utf-8",
    )
    mtime = time.time() + 2
    os.utime(path, (mtime, mtime))
    engine.grants.activate("only-p1")

    out = engine.search(engine.check("any", "search_context"), "alpha zeta",
                        None, limit=10)
    assert out["total"] == 1
    assert "total_is_lower_bound" not in out, \
        "a later rung's ceiling was reported against an earlier exact answer"


def test_a_capped_rung_with_something_visible_does_stop_the_ladder(db, tmp_path, monkeypatch):
    """The other half: once a rung has both hit the ceiling and produced
    something the caller can see, there is no reason to try a wider one."""
    monkeypatch.setattr(PolicyEngine, "_MAX_SCAN", 10)
    for i in range(12):
        db.add_note(f"alpha beta together {i}", "x")

    engine = PolicyEngine(db, GrantStore(tmp_path))
    tried = []
    real = db.search
    monkeypatch.setattr(db, "search",
                        lambda *a, **kw: (tried.append(kw.get("rung")), real(*a, **kw))[1])

    out = engine.search(engine.check("any", "search_context"), "alpha beta",
                        None, limit=10)
    assert tried == ["alpha AND beta"]
    assert out["total_is_lower_bound"] is True


def test_the_audit_line_records_what_was_returned_not_what_matched(db, tmp_path):
    """`result_count` answers "what did this client read". Logging `total`
    there would make the record claim more was read than was."""
    from evoctx.audit import AuditWriter

    for i in range(25):
        db.add_note(f"note {i}", "shared keyword")
    engine = PolicyEngine(db, GrantStore(tmp_path),
                          audit=AuditWriter(db.conn, "test-connection"))
    engine.t_search_context("pytest", "shared", None, 10)

    count = db.conn.execute(
        "SELECT result_count FROM audit_log WHERE tool = 'search_context'"
    ).fetchone()[0]
    assert count == 10
