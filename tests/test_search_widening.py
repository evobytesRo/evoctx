"""Search recall — 0.1.5's tag indexing and the AND -> OR -> prefix ladder.

Measured on a real 378-note store before any of this was written: a natural
multi-word question returned nothing at all, because FTS5's operator between
two bare words is AND. `ANAF efactura submission status polling` found 0 notes
while `ANAF OR efactura OR polling` found 5, and `gotcha` appeared in 51 notes'
tags against 39 notes' text — twelve notes tagged for a topic and unreachable
by searching for it. Both are covered here.
"""
import sqlite3
import sys

import pytest

from evoctx.db import Database


# ── tags are in the index ─────────────────────────────────────────────────────

def test_a_word_that_appears_only_in_tags_is_findable(db):
    """The 0.1.4 index held title and content, so tagging did nothing for
    retrieval — the one act of curation the store asks for."""
    nid = db.add_note("unrelated title", "unrelated body", tags=["#kubernetes"])
    assert [h["id"] for h in db.search("kubernetes")] == [nid]


def test_a_tag_is_findable_with_or_without_the_hash(db):
    """unicode61 treats `#` as a separator, so both forms tokenize the same.
    Asserted rather than assumed: users type the form they wrote in the tag."""
    nid = db.add_note("t", "b", tags=["#gotcha"])
    assert [h["id"] for h in db.search("gotcha")] == [nid]
    assert [h["id"] for h in db.search("#gotcha")] == [nid]


def test_hyphenated_tags_split_into_their_words(db):
    nid = db.add_note("t", "b", tags=["#bank-reconciliation"])
    assert [h["id"] for h in db.search("reconciliation")] == [nid]
    assert [h["id"] for h in db.search("bank")] == [nid]


def test_editing_a_note_does_not_desync_the_tag_index(db):
    """The update trigger replays the OLD tuple as a 'delete' before inserting
    the new one. A trigger still writing the two-column tuple against a
    three-column table leaves the old tags in the index forever — the note
    stays findable under a tag it no longer has, silently."""
    nid = db.add_note("t", "b", tags=["#before"])
    db.conn.execute("UPDATE notes SET tags = ? WHERE id = ?", ('["#after"]', nid))
    db.conn.commit()

    assert [h["id"] for h in db.search("after")] == [nid]
    assert db.search("before") == [], "the superseded tag is still indexed"


def test_deleting_a_note_removes_its_tags_from_the_index(db):
    nid = db.add_note("t", "b", tags=["#doomed"])
    db.delete_note(nid)
    assert db.search("doomed") == []
    # Not just "the row stopped coming back": an external-content table whose
    # delete replayed the wrong tuple stays queryable while being wrong, and
    # only the integrity command says so. It raises DatabaseError if the index
    # and the notes table disagree.
    db.conn.execute("INSERT INTO notes_fts(notes_fts) VALUES ('integrity-check')")


# ── the widening ladder ───────────────────────────────────────────────────────

def test_ladder_shape_for_a_plain_multi_word_query():
    rungs = Database.widening_ladder("bank statements")
    assert rungs == [
        "bank AND statements",
        "bank OR statements",
        "bank* OR statements*",
    ]


def test_ladder_keeps_synonym_expansion_on_both_boolean_rungs():
    rungs = Database.widening_ladder("auth timeout")
    assert rungs[0] == ("(auth OR authentication OR login OR signin) AND "
                        "(timeout OR hang OR stuck)")
    assert rungs[1] == ("(auth OR authentication OR login OR signin) OR "
                        "(timeout OR hang OR stuck)")
    # not `(auth OR ...)*` — the star would bind to the paren, not to each
    # alternative, which is not the query it looks like
    assert rungs[2] == "auth* OR timeout*"


def test_ladder_refuses_to_touch_a_deliberate_query():
    """Someone who typed quotes, an operator or a star asked a precise
    question. Widening it silently answers a different one."""
    for q in ('"exact phrase"', "auth NOT bug", "aut*", "(a OR b) c", "-nope"):
        assert Database.widening_ladder(q) == [q]


def test_single_word_ladder_has_no_redundant_or_rung():
    assert Database.widening_ladder("newsletter") == ["newsletter", "newsletter*"]


