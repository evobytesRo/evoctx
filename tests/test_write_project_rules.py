"""Where a write lands — refuse, default, or refuse again.

The backstop the rest of 0.1.4 was building toward. It could not have shipped
before resolution existed: an assistant had no way to learn the right project
name, so refusing would have converted stranded notes into failed writes.
"""
import pytest

from evoctx.grants import GrantStore
from evoctx.policy import PolicyDenied, PolicyEngine


@pytest.fixture
def engine(db, tmp_path):
    return PolicyEngine(db, GrantStore(tmp_path))


@pytest.fixture
def bound(db, tmp_path, monkeypatch):
    """A workspace that resolves, so the defaulting branch is reachable."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    db.register_project("myrepo", repo)
    monkeypatch.chdir(repo)
    return repo


# ── an invented name is refused ───────────────────────────────────────────────

def test_write_note_refuses_a_project_that_does_not_exist(engine, db):
    db.upsert_project("real")
    with pytest.raises(PolicyDenied, match="no project named 'reeal'"):
        engine.t_write_note("c", "t", "x", "reeal", None, None)
    assert db.list_notes() == [], "nothing may be written on a refusal"


def test_start_session_refuses_a_project_that_does_not_exist(engine, db):
    db.upsert_project("real")
    with pytest.raises(PolicyDenied, match="no project named"):
        engine.t_start_session("c", "reeal", "intent")
    assert db.recent_sessions() == []


def test_the_refusal_names_the_known_projects_and_the_workspace(engine, db, bound):
    """A refusal that does not say what the right answer is teaches the caller
    nothing, and the next attempt is another guess."""
    db.upsert_project("other")
    with pytest.raises(PolicyDenied) as exc:
        engine.t_write_note("c", "t", "x", "invented", None, None)
    msg = str(exc.value)
    assert "myrepo" in msg          # this workspace
    assert "other" in msg           # the known names
    assert "No note was created" in msg


# ── an unset project takes the workspace ──────────────────────────────────────

def test_an_unset_project_defaults_to_the_resolved_workspace(engine, db, bound):
    note_id = engine.t_write_note("c", "t", "x", None, None, None)
    assert db.get_note(note_id)["project"] == "myrepo"


def test_an_unset_session_project_defaults_to_the_resolved_workspace(engine, db, bound):
    """Otherwise `recent_sessions(project=…)` keeps missing sessions that
    plainly belong to a project."""
    session = engine.t_start_session("c", None, "intent")
    assert session["project"] == "myrepo"


def test_an_unset_project_with_nothing_resolved_is_refused(engine, db, tmp_path, monkeypatch):
    """Leaving it NULL preserves exactly the silence-conflated-with-intent
    defect this release removes — the note would be neither attributable nor
    deliberately global."""
    elsewhere = tmp_path / "unregistered"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(PolicyDenied, match="nowhere to file this"):
        engine.t_write_note("c", "t", "x", None, None, None)
    assert db.list_notes() == []


def test_the_unresolved_refusal_offers_both_honest_options(engine, db, tmp_path, monkeypatch):
    elsewhere = tmp_path / "unregistered"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(PolicyDenied) as exc:
        engine.t_write_note("c", "t", "x", None, None, None)
    msg = str(exc.value)
    assert "list_projects()" in msg
    assert "is_global" in msg


# ── global and a project are mutually exclusive, on the MCP path ──────────────
# `db.add_note` has raised ConflictingNoteScope on this since phase 1, and a
# test covered it there. That test stayed green while the MCP path silently
# accepted the combination and dropped the project, because policy nulled the
# project before db ever saw it. Green at one layer is not coverage of another.

def test_a_global_note_may_not_also_name_a_project(engine, db, bound):
    """The name would be silently dropped and the note published into every
    project's scoped read — a widening nobody asked for, from a call that
    looked like it succeeded."""
    db.upsert_project("other")
    with pytest.raises(PolicyDenied, match="mutually exclusive"):
        engine.t_write_note("c", "t", "x", "other", None, None, is_global=True)
    assert db.list_notes() == []


def test_a_global_note_naming_a_nonexistent_project_is_also_refused(engine, db, bound):
    """Otherwise is_global is a way around the existence check itself."""
    with pytest.raises(PolicyDenied):
        engine.t_write_note("c", "t", "x", "invented", None, None, is_global=True)
    assert db.list_notes() == []


# ── a global note is exempt ───────────────────────────────────────────────────

def test_a_global_note_is_not_defaulted_to_the_workspace(engine, db, bound):
    """It belongs to no project by definition. Defaulting one to the workspace
    would silently narrow the thing the caller explicitly widened."""
    note_id = engine.t_write_note("c", "t", "x", None, None, None, is_global=True)
    note = db.get_note(note_id)
    assert note["project"] is None
    assert note["is_global"] is True


def test_a_global_note_writes_even_where_nothing_resolves(engine, db, tmp_path, monkeypatch):
    """The unresolved refusal names is_global as a way through, so it has to
    actually be one."""
    elsewhere = tmp_path / "unregistered"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    note_id = engine.t_write_note("c", "t", "x", None, None, None, is_global=True)
    assert db.get_note(note_id)["is_global"] is True


# ── ordering against the scope check ──────────────────────────────────────────

SCOPED = """\
grants:
  - name: narrow
    client: "*"
    tools: ["*"]
    scope: {projects: ["payments"], exclude_tags: []}
    expires: "+1d"
