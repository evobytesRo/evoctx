"""Audit log — append-only record of every tool call against the store.

Each entry: who (client + connection), what (tool, action), the effective
query (post-redaction — a search string can itself contain secrets), which
records were touched, and how much left the store. Response *content* is
never logged — that would duplicate the store.

``connection_id`` is a UUID per server process (one stdio process serves one
client connection). It is NOT the work-session id from the sessions table:
connection = transport lifetime, session = unit of work.

Append-only is enforced with SQL triggers; anyone with direct file access
can bypass them — that's the human-only deletion path, not a hole.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

AUDIT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS audit_log (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ts             TEXT NOT NULL,
        connection_id  TEXT NOT NULL,
        client         TEXT NOT NULL,
        grant_name     TEXT,
        tool           TEXT NOT NULL,
        action         TEXT NOT NULL CHECK(action IN ('read','write','denied','error')),
        query          TEXT,
        record_ids     TEXT NOT NULL DEFAULT '[]',
        result_count   INTEGER NOT NULL DEFAULT 0,
        response_bytes INTEGER NOT NULL DEFAULT 0,
        redactions_applied INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts        ON audit_log(ts);
    CREATE INDEX IF NOT EXISTS idx_audit_client_ts ON audit_log(client, ts);
    CREATE INDEX IF NOT EXISTS idx_audit_tool_ts   ON audit_log(tool, ts);

    CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def migration_002_audit_log(conn: sqlite3.Connection) -> None:
    conn.executescript(AUDIT_SCHEMA)


class AuditWriter:
    def __init__(self, conn: sqlite3.Connection, connection_id: str):
        self.conn = conn
        self.connection_id = connection_id

    def log(
        self,
        client: str,
        tool: str,
        action: str,
        grant_name: str | None = None,
        query: dict | None = None,
        record_ids: list[str] | None = None,
        result_count: int = 0,
        response_bytes: int = 0,
        redactions_applied: int = 0,
    ) -> None:
        self.conn.execute(
            """INSERT INTO audit_log
               (ts, connection_id, client, grant_name, tool, action, query,
                record_ids, result_count, response_bytes, redactions_applied)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                self.connection_id, client, grant_name, tool, action,
                json.dumps(query) if query is not None else None,
                json.dumps(record_ids or []),
                result_count, response_bytes, redactions_applied,
            ),
        )
        self.conn.commit()


_SINCE_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_since(value: str) -> str:
    """'24h' / '30m' / '7d' -> ISO cutoff timestamp."""
    unit = value[-1]
    if unit not in _SINCE_UNITS:
        raise ValueError(f"bad --since '{value}' (use e.g. 30m, 24h, 7d)")
    amount = int(value[:-1])
    cutoff = datetime.now(timezone.utc) - timedelta(**{_SINCE_UNITS[unit]: amount})
    return cutoff.isoformat()


def prune_audit_log(conn: sqlite3.Connection, before: str) -> int:
    """Deletes audit_log rows with ts < `before` (an ISO cutoff — reuse
    parse_since() to build one from '90d' etc.).

    The append-only triggers exist to stop a cooperative MCP client from
    tidying its own trail through the tool interface — they were never meant
    to stop the human operator from managing their own disk space. This is
    the one place that distinction is load-bearing: the triggers are
    deliberately dropped and recreated around a single scoped DELETE, never
    reachable from policy.py or any MCP tool path. If this function is ever
    called from anywhere but the CLI, that's the bug to fix."""
    conn.executescript("""
        DROP TRIGGER IF EXISTS audit_no_update;
        DROP TRIGGER IF EXISTS audit_no_delete;
    """)
    try:
        cur = conn.execute("DELETE FROM audit_log WHERE ts < ?", (before,))
        deleted = cur.rowcount
    finally:
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
            BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
            BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
        """)
    conn.commit()
    return deleted


def query_audit(
    conn: sqlite3.Connection,
    client: str | None = None,
    tool: str | None = None,
    action: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    q = """SELECT ts, connection_id, client, grant_name, tool, action,
                  query, record_ids, result_count, response_bytes, redactions_applied
           FROM audit_log WHERE 1=1"""
    params: list = []
    if client:
        q += " AND client = ?"
        params.append(client)
    if tool:
        q += " AND tool = ?"
        params.append(tool)
    if action:
        q += " AND action = ?"
        params.append(action)
    if since:
        q += " AND ts >= ?"
        params.append(parse_since(since))
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    return [
        {
            "ts": r[0], "connection_id": r[1], "client": r[2], "grant": r[3],
            "tool": r[4], "action": r[5],
            "query": json.loads(r[6]) if r[6] else None,
            "record_ids": json.loads(r[7]),
            "result_count": r[8], "response_bytes": r[9], "redactions": r[10],
        }
        for r in conn.execute(q, params).fetchall()
    ]
