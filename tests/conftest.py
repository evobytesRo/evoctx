import importlib
import sys
from typing import TypeVar

import pytest

from evoctx.db import Database

T = TypeVar("T")


def found(value: T | None) -> T:
    """Unwrap a store lookup the test requires to have hit.

    get_note()/get_project() return Optional, and every call here fetches a
    record the test itself just wrote — so None means the setup broke. Fail
    on that, with a message, instead of on a TypeError from subscripting
    None a line later.
    """
    assert value is not None, "expected the store to return a record, got None"
    return value


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