# The shapes below are enumerated from FTS5's grammar — "what is a bareword",
# "what is an operator" — and not from what `widening_ladder` happens to do.
# The first version of this predicate was `[A-Za-z]+`, which passes every test
# an author writes while thinking about English words, and silently refused to
# widen `mt940`, `0019`, `sha256` and every Romanian word with a diacritic:
# the majority of what actually gets typed at this store.
@pytest.mark.parametrize("query", [
    "mt940 import",          # digits — an FTS5 bareword
    "0019 newsletter",       # leading digits
    "sha256 dedupe",
    "tranzacții bancă",      # non-ASCII, i.e. >= U+0080
    "élève dossier",
    "test_mt940 parser",     # `_` — see below
    "bank_statements fx",
])
def test_barewords_with_digits_diacritics_or_underscores_get_the_ladder(query):
    rungs = Database.widening_ladder(query)
    assert len(rungs) == 3, f"{query!r} was treated as deliberate syntax"
    assert " OR " in rungs[1] and rungs[2].endswith("*")


@pytest.mark.parametrize("query,found", [
    ("test_mt940", True),
    ("snake_case_name", True),
    ("test_mt940*", True),
    ("bank_statements OR zzz", True),
    # the phrase is ADJACENT, which is what "one bareword, tokenized on _"
    # actually means — these words are in the note but not next to each other
    ("mt940_bank", False),
])
def test_an_underscored_query_behaves_as_an_adjacent_phrase(db, query, found):
    """`_` was first excluded on the reasoning that FTS5 would not parse
    `test_mt940` as one bareword. It parses. What happens is a level below the
    parser: the parser takes it as one bareword and unicode61 splits it on `_`,
    so it matches as the phrase "test mt940".

    Asserted through `db.search`, not through a bare `conn.execute`, so this
    fails if evoctx stops sending such a query to the index — the earlier
    version of this test only proved something about SQLite.
    """
    nid = db.add_note("fixtures", "handles test_mt940 and snake_case_name and bank_statements")
    hits = [h["id"] for h in db.search(query)]
    assert hits == ([nid] if found else []), f"{query!r} matched {hits}"


@pytest.mark.parametrize("query", [
    "a-b c",                 # `-` is NOT, i.e. negation
    "col:value",             # column filter
    "a^b",                   # column-index operator
    "a,b",
    "#",                     # a token that is nothing but a hash
])
def test_tokens_fts5_would_not_read_as_one_bareword_are_left_alone(query):
    assert Database.widening_ladder(query) == [query]


@pytest.mark.parametrize("query", [
    "“alpha zeta”",    # “ ” — what every editor substitutes
    "‘alpha zeta’",    # ‘ ’
    "«abc zeta»",      # « »
    "‹alpha zeta›",    # ‹ › — the list version missed these
    "「alpha zeta」",    # 「 」
    "＂alpha zeta＂",    # ＂ fullwidth
    "„alpha zeta“",    # „ “
])
def test_typographic_quotes_turn_the_widening_off_like_straight_ones(query):
    """Quotes above U+0080 pass the bareword rule, so a quoted phrase would
    have been OR-widened while three documents promise that quoting turns
    widening off. FTS5 will not honour them as quotes either, but somebody
    pasting a quoted phrase means the phrase, and answering a wider question
    is the wrong way to fail.

    Enumerated well past the four forms an English keyboard produces: the
    first attempt at this was a list of six characters, and the shapes it
    missed are all in here.
    """
    assert Database.widening_ladder(query) == [query]


@pytest.mark.parametrize("query", [
    "don’t work",            # U+2019 as an apostrophe — Pf, but a letter
    "l’élève dossier",
    "ne dis pas n’importe quoi",
])
def test_a_smart_apostrophe_inside_a_word_does_not_turn_the_ladder_off(query):
    """A flat category test made every smartened apostrophe read as quoting,
    which silently narrows search for anyone whose editor substitutes them —
    ordinary English and French. Punctuation BETWEEN two letters is part of
    the word."""
    assert len(Database.widening_ladder(query)) == 3


@pytest.mark.parametrize("query", ["rock ’n’ roll", "’quoted’ word"])
def test_an_apostrophe_at_a_word_edge_still_reads_as_quoting(query):
    """The other side of the same rule, and the reason it is about position
    rather than about the character: at the edge of a token, a curly
    apostrophe is indistinguishable from a curly single quote, and honouring
    it is the safe reading."""
    assert Database.widening_ladder(query) == [query]


