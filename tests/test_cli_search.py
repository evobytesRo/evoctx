"""`evoctx search-context` — the human mirror of `search_context`.

The CLI has to report the same thing the MCP tool does: how many notes matched,
not just how many were printed. A person reading "10 result(s)" cannot tell a
complete answer from the first tenth of one either.
"""
import pytest
from conftest import run

from evoctx.db import SEARCH_SCAN_LIMIT


def _seed(cli, n, word="shared"):
    for i in range(n):
        cli.db.add_note(f"note {i}", f"{word} keyword body")


def test_no_matches_says_so(cli, monkeypatch, capsys):
    _seed(cli, 3)
    run(cli, monkeypatch, "search-context", "kumquat")
    assert "No matches." in capsys.readouterr().out


def test_a_complete_answer_reports_the_total_and_offers_no_next_page(cli, monkeypatch, capsys):
    _seed(cli, 4)
    run(cli, monkeypatch, "search-context", "shared")
    out = capsys.readouterr().out
    assert "showing 1-4 of 4 match(es)." in out
    assert "--offset" not in out


def test_a_truncated_answer_names_the_next_offset_and_the_token(cli, monkeypatch, capsys):
    """The hint has to carry the token too. Paging the CLI means re-running the
    process, so without it the second invocation re-chooses how broadly to read
    the query and "showing 11-20 of 25" describes a different 25."""
    _seed(cli, 25)
    run(cli, monkeypatch, "search-context", "shared", "--limit", "10")
    out = capsys.readouterr().out
    assert "showing 1-10 of 25 match(es)." in out
    assert '(more: --offset 10 --page-token "shared")' in out


def _printed_ids(out: str) -> list[str]:
    """The 8-character ids from a result listing.

    A result line is `  <id8>  <scope>  <title>  <tags>` and the snippet line
    under it is indented further — matching on "starts with spaces" picks up
    both and turns the first word of a snippet into an id.
    """
    return [line[2:10] for line in out.splitlines()
            if line.startswith("  ") and not line.startswith("   ")]


def test_the_offered_offset_actually_continues_the_list(cli, monkeypatch, capsys):
    """A hint that does not produce the next page is worse than no hint."""
    _seed(cli, 12)
    run(cli, monkeypatch, "search-context", "shared", "--limit", "5")
    first = _printed_ids(capsys.readouterr().out)
    run(cli, monkeypatch, "search-context", "shared", "--limit", "5", "--offset", "5")
    second_out = capsys.readouterr().out
    second = _printed_ids(second_out)

    assert "showing 6-10 of 12 match(es)." in second_out
    assert len(first) == 5 and len(second) == 5
    assert not set(first) & set(second), "the pages overlap"


def test_the_printed_token_keeps_a_re_run_reading_the_same_query(cli, monkeypatch, capsys):
    """Each `evoctx search --offset N` is a new process, so the rung is chosen
    again. A note written in between can narrow it, and page two then slices a
    different set while printing "showing 4-6 of 6" as though it continued."""
    cli.db.add_note("alpha note", "alpha body")
    cli.db.add_note("beta note", "beta body")

    run(cli, monkeypatch, "search-context", "alpha beta", "--limit", "1")
    first = capsys.readouterr().out
    assert '--page-token "alpha OR beta"' in first

    for i in range(6):                       # another window
        cli.db.add_note(f"alpha beta together {i}", "both terms")

    run(cli, monkeypatch, "search-context", "alpha beta", "--limit", "10", "--offset", "1")
    without = capsys.readouterr().out
    assert "of 6 match(es)" in without, "the fixture no longer narrows the rung"

    run(cli, monkeypatch, "search-context", "alpha beta", "--limit", "10",
        "--offset", "1", "--page-token", "alpha OR beta")
    with_token = capsys.readouterr().out
    assert "of 8 match(es)" in with_token, "the token did not pin the interpretation"


def test_the_hint_echoes_the_scope_flags_it_was_given(cli, monkeypatch, capsys):
    """A hint that drops `--project` looks pasteable and is not: the next
    command silently pages a larger, different result set while the line above
    it still reads like a continuation."""
    cli.db.upsert_project("p1")
    cli.db.upsert_project("p2")
    for i in range(8):
        cli.db.add_note(f"note {i}", "shared keyword", project="p1")
    for i in range(8):
        cli.db.add_note(f"other {i}", "shared keyword", project="p2")

    run(cli, monkeypatch, "search-context", "shared", "--project", "p1", "--limit", "3")
    out = capsys.readouterr().out
    assert "of 8 match(es)" in out
    assert "--project p1" in out
    assert "--limit 3" in out


