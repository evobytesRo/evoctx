"""The resolution ladder — which project is this workspace?

Rung 1 CONTEXT_PROJECT, rung 2 cwd against registered roots, rung 3 refuse.
The tests that matter most here are the ones asserting rung 3 does NOT answer:
a guess that happens to be right is indistinguishable from resolution until
the day it is wrong, and by then a note has been filed under a name that does
not exist.
"""
import os

import pytest

from evoctx.db import Database
from evoctx.resolve import (
    AUTHORITATIVE,
    CONTRACT,
    DEFAULT,
    NONE,
    _is_within,
    resolve_workspace,
)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "store")


@pytest.fixture
def repo(tmp_path):
    """A directory that exists — roots are refused otherwise."""
    d = tmp_path / "workspace" / "myrepo"
    d.mkdir(parents=True)
    return d


# ── _is_within ────────────────────────────────────────────────────────────────

def test_a_directory_is_within_itself(tmp_path):
    from evoctx.db import normalize_root
    here = normalize_root(tmp_path)
    assert _is_within(here, here)


def test_a_name_prefix_is_not_containment(tmp_path):
    """`d:\\proj\\apitest` is not inside `d:\\proj\\api`, and a bare
    startswith says it is — which would resolve one repo's directory to a
    different project that merely shares a name prefix."""
    from evoctx.db import normalize_root
    (tmp_path / "api").mkdir()
    (tmp_path / "apitest").mkdir()
    assert not _is_within(normalize_root(tmp_path / "apitest"),
                          normalize_root(tmp_path / "api"))


def test_a_subdirectory_is_within(tmp_path):
    from evoctx.db import normalize_root
    (tmp_path / "api" / "src").mkdir(parents=True)
    assert _is_within(normalize_root(tmp_path / "api" / "src"),
                      normalize_root(tmp_path / "api"))


def test_a_root_ending_in_a_separator_still_contains(tmp_path):
    """`normalize_root` of a drive or filesystem root ends in a separator, and
    appending another one produces a prefix that matches nothing."""
    root = os.path.splitdrive(str(tmp_path))[0] + os.sep
    assert root.endswith(os.sep)
    assert _is_within(os.path.normcase(str(tmp_path)), os.path.normcase(root))


# ── rung 1: CONTEXT_PROJECT ───────────────────────────────────────────────────

def test_context_project_wins_over_the_directory(db, repo, tmp_path):
    """A human wrote the name into a config; nothing inferred outranks it."""
    other = tmp_path / "workspace" / "other"
    other.mkdir()
    db.register_project("from-cwd", repo)
    db.register_project("from-env", other)

    block = resolve_workspace(db, env={"CONTEXT_PROJECT": "from-env"}, cwd=repo)
    assert block["project"] == "from-env"
    assert block["confidence"] == AUTHORITATIVE
    assert block["source"] == "CONTEXT_PROJECT"


def test_context_project_naming_an_unknown_project_refuses(db, repo):
    """It does NOT fall through to the directory. Resolving somewhere else
    after being told where to look is the confident-wrong behaviour rung 3
    exists to prevent, one rung up — and it would hide the typo forever."""
    db.register_project("real", repo)

    block = resolve_workspace(db, env={"CONTEXT_PROJECT": "raal"}, cwd=repo)
    assert block["project"] is None
    assert block["confidence"] == NONE
    assert "raal" in block["hint"]


def test_a_blank_context_project_is_not_a_setting(db, repo):
    """An empty or whitespace value is an unset variable, not a project named
    nothing — and treating it as rung 1 would refuse every call."""
    db.register_project("real", repo)
    block = resolve_workspace(db, env={"CONTEXT_PROJECT": "   "}, cwd=repo)
    assert block["project"] == "real"
    assert block["confidence"] == DEFAULT


# ── rung 2: cwd ───────────────────────────────────────────────────────────────

def test_cwd_resolves_to_the_project_that_owns_it(db, repo):
    db.register_project("myrepo", repo)
    block = resolve_workspace(db, env={}, cwd=repo)
    assert block["project"] == "myrepo"
    assert block["source"] == "cwd"
    assert block["confidence"] == DEFAULT
    assert block["note"] == CONTRACT


def test_a_subdirectory_resolves_to_its_project(db, repo):
    """The server's cwd is whatever the client launched it with; a repo
    subdirectory is still that repo."""
    db.register_project("myrepo", repo)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert resolve_workspace(db, env={}, cwd=sub)["project"] == "myrepo"


def test_the_longest_matching_root_wins(db, tmp_path):
    """A repo registered inside a registered monorepo resolves to the repo.
    Shortest-match would file every package under the monorepo, which is the
    misfiling this release exists to stop."""
    mono = tmp_path / "mono"
    inner = mono / "packages" / "api"
    inner.mkdir(parents=True)
    db.register_project("mono", mono)
    db.register_project("api", inner)

    assert resolve_workspace(db, env={}, cwd=inner)["project"] == "api"
    assert resolve_workspace(db, env={}, cwd=mono)["project"] == "mono"


