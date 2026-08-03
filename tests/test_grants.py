import time
from datetime import datetime, timedelta, timezone

import pytest
from conftest import found

from evoctx.grants import ALL_TOOLS, DEFAULT_GRANTS_YAML, GrantError, GrantStore
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
    active, reason = grants.active_grant()
    assert active is not None
    assert active.grant.name == "everything"
    assert reason == ""
    assert active.expires_at > datetime.now(timezone.utc)


def test_activate_unknown_grant_raises(grants):
    with pytest.raises(GrantError, match="no grant named"):
        grants.activate("nope")


def test_deactivate_denies_everything(grants):
    grants.deactivate()
    active, reason = grants.active_grant()
    assert active is None
    assert "no grant is active" in reason


def test_hot_reload_on_file_change(grants, tmp_path):
    assert "alpha-read" not in grants.grants()
    write_grants(tmp_path, SCOPED)
    assert "alpha-read" in grants.grants()


def test_expired_grant_is_no_grant(grants, tmp_path):
    write_grants(tmp_path, SCOPED.replace('expires: "+30d"', 'expires: "+0m"'))
    grants.activate("everything")
    active, reason = grants.active_grant()
    assert active is None
    assert "expired" in reason


def test_absolute_expiry_parses(grants, tmp_path):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    write_grants(tmp_path, SCOPED.replace('"+30d"', f'"{future}"'))
    grants.activate("everything")
    active, _ = grants.active_grant()
    assert active is not None
    assert active.expires_at > datetime.now(timezone.utc)


def test_validation_rejects_missing_expires(tmp_path):
    write_grants(tmp_path, "grants:\n  - name: bad\n    tools: [get_by_id]\n")
    # the constructor already parses (via first-run activation), so it raises there
    with pytest.raises(GrantError, match="mandatory"):
        GrantStore(tmp_path).grants()


def test_unknown_tool_is_tolerated_not_fatal(grants, tmp_path, capsys):
    """A grants.yaml written by a NEWER release must not lock this build out.

    _parse_grant runs inside grants() -> active_grant() -> PolicyEngine.check(),
    so raising on an unrecognised name would fail EVERY tool call, not just the
    unknown one. Instead: warn on stderr and DROP the name — this build cannot
    serve that tool, so keeping it would make effective_tools() (and therefore
    list_grants()) advertise something unservable. grants.yaml on disk is never
    rewritten, so upgrading restores it.
    """
    write_grants(
        tmp_path,
        'grants:\n  - name: futuristic\n    tools: [search_context, code_symbol]\n    expires: "+1d"\n',
    )
    grant = grants.grants()["futuristic"]

    assert grant.allows_tool("search_context")      # known tool still works
    assert not grant.allows_tool("code_symbol")     # unknown one cannot be served
    assert not grant.allows_tool("write_note")      # and nothing else leaked in
    # pins the DROP specifically: an implementation that merely kept the name
    # inert would satisfy the allows_tool asserts above, then advertise an
    # unservable tool through list_grants()
    assert grant.effective_tools() == ["search_context"]
    assert "code_symbol" in capsys.readouterr().err


def test_wildcard_suppresses_the_unknown_tool_warning(grants, tmp_path, capsys):
    """A wildcard grant permits unknown names anyway, so "ignoring them" would be false."""
    write_grants(
        tmp_path,
        'grants:\n  - name: open\n    tools: ["*", "code_symbol"]\n    expires: "+1d"\n',
    )
    grant = grants.grants()["open"]

    assert grant.allows_tool("code_symbol")            # wildcard covers it
    assert grant.effective_tools() == list(ALL_TOOLS)  # and it never surfaces
    assert "code_symbol" not in capsys.readouterr().err


def test_tools_wildcard_allows_everything_including_future_tools(grants, tmp_path):
    write_grants(tmp_path, 'grants:\n  - name: open\n    tools: ["*"]\n    expires: "+1d"\n')
    grant = grants.grants()["open"]

    for tool in ALL_TOOLS:
        assert grant.allows_tool(tool)
    assert grant.allows_tool("a_tool_added_in_some_future_release")
    # config keeps "*" so it never goes stale; responses expand it so
    # list_grants() stays answerable
    assert grant.effective_tools() == list(ALL_TOOLS)


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
    assert found(engine.get_project(grant, "alpha"))["name"] == "alpha"


def test_workspace_resolving_out_of_scope_reads_as_unresolved(db, tmp_path):
    """Same rule as `get_project`: out of scope is indistinguishable from
    nonexistent, or a narrow grant could enumerate the store by walking
    directories. The hint still says what happened — a silently empty answer
    sends the caller looking for a bug."""
    engine = scoped_engine(db, tmp_path)
    grant = engine.check("test-client", "list_projects")
    beta_dir = tmp_path / "beta"
    beta_dir.mkdir()
    db.register_project("beta", beta_dir)

    block = engine.workspace(grant, cwd=beta_dir)
    assert block["project"] is None
    assert block["confidence"] == "none"
    assert "beta" not in block["hint"]
    assert "matched_root" not in block


def test_workspace_resolving_in_scope_is_returned(db, tmp_path):
    engine = scoped_engine(db, tmp_path)
    grant = engine.check("test-client", "list_projects")
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir()
    db.register_project("alpha", alpha_dir)

    assert engine.workspace(grant, cwd=alpha_dir)["project"] == "alpha"


def test_nearby_is_filtered_to_the_grants_scope(db, tmp_path):
    """`nearby` is a list of project names, so it leaks the same existence
    `list_projects` is already careful about."""
    engine = scoped_engine(db, tmp_path)
    grant = engine.check("test-client", "list_projects")
    alpha_dir, beta_dir = tmp_path / "alpha", tmp_path / "beta"
    alpha_dir.mkdir(); beta_dir.mkdir()
    db.register_project("alpha", alpha_dir)
    db.register_project("beta", beta_dir)

    block = engine.workspace(grant, cwd=alpha_dir)
    assert block["project"] == "alpha"
    assert [n["project"] for n in block["nearby"]] == []


def test_t_list_projects_returns_projects_and_a_workspace(db, tmp_path):
    engine = scoped_engine(db, tmp_path)
    db.upsert_project("alpha")
    db.upsert_project("beta")

    result = engine.t_list_projects("test-client")
    assert result["projects"] == ["alpha"]
    assert result["workspace"]["project"] is None  # nothing bound in this test


def test_write_out_of_scope_fails_loudly(db, tmp_path):
    engine = scoped_engine(db, tmp_path)
    grant = engine.grants.grants()["alpha-read"]
    with pytest.raises(PolicyDenied, match="NOT saved") as e:
        engine.check_write_project(grant, "test-client", "beta")
    # and it names a command. A scope denial used to carry none, so a model
    # told to relay "the command from the error" relayed one it knew.
    assert "evoctx grant activate" in str(e.value)
    engine.check_write_project(grant, "test-client", "alpha")  # no raise


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
