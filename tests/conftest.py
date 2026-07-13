import importlib
import sys

import pytest

from evoctx.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_STORE", str(tmp_path))
    import evoctx.cli as cli_mod
    importlib.reload(cli_mod)  # rebind module-level db to the temp store
    return cli_mod


def run(cli, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["evoctx", *argv])
    cli.main()