def test_underscores_survive_the_punctuation_rule(db):
    """`_` is Unicode category `Pc`. The punctuation rule is scoped to
    U+0080 and above precisely so it does not undo underscore support."""
    nid = db.add_note("fixtures", "handles test_mt940 here")
    assert len(Database.widening_ladder("test_mt940 parser")) == 3
    assert [h["id"] for h in db.search("test_mt940")] == [nid]


# ── tag queries reach the index, not the LIKE error path ──────────────────────

def test_a_hash_prefixed_tag_query_goes_through_the_index(db):
    """`#` is not FTS5 syntax, it is a character FTS5 refuses to parse. Left
    alone, `#gotcha` raised a syntax error and landed in the LIKE fallback,
    where it appeared to work while doing something else entirely.

    Stemming is the discriminator, and it is the one the LIKE path cannot
    fake: `LIKE '%#gotchas%'` does not appear anywhere in a note tagged
    `#gotcha`, so a hit here proves the query reached the index.
    """
    nid = db.add_note("t", "b", tags=["#gotcha"])
    assert [h["id"] for h in db.search("#gotchas")] == [nid]
    assert [h["id"] for h in db.search("#gotcha")] == [nid]


def test_two_tags_in_one_query_still_work(db):
    """The LIKE fallback searched for the whole string `#gotcha #session` inside
    one note and so returned nothing, always."""
    both = db.add_note("t", "b", tags=["#gotcha", "#session"])
    db.add_note("one tag only", "b", tags=["#gotcha"])
    assert both in {h["id"] for h in db.search("#gotcha #session")}


def test_the_hash_form_and_the_bare_form_agree(db):
    db.add_note("t", "b", tags=["#reconciliation"])
    db.add_note("t2", "b2", tags=["#reconciliation"])
    assert ([h["id"] for h in db.search("#reconciliation")]
            == [h["id"] for h in db.search("reconciliation")])


@pytest.mark.parametrize("query", ["", "   ", "\t\n"])
def test_an_empty_query_is_passed_through_unchanged(query):
    """No tokens means nothing to widen. Passed through so the behaviour is
    whatever it was before this release, rather than a new one invented here."""
    assert Database.widening_ladder(query) == [query]


def test_the_all_terms_note_ranks_first_even_once_the_ladder_widens(db):
    """Rung 1 owns the TOP of the result, not the whole of it.

    Measured against the real store, requiring only one AND hit to stop the
    ladder was worse than no ladder for the query that motivated the feature:
    one incidental note carrying all five words hid the four notes that
    actually answered the question. So precision lives in the ordering, and
    recall in the set.
    """
    both = db.add_note("migration and reconciliation", "x")
    for i in range(6):
        db.add_note(f"reconciliation only {i}", "y")

    hits = db.search("migration reconciliation")
    assert hits[0]["id"] == both
    assert len(hits) > 1, "one AND hit suppressed everything the OR rung finds"


def test_a_rung_that_already_fills_a_page_is_not_widened(db):
    """The threshold is not "any hit", but it is also not "always widen" —
    a query with plenty of precise answers must stay precise."""
    both = [db.add_note(f"alpha beta {i}", "x") for i in range(6)]
    db.add_note("alpha only", "y")
    db.add_note("beta only", "z")

    assert {h["id"] for h in db.search("alpha beta", limit=20)} == set(both)


def test_the_fullest_rung_wins_when_none_reaches_the_threshold(db):
    """Rung 3 drops synonym expansion, so it is NOT a superset of rung 2 and
    regularly finds fewer. Falling through to "the widest rung with any hits"
    would therefore lose notes rung 2 had already found.

    Here rung 1 ANDs to nothing, rung 2 finds both notes through the
    auth/login/signin group, and rung 3 (`login* OR zzz*`) finds neither —
    so the ladder has to end up back on rung 2, not on the last one it tried.
    """
    a = db.add_note("authentication failure", "x")
    b = db.add_note("signin broken", "y")

    hits = {h["id"] for h in db.search("login zzz")}
    assert hits == {a, b}


def test_or_rung_rescues_a_query_that_ands_to_nothing(db):
    """The measured failure: five plausible words, no note carrying all five."""
    a = db.add_note("ANAF upload flow", "efactura submit")
    b = db.add_note("polling the status endpoint", "unrelated body")

    assert db.search("ANAF efactura submission status polling") != []
    assert {h["id"] for h in db.search("ANAF efactura submission status polling")} == {a, b}


