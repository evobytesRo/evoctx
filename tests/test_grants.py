import time
from datetime import datetime, timedelta, timezone

import pytest

from evoctx.grants import DEFAULT_GRANTS_YAML, GrantError, GrantStore
from evoctx.policy import PolicyDenied, PolicyEngine


@pytest.fixture
def grants(tmp_path):
    return GrantStore(tmp_path)


@pytest.fixture
def engine(db, tmp_path):
    return PolicyEngine(db, GrantStore(tmp_path))


def write_grants(tmp_path, body: str):
    path = tmp_path / "grants.yaml"
    path.write_text(body, encoding="utf-8")
    # mtime granularity on some filesystems is 1s — force a visible change
    mtime = time.time() + 2
    import os
    os.utime(path, (mtime, mtime))


SCOPED = """\
grants:
  - name: everything
    client: "*"
    tools: [list_projects, get_project, search_context, get_by_id, write_note,
            start_session, end_session, recent_sessions, list_grants]
    scope: {projects: ["*"], exclude_tags: []}
    expires: "+30d"
  - name: alpha-read
    client: "test-*"
    tools: [search_context, get_by_id, get_project, list_projects, list_grants]
    scope:
      projects: ["alpha"]
      exclude_tags: ["#personal"]
    redactions:
      - pattern: "secret-\\\\w+"
        replace: "[SECRET]"
    expires: "+8h"
"""


# ── grant store ───────────────────────────────────────────────────────────────

def test_first_run_creates_defaults_and_activates_everything(grants, tmp_path):
    assert (tmp_path / "grants.yaml").exists()
    assert (tmp_path / "active_grant.json").exists()
    grant, expires_at, reason = grants.active_grant()
    assert grant.name == "everything"
    assert reason == ""
    assert expires_at > datetime.now(timezone.utc)


def test_activate_unknown_grant_raises(grants):
    with pytest.raises(GrantError, match="no grant named"):
        grants.activate("nope")


def test_deactivate_denies_everything(grants):
    grants.deactivate()
    grant, _, reason = grants.active_grant()
    assert grant is None
    assert "no grant is active" in reason


def test_hot_reload_on_file_change(grants, tmp_path):
    assert "alpha-read" not in grants.grants()
    write_grants(tmp_path, SCOPED)
    assert "alpha-read" in grants.grants()


def test_expired_grant_is_no_grant(grants, tmp_path):
    write_grants(tmp_path, SCOPED.replace('expires: "+30d"', 'expires: "+0m"'))
    grants.activate("everything")
    grant, _, reason = grants.active_grant()
    assert grant is None
    assert "expired" in reason


def test_absolute_expiry_parses(grants, tmp_path):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    write_grants(tmp_path, SCOPED.replace('"+30d"', f'"{future}"'))
    grants.activate("everything")
    grant, expires_at, _ = grants.active_grant()
    assert grant is not None


def test_validation_rejects_missing_expires(tmp_path):
    write_grants(tmp_path, "grants:\n  - name: bad\n    tools: [get_by_id]\n")
    # the constructor already parses (via first-run activation), so it raises there
    with pytest.raises(GrantError, match="mandatory"):
        GrantStore(tmp_path).grants()


def test_validation_rejects_unknown_tool(tmp_path):
    write_grants(tmp_path, 'grants:\n  - name: bad\n    tools: [delete_everything]\n    expires: "+1d"\n')
    with pytest.raises(GrantError, match="unknown tools"):
        GrantStore(tmp_path).grants()


# ── policy engine ─────────────────────────────────────────────────────────────

def scoped_engine(db, tmp_path):
    engine = PolicyEngine(db, GrantStore(tmp_path))
    write_grants(tmp_path, SCOPED)
    engine.grants.activate("alpha-read")
    return engine


def test_check_denies_client_mismatch(db, tmp_path):
    engine = scoped_engine(db, tmp_path)
    with pytest.raises(PolicyDenied, match="covers client"):
        engine.check("cursor", "get_by_id")
    grant = engine.check("test-client", "get_by_id")
    assert grant.name == "alpha-read"


def test_check_denies_tool_not_in_grant(db, tmp_path):
    engine = scoped_engine(db, tmp_path)
    with pytest.raises(PolicyDenied, match="does not allow write_note"):
        engine.check("test-client", "write_note")


def test_denied_message_names_evoctx_command(db, tmp_path):
    engine = PolicyEngine(db, GrantStore(tmp_path))
    engine.grants.deactivate()
    with pytest.raises(PolicyDenied, match="evoctx grant activate"):
        engine.check("any", "get_by_id")


def test_search_scoped_to_projects_and_tags(db, tmp_path):
    engine = scoped_engine(db, tmp_path)
    grant = engine.check("test-client", "search_context")
    db.add_note("alpha note", "shared keyword", project="alpha")
    db.add_note("beta note", "shared keyword", project="beta")
    db.add_note("private alpha", "shared keyword", project="alpha", tags=["#personal"])

    hits = engine.search(grant, "shared", project=None, limit=10)
    assert [h["title"] for h in hits] == ["alpha note"]


def test_get_note_blocked_indistinguishable_from_missing(db, tmp_path):
    engine = scoped_engine(db, tmp_path)
    grant = engine.check("test-client", "get_by_id")
    blocked = db.add_note("beta secret", "x", project="beta")
    tagged = db.add_note("alpha personal", "x", project="alpha", tags=["#personal"])
    assert engine.get_note(grant, blocked) is None
    assert engine.get_note(grant, tagged) is None
    assert engine.get_note(grant, "nonexistent") is None


def test_list_and_get_project_scoped(db, tmp_path):
    engine = scoped_engine(db, tmp_path)
    grant = engine.check("test-client", "list_projects")
    db.upsert_project("alpha")
    db.upsert_project("beta")
    assert engine.list_projects(grant) == ["alpha"]
    assert engine.get_project(grant, "beta") is None
    assert engine.get_project(grant, "alpha")["name"] == "alpha"


def test_write_out_of_scope_fails_loudly(db, tmp_path):
    engine = scoped_engine(db, tmp_path)
    grant = engine.grants.grants()["alpha-read"]
    with pytest.raises(PolicyDenied, match="NOT saved"):
        engine.check_write_project(grant, "beta")
    engine.check_write_project(grant, "alpha")  # no raise


def test_describe_hides_redaction_patterns(db, tmp_path):
    engine = scoped_engine(db, tmp_path)
    desc = engine.describe("test-client")
    assert desc["active_grant"] == "alpha-read"
    assert desc["redactions_active"] == 1
    assert "secret-" not in str(desc)  # the regex itself must not leak
    assert "[SECRET]" not in str(desc)


def test_describe_with_no_active_grant(db, tmp_path):
    engine = PolicyEngine(db, GrantStore(tmp_path))
    engine.grants.deactivate()
    desc = engine.describe("cli")
    assert desc["active_grant"] is None
    assert "hint" in desc
