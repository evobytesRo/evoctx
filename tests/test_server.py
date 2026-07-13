"""Server tool tests — tools are plain functions under FastMCP decorators,
so they are exercised directly against a temp store."""
import asyncio
import importlib

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
    assert server.list_projects(ctx=None) == ["p1"]
    assert server.get_project("p1", ctx=None)["overview"] == "ov"
    assert server.get_project("missing", ctx=None) is None


def test_list_grants_default_everything(server):
    desc = server.list_grants(ctx=None)
    assert desc["active_grant"] == "everything"
    assert desc["client"] == "pytest"
    assert "write_note" in desc["allowed_tools"]


def test_denied_tool_raises_actionable_error(server):
    from evoctx.policy import PolicyDenied
    server.policy.grants.deactivate()
    with pytest.raises(PolicyDenied, match="evoctx grant activate"):
        server.search_context("anything", ctx=None)
