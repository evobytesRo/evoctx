"""Which project is this workspace?

Until 0.1.4 nothing in the server consulted the filesystem, so an assistant
asked to name a project had nothing to match its workspace against and could
only infer a name. That is where invented project names came from — not
carelessness, an API with no other option.

The ladder, in order, and it stops at the first rung that answers:

    1. CONTEXT_PROJECT      a human wrote it into a config — authoritative
    2. cwd, longest-prefix  matched against this machine's project roots
    3. nothing matched      report the cwd and stop

**Rung 3 refuses.** No name-similarity matching, no "closest project", and
above all no picking the only project when exactly one exists — the
single-project case is precisely when a wrong guess looks right. An
unregistered workspace gets back the cwd, a suggested name and how to
register; it never gets an answer resolution did not actually find.

**Rung 2 is a default, not a constraint.** One stdio server has one cwd, and a
workspace can span many folders — the workspace this was written in is five
sibling repos. Treating the launching folder as the answer would file
`evobytes-admin` work under `evobytes-api`: the same confident-wrong failure
rung 3 exists to prevent, rebuilt one rung up. So the answer travels with
`nearby` and with wording that says, in those words, that it is a default.
"""
import os
from collections.abc import Mapping
from pathlib import Path

from .db import Database, normalize_root, this_machine
from .install import workspace_files_near

# Response values for `workspace.confidence`. Not decoration — an assistant
# reading `project` needs to know whether to treat it as given or as a guess it
# should confirm, and "authoritative" and "default" are different instructions.
AUTHORITATIVE = "authoritative"   # rung 1: a human wrote the name into a config
DEFAULT = "default"               # rung 2: cwd matched, but the workspace may be wider
NONE = "none"                     # rung 3: nothing matched, and nothing was invented

CONTRACT = (
    "default, not constraint — name a project explicitly when the work "
    "targets another one"
)


def _is_within(child: str, parent: str) -> bool:
    """Is `child` the directory `parent`, or somewhere underneath it?

    Both arguments must already be normalized (`normalize_root`), because this
    is a string comparison and on Windows a raw path will not match a stored
    one.

    The separator is the whole point. A bare `startswith` says
    `d:\\proj\\apitest` is inside `d:\\proj\\api`, which would resolve one
    project's directory to a different project that merely shares a name
    prefix. The endswith check covers a root that is already a separator —
    `normalize_root("d:\\")` is `d:\\`, and appending another one matches
    nothing.
    """
    if child == parent:
        return True
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def current_directory() -> str | None:
    """This process's cwd, normalized — or None if it cannot be read.

    For the MCP server this is whatever directory the client launched it with,
    which on both clients measured (2026-07-31) is the repo folder. It is
    behaviour rather than configuration, so the ladder degrades to rung 3 for a
    client that does something else instead of trusting it.

    `os.getcwd()` genuinely raises when the working directory has been deleted
    out from under the process. That is a workspace nobody can resolve, not a
    reason to take a tool call down with a traceback.
    """
    try:
        return normalize_root(Path.cwd())
    except OSError:
        return None


def _nearby_roots(db: Database, anchor: str, exclude: str | None) -> list[dict]:
    """Registered roots on this machine that plausibly belong to the same
    workspace as `anchor` — the other repos the user may mean.

    Two sources, unioned:

    - **siblings of the anchor.** Cheap, needs nothing on disk, and covers the
      common layout of one folder holding several repos.
    - **folders declared by any `.code-workspace` found near it.** This is the
      only signal that actually describes the *workspace* rather than the
      directory, and `install.py` already parses those files, JSONC and all.

    The anchor is the *matched root* when one matched, not the cwd. A client
    can launch the server anywhere inside a repo, and a cwd of
    `<ws>/repo-a/src` has `<ws>/repo-a` as its only sibling — so anchoring on
    cwd returns an empty `nearby` for exactly the multi-repo workspace the
    field exists for. The repo's own directory is the thing with siblings.
    Rung 3 has no matched root and anchors on cwd, which is all it has.

    The resolved project's own root is excluded: `nearby` answers "what else
    might you mean", and repeating the answer as an alternative to itself
    reads as two candidates where there is one.
    """
    anchor_parent = os.path.dirname(anchor)
    declared = set()
    for _ws, folders in workspace_files_near(Path(anchor)):
        for folder in folders:
            try:
                declared.add(normalize_root(folder))
            except OSError:
                continue  # a declared folder on a dead share is not worth failing over

    out = []
    for row in db.project_roots(machine=this_machine()):
        root = row["root_path"]
        if root == exclude:
            continue
        if os.path.dirname(root) == anchor_parent or root in declared:
            out.append({"project": row["project"], "root_path": root})
    out.sort(key=lambda r: (r["project"], r["root_path"]))
    return out


