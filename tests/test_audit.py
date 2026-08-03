import sqlite3
import time

import pytest

from evoctx.audit import AuditWriter, query_audit
from evoctx.grants import GrantStore
from evoctx.policy import PolicyDenied, PolicyEngine


@pytest.fixture
def engine(db, tmp_path):
    return PolicyEngine(db, GrantStore(tmp_path), audit=AuditWriter(db.conn, "conn-1"))


def entries(db, **kw):
    return query_audit(db.conn, **kw)


def test_reads_and_writes_logged(engine, db):
    db.upsert_project("p1")
    note_id = engine.t_write_note("clientA", "Title", "content", "p1", None, None)
    engine.t_search_context("clientA", "content", None, 10)
    engine.t_get_by_id("clientA", note_id)
    engine.t_list_projects("clientA")

    log = entries(db)
    tools = [e["tool"] for e in log]
    assert tools == ["list_projects", "get_by_id", "search_context", "write_note"]
    assert all(e["client"] == "clientA" for e in log)
    assert all(e["connection_id"] == "conn-1" for e in log)

    write = log[-1]
    assert write["action"] == "write"
    assert write["record_ids"] == [note_id]
    # content is never logged — only the title
    assert "content" not in str(write["query"])

    search = log[-2]
    assert search["action"] == "read"
    assert search["query"]["query"] == "content"
    assert search["record_ids"] == [note_id]
    assert search["result_count"] == 1
    assert search["response_bytes"] > 0


def test_denied_calls_logged(engine, db):
    engine.grants.deactivate()
    with pytest.raises(PolicyDenied):
        engine.t_search_context("rogue", "anything", None, 10)
    log = entries(db, action="denied")
    assert len(log) == 1
    assert log[0]["client"] == "rogue"
    assert log[0]["tool"] == "search_context"
    assert log[0]["query"] is None  # denied entries carry no arguments


def test_session_tools_logged(engine, db):
    db.upsert_project("p1")
    s = engine.t_start_session("clientA", "p1", "intent line")
    engine.t_end_session("clientA", s["id"], "done")
    log = entries(db, action="write")
    assert [e["tool"] for e in log] == ["end_session", "start_session"]
    assert log[1]["record_ids"] == [s["id"]]


def test_audit_query_is_redacted(db, tmp_path):
    grants_yaml = tmp_path / "grants.yaml"
    engine = PolicyEngine(db, GrantStore(tmp_path), audit=AuditWriter(db.conn, "c"))
    grants_yaml.write_text("""\
grants:
  - name: red
    client: "*"
    tools: [search_context]
    scope: {projects: ["*"], exclude_tags: []}
    redactions:
      - pattern: "sk-\\\\w+"
        replace: "[KEY]"
    expires: "+1d"
""", encoding="utf-8")
    mtime = time.time() + 2
    import os
    os.utime(grants_yaml, (mtime, mtime))
    engine.grants.activate("red")

    engine.t_search_context("cli", "find sk-verysecret now", None, 10)
    entry = entries(db, tool="search_context")[0]
    assert "sk-verysecret" not in str(entry["query"])
    assert "[KEY]" in entry["query"]["query"]


def test_append_only_triggers(db):
    AuditWriter(db.conn, "c").log("x", "get_by_id", "read")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.conn.execute("UPDATE audit_log SET client = 'forged'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.conn.execute("DELETE FROM audit_log")


def test_query_filters(engine, db):
    engine.t_list_projects("alpha")
    engine.t_list_projects("beta")
    assert len(entries(db, client="alpha")) == 1
    assert len(entries(db, since="1h")) == 2
    assert len(entries(db, limit=1)) == 1
    with pytest.raises(ValueError, match="bad --since"):
        entries(db, since="tomorrow")


def test_migration_002_on_old_store(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "store.db"))
    conn.executescript("""
        CREATE TABLE notes (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
            project TEXT, tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, project TEXT,
            summary TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    from evoctx.db import MIGRATIONS, Database
    db = Database(tmp_path)  # runs 001 + 002
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'"
    ).fetchone()