"""


def test_a_defaulted_project_is_scope_checked_like_any_other(db, tmp_path, monkeypatch):
    """`allows_project(None)` is always True, so checking scope before
    resolution would wave through a null that then resolves to a project the
    grant does not cover — the write would land out of scope.

    The refusal arrives via the unresolved branch rather than the scope check,
    because `workspace()` has already nulled the out-of-scope project: out of
    scope reads as nonexistent, so a narrow grant cannot enumerate the store by
    walking directories. What matters is that the write is refused, nothing is
    saved, and the message says the grant is why — without naming the project
    it is not allowed to know about.
    """
    import os
    import time

    engine = PolicyEngine(db, GrantStore(tmp_path))
    y = tmp_path / "grants.yaml"
    y.write_text(SCOPED, encoding="utf-8")
    stamp = time.time() + 2
    os.utime(y, (stamp, stamp))
    engine.grants.activate("narrow")

    repo = tmp_path / "myrepo"
    repo.mkdir()
    db.register_project("myrepo", repo)
    monkeypatch.chdir(repo)

    # resolves to 'myrepo', which this grant does not cover
    with pytest.raises(PolicyDenied) as exc:
        engine.t_write_note("c", "t", "x", None, None, None)
    msg = str(exc.value)
    assert "Nothing was saved" in msg
    assert "narrow" in msg, "the refusal must say the grant is why"
    assert "myrepo" not in msg, "and must not leak the out-of-scope project name"
    assert db.list_notes() == []


def test_a_scope_denied_default_does_not_fall_back_to_null(db, tmp_path, monkeypatch):
    """Falling back would file the note nowhere to dodge an error, which is
    worse than the error: it is the stranding this release exists to stop,
    reached by way of a scope denial."""
    import os
    import time

    engine = PolicyEngine(db, GrantStore(tmp_path))
    y = tmp_path / "grants.yaml"
    y.write_text(SCOPED, encoding="utf-8")
    stamp = time.time() + 2
    os.utime(y, (stamp, stamp))
    engine.grants.activate("narrow")

    repo = tmp_path / "myrepo"
    repo.mkdir()
    db.register_project("myrepo", repo)
    monkeypatch.chdir(repo)

    with pytest.raises(PolicyDenied):
        engine.t_write_note("c", "t", "x", None, None, None)
    assert db.list_notes() == []
    assert [n for n in db.list_notes() if n["project"] is None] == []


# ── refusals must not leak, and must be actionable for their own caller ───────

NARROW_SCOPE = """\
grants:
  - name: narrow
    client: "*"
    tools: ["*"]
    scope: {projects: ["payments"], exclude_tags: []}
    expires: "+1d"
