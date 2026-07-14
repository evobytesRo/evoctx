"""Regression coverage for cmd_serve — 0.1.0 shipped with os.execv(sys.executable, ...),
which is broken on Windows whenever sys.executable contains a space (confirmed with a
minimal repro fully outside this codebase: `os.execv` mangles the path, python reports
"can't open file '...\\Bytes'"; `subprocess.run` with the same argv does not). Since most
Windows install paths contain a space somewhere (Program Files, OneDrive - Company, a
username, ...), this broke the MCP server launch for a large fraction of real installs
despite 160 other tests passing — none of them exercised cmd_serve at all.

These tests don't spawn a real server (it blocks on stdio waiting for a client); they
verify the *mechanism* cmd_serve uses to launch it.
"""
import subprocess
import sys

import pytest

from conftest import run


def test_serve_uses_subprocess_run_not_execv(cli, monkeypatch):
    """The actual regression test: must be subprocess.run, never os.execv."""
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fail_if_called(*a, **kw):
        pytest.fail("cmd_serve must not call os.execv — broken on Windows with a space "
                     "in sys.executable (see module docstring)")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("os.execv", fail_if_called)

    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "serve")

    assert exc.value.code == 0
    assert calls == [[sys.executable, "-m", "evoctx.server"]]


def test_serve_propagates_nonzero_exit_code(cli, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda argv: subprocess.CompletedProcess(argv, 7))
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "serve")
    assert exc.value.code == 7


def test_mcp_alias_also_uses_subprocess(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
    with pytest.raises(SystemExit):
        run(cli, monkeypatch, "mcp")
    assert calls == [[sys.executable, "-m", "evoctx.server"]]