def test_another_machines_root_never_resolves(db, repo):
    """A path recorded on another machine describes that machine's
    filesystem. Matching it here is how a synced store resolves a directory to
    a project whose code is not in it."""
    db.upsert_project("elsewhere")
    db.add_project_root("elsewhere", repo, machine="other-box")

    block = resolve_workspace(db, env={}, cwd=repo)
    assert block["project"] is None
    assert block["confidence"] == NONE


# ── rung 3: refuse ────────────────────────────────────────────────────────────

def test_an_unregistered_directory_resolves_to_nothing(db, repo):
    block = resolve_workspace(db, env={}, cwd=repo)
    assert block["project"] is None
    assert block["confidence"] == NONE
    assert block["suggested_name"] == "myrepo"
    assert "evoctx init" in block["hint"]


def test_the_only_project_is_still_not_the_answer(db, repo, tmp_path):
    """Exactly when a wrong guess looks right. One project in the store is not
    evidence that this directory is it."""
    somewhere_else = tmp_path / "unrelated"
    somewhere_else.mkdir()
    db.register_project("the-only-one", somewhere_else)

    assert db.list_projects() == ["the-only-one"]
    assert resolve_workspace(db, env={}, cwd=repo)["project"] is None


def test_a_similar_name_is_not_the_answer(db, repo, tmp_path):
    """No name-similarity matching. `myrepo` on disk next to a project called
    `my-repo` is exactly the near-miss that produced the phantom this release
    exists to prevent."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    db.register_project("my-repo", elsewhere)

    assert resolve_workspace(db, env={}, cwd=repo)["project"] is None


def test_suggested_name_is_offered_not_applied(db, repo):
    """The suggestion is for the user to accept in a command; nothing creates
    a project from it. Auto-registration from a read would fill the store with
    junk projects for every directory anyone ever opened."""
    block = resolve_workspace(db, env={}, cwd=repo)
    assert block["suggested_name"] == "myrepo"
    assert db.list_projects() == []


# ── nearby ────────────────────────────────────────────────────────────────────

def test_sibling_repos_are_reported_as_nearby(db, repo, tmp_path):
    """One stdio server has one cwd, and this workspace is five sibling repos.
    Without `nearby` the launching folder's project reads as the answer for
    all of them."""
    sibling = tmp_path / "workspace" / "admin"
    sibling.mkdir()
    db.register_project("myrepo", repo)
    db.register_project("admin", sibling)

    block = resolve_workspace(db, env={}, cwd=repo)
    assert block["project"] == "myrepo"
    assert [n["project"] for n in block["nearby"]] == ["admin"]


def test_siblings_are_found_from_a_subdirectory(db, repo, tmp_path):
    """`nearby` anchors on the matched ROOT, not on cwd. A client can launch
    the server anywhere inside a repo, and `<ws>/myrepo/src` has only
    `<ws>/myrepo` as a sibling — so anchoring on cwd empties `nearby` for
    exactly the multi-repo workspace the field exists for."""
    sibling = tmp_path / "workspace" / "admin"
    sibling.mkdir()
    db.register_project("myrepo", repo)
    db.register_project("admin", sibling)
    deep = repo / "src" / "deep"
    deep.mkdir(parents=True)

    block = resolve_workspace(db, env={}, cwd=deep)
    assert block["project"] == "myrepo"
    assert [n["project"] for n in block["nearby"]] == ["admin"]


def test_the_resolved_project_is_not_listed_as_an_alternative(db, repo, tmp_path):
    sibling = tmp_path / "workspace" / "admin"
    sibling.mkdir()
    db.register_project("myrepo", repo)
    db.register_project("admin", sibling)

    block = resolve_workspace(db, env={}, cwd=repo)
    assert "myrepo" not in [n["project"] for n in block["nearby"]]


def test_a_code_workspace_folder_is_nearby_even_when_it_is_not_a_sibling(db, repo, tmp_path):
    """The only signal that describes the *workspace* rather than the
    directory. A repo two levels away, listed in the open .code-workspace, is
    as much part of this session as the one next door."""
    far = tmp_path / "other-tree" / "shared"
    far.mkdir(parents=True)
    db.register_project("myrepo", repo)
    db.register_project("shared", far)
    (repo.parent / "evo.code-workspace").write_text(
        '{"folders": [{"path": "myrepo"}, {"path": "../other-tree/shared"}]}',
        encoding="utf-8",
    )

    block = resolve_workspace(db, env={}, cwd=repo)
    assert [n["project"] for n in block["nearby"]] == ["shared"]


def test_nearby_is_reported_even_when_nothing_resolves(db, repo, tmp_path):
    """The refusal is more useful with the candidates attached — that is the
    difference between "register this" and "you probably meant one of these"."""
    sibling = tmp_path / "workspace" / "admin"
    sibling.mkdir()
    db.register_project("admin", sibling)

    block = resolve_workspace(db, env={}, cwd=repo)
    assert block["project"] is None
    assert [n["project"] for n in block["nearby"]] == ["admin"]


def test_another_machines_root_is_not_nearby(db, repo, tmp_path):
    sibling = tmp_path / "workspace" / "admin"
    sibling.mkdir()
    db.register_project("myrepo", repo)
    db.upsert_project("admin")
    db.add_project_root("admin", sibling, machine="other-box")

    assert resolve_workspace(db, env={}, cwd=repo)["nearby"] == []