def test_a_deliberate_syntax_query_prints_a_pasteable_hint(cli, monkeypatch, capsys):
    """A quoted phrase has a one-rung ladder, so there is nothing to pin — and
    its "rung" is the raw query, which printed as `--page-token ""a b""` and
    made argparse reject the pasted line."""
    for i in range(6):
        cli.db.add_note(f"note {i}", "the bank statement arrived")

    run(cli, monkeypatch, "search-context", '"bank statement"', "--limit", "2")
    out = capsys.readouterr().out
    assert "(more: --offset 2 --limit 2)" in out
    assert "--page-token" not in out


def test_a_page_token_from_a_different_query_is_ignored(cli, monkeypatch, capsys):
    _seed(cli, 3)
    run(cli, monkeypatch, "search-context", "shared", "--page-token", "something else")
    assert "showing 1-3 of 3 match(es)." in capsys.readouterr().out


def test_an_offset_past_the_end_blames_the_offset(cli, monkeypatch, capsys):
    _seed(cli, 3)
    run(cli, monkeypatch, "search-context", "shared", "--offset", "99")
    assert "No results at offset 99 — only 3 match(es)." in capsys.readouterr().out


def test_a_zero_limit_does_not_blame_the_offset(cli, monkeypatch, capsys):
    """`--limit 0` used to print "No results at offset 0", sending the user to
    fix an argument they had not touched."""
    _seed(cli, 3)
    run(cli, monkeypatch, "search-context", "shared", "--limit", "0")
    out = capsys.readouterr().out
    assert "--limit 0 requested; 3 match(es) found." in out
    assert "offset" not in out


def test_a_negative_offset_is_treated_as_the_start(cli, monkeypatch, capsys):
    _seed(cli, 3)
    run(cli, monkeypatch, "search-context", "shared", "--offset", "-5")
    assert "showing 1-3 of 3 match(es)." in capsys.readouterr().out


def test_past_the_scan_ceiling_the_total_is_marked_and_paging_is_not_offered(
        cli, monkeypatch, capsys):
    """The `+` says there is more; `--offset` cannot reach it, because the scan
    stopped there. Offering the next offset anyway dead-ends on "No results at
    offset N" one command later, contradicting the `+` just printed."""
    monkeypatch.setattr("evoctx.cli.SEARCH_SCAN_LIMIT", 10)
    _seed(cli, 14)

    run(cli, monkeypatch, "search-context", "shared", "--limit", "5", "--offset", "5")
    out = capsys.readouterr().out
    assert "showing 6-10 of 10+ match(es)." in out
    assert "--offset 10" not in out
    assert "narrow the query" in out


def test_the_scan_ceiling_is_a_database_constant_not_a_policy_private(cli):
    """Both the CLI and the policy engine bound the same store read. Reaching
    up into `PolicyEngine._MAX_SCAN` from the CLI put a database fact in the
    layer above it.

    `is`, not `==`: writing `_MAX_SCAN = 2000` back as a literal keeps the
    values equal while restoring exactly the duplication this names.
    """
    from evoctx.policy import PolicyEngine
    assert PolicyEngine._MAX_SCAN is SEARCH_SCAN_LIMIT


def test_past_the_ceiling_the_empty_page_does_not_deny_the_plus(cli, monkeypatch, capsys):
    """The page before printed `10+`. Answering the offset it implied with
    "only 10 match(es)" contradicts it one command later."""
    monkeypatch.setattr("evoctx.cli.SEARCH_SCAN_LIMIT", 10)
    _seed(cli, 14)

    run(cli, monkeypatch, "search-context", "shared", "--limit", "5", "--offset", "10")
    out = capsys.readouterr().out
    assert "only" not in out
    assert "the scan stopped at 10 match(es)" in out
    assert "Narrow the query" in out


@pytest.mark.parametrize("alias", ["search-context", "search"])
def test_both_names_reach_the_same_command(cli, monkeypatch, capsys, alias):
    _seed(cli, 2)
    run(cli, monkeypatch, alias, "shared")
    assert "showing 1-2 of 2 match(es)." in capsys.readouterr().out