"""


def test_the_invented_name_refusal_lists_only_projects_this_grant_may_see(
        db, tmp_path, monkeypatch):
    """`db.list_projects()` would enumerate the whole store to a caller
    narrowed away from most of it — the existence leak `workspace()` and
    `get_project` both avoid, and a NEW one: before this, an out-of-scope name
    only ever echoed back the name the caller already had."""
    import os
    import time

    engine = PolicyEngine(db, GrantStore(tmp_path))
    y = tmp_path / "grants.yaml"
    y.write_text(NARROW_SCOPE, encoding="utf-8")
    stamp = time.time() + 2
    os.utime(y, (stamp, stamp))
    engine.grants.activate("narrow")

    db.upsert_project("payments")
    db.upsert_project("acme-secret-client")

    with pytest.raises(PolicyDenied) as exc:
        engine.t_write_note("c", "t", "x", "typo", None, None)
    msg = str(exc.value)
    assert "payments" in msg
    assert "acme-secret-client" not in msg, "leaked a project outside the grant's scope"


def test_start_session_refusals_never_mention_is_global(engine, db, tmp_path, monkeypatch):
    """`start_session` has no is_global parameter. An assistant obeying advice
    to set one gets a schema error, or loops. This is channel (f) — the text
    the model treats as most authoritative, because it answers the call it
    just made."""
    elsewhere = tmp_path / "unregistered"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    db.upsert_project("real")

    with pytest.raises(PolicyDenied) as unresolved:
        engine.t_start_session("c", None, "intent")
    with pytest.raises(PolicyDenied) as invented:
        engine.t_start_session("c", "invented", "intent")

    for exc in (unresolved, invented):
        msg = str(exc.value)
        assert "is_global" not in msg
        assert "global" not in msg
        assert "session" in msg, "and it should say what it could not file"


def test_write_note_refusals_do_offer_is_global(engine, db, tmp_path, monkeypatch):
    """The mirror of the test above — write_note DOES have the parameter, so
    withholding it would remove a real option."""
    elsewhere = tmp_path / "unregistered"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(PolicyDenied) as exc:
        engine.t_write_note("c", "t", "x", None, None, None)
    assert "is_global" in str(exc.value)


def test_a_blank_project_is_treated_as_unset(engine, db, bound):
    """A client that serialises an unset optional as "" must not get a denial
    quoting `project=''` — which reads as a bug report about itself. The db
    layer tests truthiness while policy tested `is not None`, so the two
    guards disagreed on exactly this input."""
    note_id = engine.t_write_note("c", "t", "x", "", None, None)
    assert db.get_note(note_id)["project"] == "myrepo"      # defaulted, not refused

    other = engine.t_write_note("c", "t2", "x", "   ", None, None, is_global=True)
    assert db.get_note(other)["is_global"] is True          # blank + global is fine


def test_a_refusal_never_ends_in_a_terminated_command(engine, db, tmp_path, monkeypatch):
    """The hint ends in the name the user is meant to type. A period spliced
    after it makes `myrepo.` look like part of the argument, and anything
    spliced after it runs the two together — so the hint goes last and nothing
    follows it."""
    elsewhere = tmp_path / "nowhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    db.upsert_project("real")

    with pytest.raises(PolicyDenied) as exc:
        engine.t_write_note("c", "t", "x", "typo", None, None)
    msg = str(exc.value)
    assert "evoctx init --name nowhere" in msg
    assert "--name nowhere." not in msg, "a period glued to the name the user must type"
    assert msg.rstrip().endswith("evoctx init --name nowhere")


def test_a_scope_denial_names_a_session_as_a_session(db, tmp_path, monkeypatch):
    """`check_write_project` is the surviving path for an existing but
    out-of-scope name, one line after the resolver that was parameterised."""
    import os
    import time

    engine = PolicyEngine(db, GrantStore(tmp_path))
    y = tmp_path / "grants.yaml"
    y.write_text(NARROW_SCOPE, encoding="utf-8")
    stamp = time.time() + 2
    os.utime(y, (stamp, stamp))
    engine.grants.activate("narrow")
    db.upsert_project("elsewhere")

    with pytest.raises(PolicyDenied) as exc:
        engine.t_start_session("c", "elsewhere", "i")
    assert "The session was NOT saved" in str(exc.value)


# ── the CLI is not governed by this ───────────────────────────────────────────

def test_the_cli_still_writes_what_the_human_says(db):
    """Grants and this refusal govern the MCP path, not the user's own store.
    A human typing a name into their own CLI is not guessing."""
    note_id = db.add_note("t", "x", project="a-name-nobody-registered")
    assert db.get_note(note_id)["project"] == "a-name-nobody-registered"
