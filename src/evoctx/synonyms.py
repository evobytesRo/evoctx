"""Cheap synonym expansion for search — a hand-curated dictionary of common
dev-work term groups, expanded into an FTS5 OR-group per matching token
before the query hits the store. Catches concept pairs FTS5's tokenizer
never could ("auth bug" -> also matches "login failure"); porter stemming
(db.py's FTS5 tokenizer) handles word-form variants instead, a different
axis — the two are complementary, not overlapping.

Deliberately conservative: only bare alphabetic tokens are expanded. FTS5
operators, quoted phrases, prefix (*) and NOT (-) syntax, and anything not
a plain word are passed through untouched, so this never corrupts a more
deliberate query someone hand-writes.
"""
import re

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

_BARE_WORD = re.compile(r"^[A-Za-z]+$")
_FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


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
    tokens = query.split()
    if not tokens:
        return query
    if any(t.upper() in _FTS_OPERATORS or not _BARE_WORD.match(t) for t in tokens):
        return query

    out = []
    for tok in tokens:
        group = _LOOKUP.get(tok.lower())
        out.append("(" + " OR ".join(group) + ")" if group and len(group) > 1 else tok)
    return " AND ".join(out)
