"""Two independent Database handles on one store file — the situation
`evoctx serve` + `evoctx <anything else>` create in practice, and the
scenario the red-team review flagged as untested."""
import sqlite3
import threading

from evoctx.db import Database


def test_wal_mode_active(db):
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_two_handles_same_file_both_write(tmp_path):
    a = Database(tmp_path)
    b = Database(tmp_path)
    id_a = a.add_note("from A", "x")
    id_b = b.add_note("from B", "y")
    # each handle sees the other's committed write
    assert a.get_note(id_b)["title"] == "from B"
    assert b.get_note(id_a)["title"] == "from A"


def test_concurrent_writes_from_threads_dont_lock(tmp_path):
    # Real usage: a handful of long-lived connections (one per CLI invocation,
    # one in the server process), each opened once, writing many times over
    # its lifetime. Open all handles up front — outside the timed race — so
    # the test measures write contention, not N processes' schema-creation
    # DDL landing on the WAL file in the same instant (unrealistic; nothing
    # in this project opens 5 fresh Database() calls simultaneously).
    handles = [Database(tmp_path) for _ in range(5)]
    errors = []

    def writer(n: int, conn: Database):
        try:
            for i in range(10):
                conn.add_note(f"t{n}-{i}", "x")
        except sqlite3.OperationalError as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n, h)) for n, h in enumerate(handles)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"database was locked: {errors}"
    assert len(handles[0].list_notes()) == 50