def test_or_rung_still_ranks_the_fullest_match_first(db):
    """Widening changes what is reachable, not what ranks first — otherwise
    the ladder would trade recall for precision instead of adding recall."""
    db.add_note("only alpha here", "alpha")
    best = db.add_note("alpha and beta and gamma", "alpha beta gamma")
    db.add_note("only gamma here", "gamma")

    hits = db.search("alpha beta gamma delta")   # delta appears nowhere
    assert hits[0]["id"] == best


def test_prefix_rung_catches_a_half_remembered_word(db):
    nid = db.add_note("Newsletter subscription shipped", "double opt-in")
    assert db.search("newslet") != [], "the prefix rung never fired"
    assert [h["id"] for h in db.search("newslet")] == [nid]


def test_no_rung_can_match_inside_a_word(db):
    """FTS5 has no infix operator. Stated as a test so the limit is a
    documented property rather than a surprise — the README promises it."""
    db.add_note("Newsletter subscription", "body")
    assert db.search("letter") == []


def test_a_query_matching_nothing_stays_empty(db):
    db.add_note("something", "else")
    assert db.search("kumquat zeppelin") == []


# ── the rung must not change between pages ────────────────────────────────────

def test_db_search_honours_a_pinned_rung_instead_of_re_choosing(db):
    """The store-level half of rung pinning: given a rung, `db.search` uses it
    and does not consult `choose_rung`.

    This is what makes a page token possible; the policy engine builds on it
    and `tests/test_search_totals.py` covers the paging behaviour end to end.
    Deliberately NOT claiming to test PolicyEngine — an earlier version of this
    test said "ONCE, as PolicyEngine does" while PolicyEngine did no such
    thing, which is a green test certifying a false claim.
    """
    alpha_only = db.add_note("alpha note", "alpha body")
    db.add_note("beta note", "beta body")

    rung = db.choose_rung("alpha beta")
    assert rung == "alpha OR beta"

    # another window writes six notes carrying BOTH terms, which pushes rung 1
    # over the threshold — so a re-chosen rung would now be the AND one
    for i in range(6):
        db.add_note(f"alpha beta together {i}", "both terms")
    assert db.choose_rung("alpha beta") == "alpha AND beta", \
        "the fixture no longer changes the rung — this test would prove nothing"

    later_page = [h["id"] for h in db.search("alpha beta", limit=20, rung=rung)]

    # The pinned rung is still the OR one, so the alpha-only note is still
    # reachable. Re-choosing per page would have switched to AND and dropped
    # it — the same note present on page 1 and absent from page 2.
    assert alpha_only in later_page
    assert db.search("alpha beta", limit=20) != later_page, \
        "an unpinned call returns the same thing — the pin is not doing anything"


def test_project_scope_is_counted_when_choosing_a_rung(db):
    """A rung matching notes in projects the caller did not ask about has, for
    this caller, matched nothing — so it must not stop the ladder."""
    for i in range(6):
        db.add_note(f"alpha beta together {i}", "x", project="other")
    mine = db.add_note("alpha only", "y", project="mine")

    # unscoped, rung 1 clears the threshold on the 'other' notes and wins
    assert db.choose_rung("alpha beta") == "alpha AND beta"
    # scoped to 'mine', rung 1 counts zero, so the ladder has to widen
    assert db.choose_rung("alpha beta", project="mine") != "alpha AND beta"
    assert [h["id"] for h in db.search("alpha beta", project="mine")] == [mine]


# ── ranking ───────────────────────────────────────────────────────────────────

def _order_under(db, query, weights):
    """The same query ordered by an arbitrary bm25 weighting, for comparison."""
    sql = (f"SELECT n.id FROM notes_fts JOIN notes n ON n.rowid = notes_fts.rowid "
           f"WHERE notes_fts MATCH ? "
           f"ORDER BY bm25(notes_fts, {weights[0]}, {weights[1]}, {weights[2]}), "
           f"n.rowid DESC")
    return [r[0] for r in db.conn.execute(sql, (query,))]


