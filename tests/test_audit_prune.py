import sqlite3
import time

import pytest

from evoctx.audit import AuditWriter, parse_since, prune_audit_log


def make_entry(db, days_ago: int, tool: str = "get_by_id"):
    """Insert an audit_log row backdated by `days_ago` — a direct INSERT
    with the target ts, not insert-then-UPDATE, since UPDATE is exactly
    what the append-only trigger blocks."""
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    db.conn.execute(
        """INSERT INTO audit_log (ts, connection_id, client, grant_name, tool, action,
           query, record_ids, result_count, response_bytes, redactions_applied)
           VALUES (?, 'c', 'test-client', NULL, ?, 'read', NULL, '[]', 0, 0, 0)""",
        (ts, tool),
    )
    db.conn.commit()


def test_prune_deletes_only_older_entries(db):
    make_entry(db, days_ago=200)
    make_entry(db, days_ago=100)
    make_entry(db, days_ago=1)

    cutoff = parse_since("90d")
    n = prune_audit_log(db.conn, cutoff)

    assert n == 2  # the 200d and 100d entries
    remaining = db.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    assert remaining == 1


def test_prune_nothing_to_delete_returns_zero(db):
    make_entry(db, days_ago=1)
    cutoff = parse_since("90d")
    n = prune_audit_log(db.conn, cutoff)
    assert n == 0
    assert db.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1


def test_append_only_triggers_resume_after_prune(db):
    make_entry(db, days_ago=200)
    prune_audit_log(db.conn, parse_since("90d"))

    # the append-only guard must still be active for everything else —
    # pruning bypasses it only for its own scoped DELETE, not permanently
    AuditWriter(db.conn, "c").log("x", "get_by_id", "read")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.conn.execute("UPDATE audit_log SET client = 'forged'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.conn.execute("DELETE FROM audit_log")


def test_prune_via_cli(cli, monkeypatch, capsys):
    from conftest import run
    make_entry(cli.db, days_ago=200)
    make_entry(cli.db, days_ago=1)

    run(cli, monkeypatch, "audit-prune", "--older-than", "90d", "--force")
    out = capsys.readouterr().out
    assert "Pruned 1" in out
    assert cli.db.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1


def test_prune_via_cli_no_matches(cli, monkeypatch, capsys):
    from conftest import run
    make_entry(cli.db, days_ago=1)
    run(cli, monkeypatch, "audit-prune", "--older-than", "90d", "--force")
    assert "No audit entries" in capsys.readouterr().out


def test_prune_bad_older_than_exits(cli, monkeypatch, capsys):
    from conftest import run
    with pytest.raises(SystemExit):
        run(cli, monkeypatch, "audit-prune", "--older-than", "notaduration")
