"""Server tool tests — tools are plain functions under FastMCP decorators,
so they are exercised directly against a temp store."""
import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_STORE", str(tmp_path))
    monkeypatch.setenv("CONTEXT_CLIENT", "pytest")
    import evoctx.server as server_mod
    importlib.reload(server_mod)  # rebind module-level db to the temp store
    return server_mod


EXPECTED_TOOLS = {
    "list_projects", "get_project", "search_context", "get_by_id",
    "write_note", "start_session", "end_session", "recent_sessions",
    "list_grants",
}


def test_all_tools_registered(server):
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_write_then_search_then_get(server):
    note_id = server.write_note("Test note", "searchable body", ctx=None, project="p1", tags=["#t"])
    hits = server.search_context("searchable", ctx=None)
    assert hits[0]["id"] == note_id
    full = server.get_by_id(note_id, ctx=None)
    assert full["content"] == "searchable body"


def test_session_lifecycle_threads_notes(server):
    started = server.start_session(ctx=None, project="p1", intent="test run")
    assert started["open"] is True
    assert started["client"] == "pytest"

    server.write_note("mid-session note", "x", ctx=None, project="p1")
    ended = server.end_session("all done", ctx=None)
    assert ended["summary"] == "all done"
    assert [n["title"] for n in ended["notes"]] == ["mid-session note"]

    # after ending, notes are no longer attached to any session
    server.write_note("post-session note", "y", ctx=None)
    sessions = server.recent_sessions(ctx=None)
    assert len(sessions) == 1
    assert [n["title"] for n in sessions[0]["notes"]] == ["mid-session note"]


def test_end_session_without_start_returns_error(server):
    result = server.end_session("orphan summary", ctx=None)
    assert "error" in result


def test_project_roundtrip(server):
    server.db.upsert_project("p1", overview="ov")
    listed = server.list_projects(ctx=None)
    assert listed["projects"] == ["p1"]
    # the envelope always carries a workspace block, resolved or not — an
    # assistant that has to check whether the key exists is one that will
    # eventually skip it and guess a name instead
    assert "workspace" in listed
    assert server.get_project("p1", ctx=None)["overview"] == "ov"
    assert server.get_project("missing", ctx=None) is None


def test_list_grants_default_everything(server):
    desc = server.list_grants(ctx=None)
    assert desc["active_grant"] == "everything"
    assert desc["client"] == "pytest"
    assert "write_note" in desc["allowed_tools"]


# ── client attribution ────────────────────────────────────────────────────────
# Every other test in this file sets CONTEXT_CLIENT and so short-circuits on the
# first line of _client_name — these are the only ones that reach the MCP
# handshake fallback and its failure branches at all.

def _fake(**kwargs) -> Any:
    """Stand-in for the SDK objects — _client_name only walks .session,
    .client_params, .clientInfo, .name. Typed Any so the stubs can be passed
    where a real Context is declared."""
    return SimpleNamespace(**kwargs)


class _NoRequestContext:
    """Mirrors FastMCP: Context.session raises when there is no active request."""

    @property
    def session(self):
        raise ValueError("Context is not available outside of a request")


def test_client_name_env_wins_over_handshake(server, monkeypatch):
    monkeypatch.setenv("CONTEXT_CLIENT", "from-env")
    ctx = _fake(session=_fake(client_params=_fake(clientInfo=_fake(name="cursor"))))
    assert server._client_name(ctx) == "from-env"


def test_client_name_falls_back_to_handshake(server, monkeypatch):
    monkeypatch.delenv("CONTEXT_CLIENT")
    ctx = _fake(session=_fake(client_params=_fake(clientInfo=_fake(name="cursor"))))
    assert server._client_name(ctx) == "cursor"


@pytest.mark.parametrize("ctx,why", [
    (None, "no context at all — the CLI and the tests call it this way"),
    (_NoRequestContext(), "outside a request context; the SDK raises ValueError"),
    (_fake(), "a context with no .session — AttributeError"),
    (_fake(session=_fake(client_params=None)), "handshake never completed"),
    (_fake(session=_fake(client_params=_fake(clientInfo=None))), "client sent no name"),
])
def test_client_name_is_unknown_when_nobody_identifies_themselves(
    server, monkeypatch, ctx, why
):
    """Attribution is best-effort: every one of these must degrade to
    "unknown", never raise into the tool call that asked."""
    monkeypatch.delenv("CONTEXT_CLIENT")
    assert server._client_name(ctx) == "unknown", why


def test_denied_tool_raises_actionable_error(server):
    from evoctx.policy import PolicyDenied
    server.policy.grants.deactivate()
    with pytest.raises(PolicyDenied, match="evoctx grant activate"):
        server.search_context("anything", ctx=None)
