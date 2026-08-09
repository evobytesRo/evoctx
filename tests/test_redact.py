import time

from evoctx.grants import GrantStore
from evoctx.policy import PolicyEngine
from evoctx.redact import Redactor

REDACTING = """\
grants:
  - name: redacting
    client: "*"
    tools: [list_projects, get_project, search_context, get_by_id, write_note,
            start_session, end_session, recent_sessions, list_grants]
    scope: {projects: ["*"], exclude_tags: []}
    redactions:
      - pattern: "acme-\\\\w+"
        replace: "[CLIENT]"
      - pattern: "sk-[A-Za-z0-9]+"
        replace: "[KEY]"
    expires: "+1d"
"""


def make_engine(db, tmp_path):
    engine = PolicyEngine(db, GrantStore(tmp_path))
    path = tmp_path / "grants.yaml"
    path.write_text(REDACTING, encoding="utf-8")
    mtime = time.time() + 2
    import os
    os.utime(path, (mtime, mtime))
    engine.grants.activate("redacting")
    return engine


# ── Redactor unit ─────────────────────────────────────────────────────────────

def test_basic_substitution():
    r = Redactor([{"pattern": r"sk-\w+", "replace": "[KEY]"}])
    assert r.redact_text("key is sk-abc123 ok") == "key is [KEY] ok"
    assert r.hits == 1


def test_stable_tokenization_distinct_originals_numbered():
    r = Redactor([{"pattern": r"acme-\w+", "replace": "[CLIENT]"}])
    first = r.redact_text("acme-alpha and acme-beta and acme-alpha again")
    assert first == "[CLIENT] and [CLIENT_2] and [CLIENT] again"
    # same original later in the session -> same token
    assert r.redact_text("acme-beta returns") == "[CLIENT_2] returns"


def test_walks_nested_structures_not_keys():
    r = Redactor([{"pattern": r"secret", "replace": "[X]"}])
    out = r.redact({"secret": ["a secret", {"inner": "secret two"}], "n": 5})
    assert out == {"secret": ["a [X]", {"inner": "[X] two"}], "n": 5}


def test_no_rules_is_passthrough():
    r = Redactor([])
    obj = {"a": "unchanged"}
    assert r.redact(obj) is obj


# ── policy integration ────────────────────────────────────────────────────────

def test_read_paths_redact(db, tmp_path):
    engine = make_engine(db, tmp_path)
    grant = engine.check("any", "get_by_id")
    note_id = db.add_note("Client acme-corp", "token sk-live999 in body", project="p1")

    note = engine.get_note(grant, note_id)
    assert note is not None
    assert note["title"] == "Client [CLIENT]"
    assert note["content"] == "token [KEY] in body"

    hits = engine.search(grant, "token", project=None, limit=5)["results"]
    # The `or True` that used to close this line made it unconditionally green.
    # The snippet is where the masked value would surface, and the title is
    # redacted on its own account.
    assert hits, "the search found nothing, so the assertions below say nothing"
    assert "[KEY]" in hits[0]["snippet"]
    assert hits[0]["title"] == "Client [CLIENT]"
    assert "sk-live999" not in str(hits)


def test_store_keeps_raw_content(db, tmp_path):
    engine = make_engine(db, tmp_path)
    grant = engine.check("any", "get_by_id")
    note_id = db.add_note("t", "raw sk-live999", project="p1")
    engine.get_note(grant, note_id)
    # redaction is response-only; the store itself is untouched
    assert db.get_note(note_id)["content"] == "raw sk-live999"


def test_map_never_persisted(db, tmp_path):
    engine = make_engine(db, tmp_path)
    grant = engine.check("any", "get_by_id")
    note_id = db.add_note("t", "value acme-hidden here")
    engine.get_note(grant, note_id)

    # the original->placeholder map must exist nowhere on disk: the original
    # value may only live inside the store's own files (store.db and its WAL
    # sidecars, in WAL journal mode), never beside a placeholder in some
    # separate mapping file
    store_files = {"store.db", "store.db-wal", "store.db-shm", "store.db-journal"}
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.name not in store_files:
            assert "acme-hidden" not in f.read_text(encoding="utf-8", errors="ignore")

    db.conn.execute("PRAGMA wal_checkpoint(FULL)")  # force pending WAL data into store.db
    raw_db = (tmp_path / "store.db").read_bytes()
    assert b"[CLIENT]" not in raw_db  # placeholders never written into the store


def test_redactor_rebuilt_on_grant_change(db, tmp_path):
    engine = make_engine(db, tmp_path)
    grant = engine.check("any", "get_by_id")
    assert engine.redactor(grant).active
    engine.grants.activate("redacting")  # same grant -> same instance
    assert engine.redactor(grant) is engine.redactor(grant)
