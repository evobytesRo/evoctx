"""Cheap synonym expansion for search — a hand-curated dictionary of common
dev-work term groups, expanded into an FTS5 OR-group per matching token
before the query hits the store. Catches concept pairs FTS5's tokenizer
never could ("auth bug" -> also matches "login failure"); porter stemming
(db.py's FTS5 tokenizer) handles word-form variants instead, a different
axis — the two are complementary, not overlapping.

Deliberately conservative: only FTS5 barewords are rewritten. Operators,
quoted phrases, prefix (*) and NOT (-) syntax, and anything else that parses
as deliberate FTS5 are passed through untouched, so this never corrupts a
query someone hand-writes. `plain_tokens` is where that line is drawn, and
db.py's widening ladder asks the same question through the same function.
"""
import re
import unicodedata

SYNONYM_GROUPS: list[list[str]] = [
    ["auth", "authentication", "login", "signin"],
    ["bug", "error", "issue", "failure", "problem"],
    ["config", "configuration", "settings", "setup"],
    ["delete", "remove", "deletion"],
    ["create", "add"],
    ["update", "edit", "modify", "change"],
    ["db", "database"],
    ["api", "endpoint"],
    ["test", "tests", "testing"],
    ["deploy", "deployment", "release"],
    ["fix", "patch", "resolve", "resolved"],
    ["slow", "performance", "latency", "perf"],
    ["crash", "fail", "failing"],
    ["token", "key", "credential", "secret"],
    ["timeout", "hang", "stuck"],
]

_LOOKUP: dict[str, list[str]] = {
    word: group for group in SYNONYM_GROUPS for word in group
}

# What FTS5 will accept as a single bareword. Deliberately NOT `[A-Za-z]+`,
# which is what this started as and which silently excluded most of the
# queries people actually type at a dev context store: `mt940`, `0019`,
# `sha256`, `bank_statements`, and every Romanian word carrying a diacritic.
# Refusing to touch those bought no safety and cost them both synonym
# expansion and the whole widening ladder.
#
# `_` is in the set, and getting that wrong is why this comment is long. The
# reasoning that excluded it — "U+005F is not alphanumeric, so FTS5 will not
# parse `test_mt940` as one bareword" — sounds right and is false; measured
# against the sqlite this repo runs (3.42.0), `test_mt940`, `snake_case_name`,
# `bank_statements OR alpha` and `test_mt940*` all parse. What actually
# happens is a level down: the PARSER takes the whole thing as one bareword,
# and the unicode61 TOKENIZER then splits it on `_`, so it behaves as the
# phrase "test mt940". That is the behaviour we want, not a hazard.
_FTS_BAREWORD = re.compile(r"^[0-9A-Za-z_\u0080-\U0010FFFF]+$")
_FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def _is_deliberate_punctuation(token: str) -> bool:
    """Does this token carry punctuation that should stop the query being
    rewritten?

    Position matters, and only for one shape. `don’t` and `l’élève` are words:
    U+2019 is category `Pf`, so a flat category test turned the ladder off for
    any note-taker whose editor smartens apostrophes — a silent loss of recall
    on ordinary English and French. Punctuation BETWEEN two letters is part of
    the word; punctuation anywhere else is quoting or bracketing, which is
    exactly what must be honoured.
    """
    return any(
        _is_punctuation(c) and not (0 < i < len(token) - 1
                                    and token[i - 1].isalpha()
                                    and token[i + 1].isalpha())
        for i, c in enumerate(token)
    )


def _is_punctuation(char: str) -> bool:
    """Unicode punctuation, above the ASCII range.

    The bareword rule accepts everything from U+0080 up, which is right for
    letters \u2014 `tranzac\u021bii`, `\u00e9l\u00e8ve` \u2014 and wrong for the punctuation mixed in
    with them. `\u201cexact phrase\u201d` would otherwise be widened like any other pair
    of words, while three documents promise that quoting a phrase turns
    widening off.

    A category test rather than a list of quote characters. The list version
    covered `\u201c\u201d \u2018\u2019 \u00ab\u00bb` and missed `\u2039\u203a`, `\u300c\u300d` and the fullwidth quote \u2014 and a
    release whose whole argument is that error-string allowlists go stale
    should not answer this with another list. `P*` catches every quote,
    bracket and dash form at once, and stays right as Unicode grows.
    """
    # The ASCII exclusion is load-bearing, not tidiness: `_` is Unicode
    # category `Pc`, and without the floor this rule would undo the underscore
    # support the bareword class above exists to provide.
    return ord(char) >= 0x80 and unicodedata.category(char).startswith("P")


def plain_tokens(query: str) -> list[str] | None:
    """The query's tokens when every one is a bare word, else None.

    A leading `#` is stripped, because `#gotcha` is how anyone refers to a tag
    and `#` is not FTS5 syntax — it is a character FTS5's parser rejects
    outright. Left alone, `search("#gotcha")` raised a syntax error, fell into
    the LIKE fallback, and appeared to work while quietly doing something
    else: substring matching (so `#gotcha` also hit a note tagged `#gotchas`),
    no stemming, no synonyms, no ranking, and a flat zero for
    `#gotcha #session`, because LIKE looked for that entire string inside one
    note. Stripping the `#` sends tag queries to the index like any other word.

    None means "hands off": the query carries deliberate FTS5 syntax —
    quotes, parens, a prefix `*`, a leading `-`, or an operator keyword — and
    nothing may rewrite it. Both rewriters in this codebase ask here first,
    so a hand-written query gets synonym expansion and the widening ladder
    (db.py's `widening_ladder`) refused by the same rule rather than by two
    that can drift apart.

    The operator check is case-insensitive while FTS5's is not, so a lowercase
    `and` bails out too. Over-conservative by one word in each direction, and
    the safe direction: the cost is that `search("cats and dogs")` gets no
    ladder, not that a rewrite corrupts somebody's query.
    """
    tokens = [t.lstrip("#") for t in query.split()]
    if not tokens:
        return None
    # A token that was ONLY hashes is empty now, and an empty token would make
    # every rung of the ladder unparseable.
    if any(not t or t.upper() in _FTS_OPERATORS or not _FTS_BAREWORD.match(t)
           or _is_deliberate_punctuation(t)
           for t in tokens):
        return None
    return tokens


def expanded_tokens(tokens: list[str]) -> list[str]:
    """Each token, as itself or as an FTS5 '(a OR b OR c)' group.

    Returned per token rather than pre-joined because the caller decides the
    operator: the ladder's first rung joins these with AND and its second
    with OR, and both need the same expansion underneath.
    """
    out = []
    for tok in tokens:
        group = _LOOKUP.get(tok.lower())
        out.append("(" + " OR ".join(group) + ")" if group and len(group) > 1 else tok)
    return out


def expand_query(query: str) -> str:
    """Expand bare-word tokens that have a known synonym group into an
    FTS5 '(a OR b OR c)' clause.

    FTS5 requires an explicit boolean operator between any parenthesized
    subexpression and its neighbor — plain "(a OR b) c" is a syntax error,
    not an implicit AND the way "a c" is for two bare words. So once any
    token expands, every token in the query gets joined with an explicit
    AND rather than relying on adjacency.

    Bails out and returns the query unchanged if it contains anything that
    looks like deliberate FTS5 syntax already — quotes, parens, prefix `*`,
    a leading `-`, or an operator keyword — rather than risk mangling a
    query someone hand-wrote."""
    tokens = plain_tokens(query)
    if tokens is None:
        return query
    return " AND ".join(expanded_tokens(tokens))