def test_a_title_match_outranks_a_body_match(db):
    """Built so that FLAT weights get it wrong.

    The first version of this test passed under (10,1,4), under (1,1,1) and
    under 0.1.4's bare `ORDER BY rank` alike — so the weights could have been
    deleted with the suite still green while three documents promised they
    were doing something. bm25 already favours the shorter document, so the
    fixture has to fight that: the body match is SHORT and the title match is
    LONG, and only the title weight can overcome the length normalisation.
    """
    short_body = db.add_note("unrelated", "webhooks")
    long_title = db.add_note(
        "webhooks",
        "a deliberately long body about many other things entirely, with enough "
        "words in it that length normalisation would otherwise sink this note "
        "below the short one above, covering deployment and invoices and "
        "reconciliation and several other unrelated subjects at some length",
    )

    assert db.search("webhooks")[0]["id"] == long_title
    assert _order_under(db, "webhooks", (1.0, 1.0, 1.0))[0] == short_body, \
        "flat weights rank this the same way — the fixture proves nothing"


def test_a_tag_match_outranks_an_incidental_body_mention(db):
    """Same requirement — flat weights must get this one wrong — reached a
    different way, because the tag weight is 4 rather than the title's 10 and
    cannot outrun a large length difference.

    So the two notes are the same length and differ only in WHERE the term is,
    and the tagged one is written FIRST. Under flat weights the two score
    identically, the `rowid DESC` tiebreaker puts the later note on top, and
    the assertion below fails — which is what makes it a test of the weights.
    """
    tagged = db.add_note("unrelated", "a body about invoices and imports",
                         tags=["#latency"])
    body_mention = db.add_note("unrelated too", "a body about latency and imports")

    assert db.search("latency")[0]["id"] == tagged
    assert _order_under(db, "latency", (1.0, 1.0, 1.0))[0] == body_mention, \
        "flat weights rank this the same way — the fixture proves nothing"


# ── the LIKE fallback is still an error path ──────────────────────────────────

def test_a_broken_index_raises_instead_of_silently_matching_substrings(db):
    """The LIKE fallback exists for ONE reason: FTS5 refused to parse a query.

    `sqlite3.OperationalError` also covers "database is locked" and "no such
    table: notes_fts". Letting those reach the fallback turns an ordinary
    search into a substring search over a broken store — plausible-looking
    rows, no error, and three shipped documents promising that substring
    matching is impossible.

    The offender is planted rather than mocked: the FTS table is actually
    dropped, so the error travels the real path through `choose_rung`'s
    counting query. A stubbed `choose_rung` raises before reaching the `except`
    that has to re-raise it, and proves nothing about that branch.
    """
    db.add_note("Newsletter subscription", "body")
    assert db.search("letter") == []          # the promise, on a healthy store

    db.conn.executescript(
        "DROP TRIGGER notes_ai; DROP TRIGGER notes_ad; DROP TRIGGER notes_au;"
        "DROP TABLE notes_fts;"
    )
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db.search("letter")


def test_a_transient_failure_is_retried_rather_than_read_as_a_bad_query(db):
    """The failure mode a health probe ALONE cannot catch.

    A probe is a second observation taken after the fact. `SQLITE_BUSY` under
    WAL surfaces only once `busy_timeout` has already elapsed, and a Windows
    `SQLITE_IOERR` is routinely an antivirus or a syncing folder — both are
    likely gone by the time the probe runs, so the probe says "store fine",
    the failure is blamed on the query, and `letter` comes back matching
    `Newsletter` through the LIKE path. That is the one thing this release
    promises cannot happen.
    """
    db.add_note("Newsletter subscription", "body")
    assert db.search("letter") == []

    class FlakyOnce:
        """Delegates to the real connection, failing the ranked page query
        exactly once. `sqlite3.Connection.execute` is read-only, so the
        connection is wrapped rather than patched."""

        def __init__(self, conn):
            self._conn = conn
            self.failures = 1

        def execute(self, sql, *args):
            if self.failures and "MATCH" in sql and "snippet" in sql:
                self.failures -= 1
                raise sqlite3.OperationalError("database is locked")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    real_conn = db.conn
    db.conn = FlakyOnce(real_conn)             # type: ignore[assignment]
    try:
        assert db.search("letter") == [], \
            "a passing hiccup was mistaken for a bad query and answered with LIKE"
    finally:
        db.conn = real_conn


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="sqlite3 errors carry sqlite_errorcode from 3.11. On 3.10 _rejected_the_query "
           "has no code to read, so a fault that persists through the retry and clears "
           "before the probe reaches the LIKE path - the 3.10 limit its docstring states. "
           "The retry itself is covered by the test above.")