def resolve_workspace(db: Database, env: Mapping[str, str] | None = None,
                      cwd: str | Path | None = None) -> dict:
    """Run the ladder and describe the result, including how it was reached.

    `env` and `cwd` are injectable, for two different reasons. `env` because a
    test that mutates the process environment leaks into every later test in
    the same process. `cwd` because one caller is not resolving *its own*
    directory: the Stop hook is handed the workspace it fired for, and
    `os.getcwd()` in a hook subprocess is not a documented guarantee. Both
    default to this process.

    Always returns a block — there is no failure case, because "nothing
    matched" is an answer and the honest one. Callers read `project` and
    `confidence` together: a null project with a `hint` is the refusal, and
    the hint says how to make the next call resolve.
    """
    # separate names, not reassigned parameters: `here` is always a normalized
    # string or None, while `cwd` may arrive as a Path, and collapsing the two
    # is how a Path ends up compared against a stored string and matching
    # nothing
    environ: Mapping[str, str] = os.environ if env is None else env
    here: str | None
    if cwd is None:
        here = current_directory()
    else:
        try:
            here = normalize_root(cwd)
        except OSError:
            here = None
    block: dict = {
        "cwd": here,
        "project": None,
        "source": None,
        "confidence": NONE,
        "nearby": [],
    }

    # ── rung 1 ────────────────────────────────────────────────────────────────
    # A name from a config file was typed by a human on purpose, so it outranks
    # anything inferred — and when it is wrong it must say so rather than fall
    # through. Resolving somewhere else after being told where to look is the
    # confident-wrong behaviour rung 3 exists to prevent, one rung up.
    named = (environ.get("CONTEXT_PROJECT") or "").strip()
    if named:
        block["source"] = "CONTEXT_PROJECT"
        if db.project_exists(named):
            block["project"] = named
            block["confidence"] = AUTHORITATIVE
            return block
        block["hint"] = (
            f"CONTEXT_PROJECT names '{named}', which is not a project in this "
            f"store. Nothing was resolved — fix the name in your MCP config, or "
            f"create the project with: evoctx init --name {named}"
        )
        return block

    # ── rung 2 ────────────────────────────────────────────────────────────────
    # Longest match wins, so a repo registered inside a registered monorepo
    # resolves to the repo. Ties are impossible: the UNIQUE index on
    # (root_path, machine) means one directory has at most one project.
    if here is not None:
        matches = [
            row for row in db.project_roots(machine=this_machine())
            if _is_within(here, row["root_path"])
        ]
        if matches:
            best = max(matches, key=lambda r: len(r["root_path"]))
            block["project"] = best["project"]
            block["source"] = "cwd"
            block["confidence"] = DEFAULT
            block["matched_root"] = best["root_path"]
            block["note"] = CONTRACT
            block["nearby"] = _nearby_roots(db, best["root_path"],
                                            exclude=best["root_path"])
            return block

    # ── rung 3 ────────────────────────────────────────────────────────────────
    # Refuse. Everything below is about making the *next* call resolve; none of
    # it is a guess at the answer to this one.
    if here is None:
        block["hint"] = (
            "This process has no readable working directory, so the workspace "
            "cannot be identified. Name a project explicitly."
        )
        return block

    block["nearby"] = _nearby_roots(db, here, exclude=None)
    suggested = Path(here).name
    block["suggested_name"] = suggested or None
    # The CLI is the floor, not the whole answer. `PolicyEngine.workspace`
    # replaces this hint with one offering `register_project()` when the
    # caller's grant allows that tool — a tool the caller may not use is not a
    # next step, and this function has no grant to check. Callers with no
    # grant at all (`doctor`) are CLI users anyway, so the text they get here
    # is already the right one for them.
    block["hint"] = (
        f"This directory is not registered to any project. Nothing was "
        f"resolved and no name was guessed. To register it, run in that "
        f"directory: evoctx init"
        + (f" --name {suggested}" if suggested else "")
    )
    return block