def test_a_real_busy_error_never_reaches_the_substring_path(db):
    """The guard that survived a mutation: `_rejected_the_query`.

    A hand-built `sqlite3.OperationalError("database is locked")` has no
    `sqlite_errorcode` attribute even on 3.12, so the test above it exercises
    the 3.10 degradation path rather than the error-code check. This one takes
    a GENUINE `SQLITE_BUSY` off a second connection holding an exclusive lock,
    replays it on both attempts, and asserts the search raises — because the
    only thing worse than a locked store is a locked store that answers with
    plausible substring matches.
    """
    db.add_note("Newsletter subscription", "body")

    blocker = sqlite3.connect(str(db.store_dir / "store.db"), timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        busy = None
        try:
            db.conn.execute("INSERT INTO notes (id, title, content, created_at, "
                            "updated_at) VALUES ('x','x','x','t','t')")
        except sqlite3.OperationalError as e:
            busy = e
        assert busy is not None, "the fixture did not actually lock the store"
        assert getattr(busy, "sqlite_errorcode", None) == 5, \
            "not a genuine SQLITE_BUSY — this test would prove nothing"
    finally:
        blocker.rollback()
        blocker.close()

    class AlwaysBusy:
        def __init__(self, conn, exc):
            self._conn, self._exc = conn, exc

        def execute(self, sql, *args):
            if "MATCH" in sql and "snippet" in sql:
                raise self._exc
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    real_conn = db.conn
    db.conn = AlwaysBusy(real_conn, busy)      # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.search("letter")
    finally:
        db.conn = real_conn


def test_a_broken_index_raises_from_the_page_query_too(db):
    """The second narrowed `except`, reached with the rung already chosen —
    the path `PolicyEngine.search` takes on every page."""
    db.add_note("Newsletter subscription", "body")
    db.conn.executescript(
        "DROP TRIGGER notes_ai; DROP TRIGGER notes_ad; DROP TRIGGER notes_au;"
        "DROP TABLE notes_fts;"
    )
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db.search("letter", rung="letter")


def test_every_way_fts5_words_a_rejected_query_still_reaches_the_fallback(db):
    """FTS5 does not use one phrasing for a rejected query, which is why
    nothing in the code reads those messages any more — a retry plus a health
    probe decides. These are kept as a spread of genuinely different refusals,
    so the classification is exercised across the shapes it has to survive
    rather than against one."""
    nid = db.add_note("Dash-note", 'content with "quotes" and (parens)')
    for query, wording in (('"unbalanced', "unterminated string"),
                           ("*", "unknown special query"),
                           ("(parens", "fts5: syntax error"),
                           ("nosuchcol:x", "no such column")):
        with pytest.raises(sqlite3.OperationalError) as exc:
            db.conn.execute("SELECT 1 FROM notes_fts WHERE notes_fts MATCH ?",
                            (query,)).fetchall()
        assert wording in str(exc.value), f"{query!r} no longer says {wording!r}"
        db.search(query)                       # must not raise
    assert [h["id"] for h in db.search("(parens")] == [nid]


def test_malformed_query_falls_back_and_now_reaches_tags(db):
    """The fallback fires only when MATCH refuses to parse. It gained `tags`
    so it covers what the index covers — not so it becomes a substring mode.

    The unbalanced paren is what forces the error, and it survives JSON
    encoding intact: an unbalanced *quote* would have been the obvious choice
    and is the wrong one, because `json.dumps` escapes it and the LIKE pattern
    then no longer appears in the stored text.
    """
    nid = db.add_note("t", "b", tags=["#(alpha)"])
    with pytest.raises(sqlite3.OperationalError):
        db.conn.execute("SELECT 1 FROM notes_fts WHERE notes_fts MATCH ?",
                        ("(alpha",)).fetchall()
    assert [h["id"] for h in db.search("(alpha")] == [nid]


# ── migrating a real 0.1.4 store ──────────────────────────────────────────────

def test_006_reindexes_notes_written_before_tags_were_indexed(tmp_path):
    """The rebuild has to reach notes that already existed — a store that only
    indexes tags written after the upgrade fixes nothing for anyone."""
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
        INSERT INTO notes VALUES
            ('old','ancient','body',NULL,'["#archaeology"]',NULL,NULL,'t','t');
    """)
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    db = Database(tmp_path)
    assert [h["id"] for h in db.search("archaeology")] == ["old"]

    # and the rebuilt triggers keep working for notes written afterwards
    fresh = db.add_note("new", "body", tags=["#archaeology"])
    assert {h["id"] for h in db.search("archaeology")} == {"old", fresh}
