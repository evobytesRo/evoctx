"""Pre-review preflight for evoctx: produce EVIDENCE that a change was checked.

Adapted from chillhub-backend-api/scripts/preflight.py. Same premise — a checklist item
that is skipped looks exactly like one that passed, so the checks execute and print a
report, and the report is what gets pasted when review is requested.

WHAT CHANGED FOR THIS REPO (each one is a place the original would have silently misfired)
    * Interpreter is `.venv\\Scripts\\python.exe`, not `venv\\`.
    * **pyright is not a Python module here.** `python -m pyright` fails with "No module
      named pyright"; the gate is `npx pyright@1.1.411`, the version CONTRIBUTING.md and CI
      name. Running an unpinned `npx pyright` resolves a newer version that adds checks this
      codebase was never held to; running a DIFFERENT pin is the same error with a false
      sense of rigour, which is how this file first ran at 1.1.410.
    * Source lives in `src/evoctx/`, so a touched file maps to module `evoctx.<name>` —
      the original's `app/` prefix matches nothing here and would have selected zero tests.
    * There is no pyright debt ledger. Absent one, every allowance is 0, which IS this
      repo's bar — `0 errors, 0 warnings` over the whole diff.
    * pytest-cov is a `dev` extra rather than a hard dependency, so check 6 degrades to REVIEW
      naming the install command rather than reporting a coverage verdict it never measured.

WHAT IT ADDS, because two of this repo's bug classes are mechanical and repo-specific
    7. **Schema drift.** `_init()` builds the current schema for fresh stores while
       MIGRATIONS build it for existing ones, and nothing forces them to agree. Builds one
       store each way and diffs `sqlite_master`. This is the class that produced migration
       005's "duplicate column name" trap and that migration 006 had to be careful about.
    8. **Instruction-channel drift.** The same guidance ships in four places: the server's
       `instructions=` string, the tool docstrings, `docs/instructions-snippet.md` and
       `README.md` — three files, since the first two are both `server.py`. Each has gone
       stale in a past release. If one changed and the others did not, say so. REVIEW rather
       than FAIL: `server.py` is the whole MCP surface, so touching it for an unrelated reason
       is ordinary and this must not cry wolf.

USAGE
    .venv\\Scripts\\python.exe scripts\\preflight.py
    .venv\\Scripts\\python.exe scripts\\preflight.py --base main
    .venv\\Scripts\\python.exe preflight.py --repo "D:\\path\\to\\evobytes-mcp"   (out-of-tree)

EXIT CODE
    Non-zero when any check FAILS. REVIEW items do not fail the run — they are things a
    human must look at, and pretending a script can judge them would be the same false
    confidence this exists to remove.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

#: The pinned type-checker command. A LIST, and invoked through the shell resolver on
#: Windows via shutil.which, because `npx` is `npx.cmd` and subprocess will not find it
#: otherwise — the failure mode is FileNotFoundError inside the gate, which reads as
#: "pyright is clean" to anyone skimming.
#: Must stay equal to the version CONTRIBUTING.md and CI name. Different
#: pyright releases add different checks, so a report produced at another
#: version is judged against a bar this codebase was never held to — and it
#: reads as evidence either way.
PYRIGHT_CMD = ["npx", "--yes", "pyright@1.1.411"]

#: Marker widths so the report scans vertically.
PASS, FAIL, REVIEW, SKIP = "PASS  ", "FAIL  ", "REVIEW", "SKIP  "

#: A module-level constant: NAME = <literal>.
CONSTANT = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.+?)\s*(?:#.*)?$")

#: The three FILES carrying the same assistant-facing guidance. (There are
#: four channels — the tool docstrings are the fourth — but they live inside
#: server.py, so file-level drift collapses to three.)
INSTRUCTION_CHANNELS = (
    "src/evoctx/server.py",
    "docs/instructions-snippet.md",
    "README.md",
)


def git(*args: str) -> str:
    """Run git in the repo and return stdout, raising with stderr on failure."""
    proc = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def git_grep(needle: str) -> List[str]:
    """Search tracked AND untracked Python files for a literal. Empty list when absent.

    `git grep` exits 1 when it finds nothing, which is not an error, and it searches the
    index, so untracked files are invisible without `--untracked`.
    """
    proc = subprocess.run(
        ["git", "grep", "--untracked", "-n", "-F", "--", needle, "--", "*.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed ({proc.returncode}):\n{proc.stderr}")
    return [line for line in proc.stdout.splitlines() if line]


def tree_fingerprint(paths: Sequence[str]) -> str:
    """Short digest of the changed files' current contents, so a PASTED report can be
    checked against the tree it claims to describe."""
    digest = hashlib.sha256()
    for path in sorted(paths):
        full = REPO_ROOT / path
        digest.update(path.encode("utf-8"))
        if full.is_file():
            digest.update(full.read_bytes())
    return digest.hexdigest()[:12]


def _default_base() -> str:
    """The point this branch forked from, or `HEAD` if that cannot be found.

    `HEAD` is the wrong default for a committed branch and the right one for a
    working tree, so it stays as the fallback: on a repo with no `main`, or a
    detached checkout, an uncommitted-changes report is still better than a
    crash.
    """
    for ref in ("main", "master"):
        proc = subprocess.run(
            ["git", "merge-base", "HEAD", ref], cwd=REPO_ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return "HEAD"


def changed_files(base: str) -> List[str]:
    """Every file this change touches: tracked modifications plus untracked additions."""
    tracked = [line.strip() for line in git("diff", "--name-only", base).splitlines()]
    untracked = [
        line.strip()
        for line in git("ls-files", "--others", "--exclude-standard").splitlines()
    ]
    return sorted({path for path in tracked + untracked if path})


def added_lines(base: str, path: str) -> Set[int]:
    """Line numbers ADDED to a file by this change, in the file's current numbering."""
    if not (REPO_ROOT / path).exists():
        return set()

    if path in git("ls-files", "--others", "--exclude-standard"):
        total = len((REPO_ROOT / path).read_text(encoding="utf-8", errors="replace").splitlines())
        return set(range(1, total + 1))

    lines: Set[int] = set()
    cursor = 0
    for line in git("diff", "-U0", base, "--", path).splitlines():
        header = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if header:
            cursor = int(header.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            lines.add(cursor)
            cursor += 1
    return lines


def added_source(base: str, path: str) -> List[Tuple[int, str]]:
    """(line number, text) for every added line."""
    if not (REPO_ROOT / path).exists():
        return []
    body = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace").splitlines()
    return [(n, body[n - 1]) for n in sorted(added_lines(base, path)) if n <= len(body)]


# ---------------------------------------------------------------------------
# Check 2 -- pyright
# ---------------------------------------------------------------------------

def check_pyright(python_files: Sequence[str]) -> Tuple[str, List[str]]:
    """Run the PINNED pyright over exactly the touched files.

    No debt ledger in this repo, so every allowance is 0. That is not a simplification —
    it is this codebase's actual bar, and encoding it as "0 unless a ledger says otherwise"
    means adding a ledger later needs no change here.
    """
    if not python_files:
        return SKIP, ["no Python files touched"]

    # `.cmd` FIRST, and this is not belt-and-braces. Node ships both `npx` (a POSIX shell
    # script, for Git Bash) and `npx.cmd` next to each other; `shutil.which("npx")` returns
    # the extensionless one, and handing that to CreateProcess dies with
    # `OSError: [WinError 193] %1 is not a valid Win32 application`. This tool's own first
    # run against this repo failed exactly there — which is the right failure, because the
    # alternative is a swallowed exception reading as "pyright is clean".
    exe = shutil.which(f"{PYRIGHT_CMD[0]}.cmd") or shutil.which(PYRIGHT_CMD[0])
    if exe is None:
        return FAIL, [f"{PYRIGHT_CMD[0]} not on PATH — the type gate did NOT run. "
                      f"Install Node, or the report below is missing its main check."]

    proc = subprocess.run(
        [exe, *PYRIGHT_CMD[1:], "--outputjson", *python_files],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    match = re.search(r"^\s*\{", proc.stdout, re.MULTILINE)
    if match is None:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        return FAIL, ["pyright produced no JSON — the type gate did NOT run",
                      tail[-1] if tail else ""]

    report = json.loads(proc.stdout[match.start():])
    per_file: Dict[str, int] = defaultdict(int)
    dropped: List[str] = []
    for diagnostic in report.get("generalDiagnostics", []):
        if diagnostic.get("severity") != "error":
            continue
        try:
            rel = Path(diagnostic["file"]).resolve().relative_to(REPO_ROOT).as_posix()
        except (KeyError, ValueError):
            # NEVER silently: an unattributable diagnostic would make the file look clean.
            dropped.append(str(diagnostic.get("file", "<no path>")))
            continue
        per_file[rel] += 1

    notes, failed = [], False
    if dropped:
        failed = True
        notes.append(f"{len(dropped)} diagnostic(s) could not be attributed to a file and "
                     f"were NOT counted: {', '.join(sorted(set(dropped))[:3])}. "
                     f"The count below is an under-report.")
    for path in python_files:
        actual = per_file.get(path, 0)
        if actual:
            failed = True
            notes.append(f"{path}: {actual} error(s)")
    if not notes:
        notes.append(f"0 errors across {len(python_files)} file(s)")
    return (FAIL if failed else PASS), notes


# ---------------------------------------------------------------------------
# Check 3 -- which tests cover the touched modules
# ---------------------------------------------------------------------------

def discover_tests(paths: Sequence[str]) -> List[str]:
    """Test files that import a touched module, plus any touched test files themselves.

    `src/evoctx/db.py` -> `evoctx.db`, which is how every test in this repo imports it
    (`from evoctx.db import Database`). The original's `app/` prefix selects nothing here,
    and a check that silently selects no tests is worse than no check.
    """
    selected = {p for p in paths if p.startswith("tests/") and p.endswith(".py")}

    # Import-matching alone is not safe here. Most tests in this repo reach the
    # code through conftest's `db` and `cli` fixtures and never name a module,
    # so matching on `evoctx.db` selected 25 of 29 files and silently dropped
    # `tests/test_sessions.py` — seven references to `end_session`, in the
    # release that changed `end_session`. If any product module changed, the
    # whole suite is the honest selection; it takes 37 seconds.
    if any(p.startswith("src/evoctx/") and p.endswith(".py") for p in paths):
        selected |= {
            f.relative_to(REPO_ROOT).as_posix()
            for f in (REPO_ROOT / "tests").rglob("test_*.py")
        }
    return sorted(selected)


# ---------------------------------------------------------------------------
# Check 4 -- changed constants still referenced by their OLD literal
# ---------------------------------------------------------------------------

def check_constants(base: str, python_files: Sequence[str]) -> Tuple[str, List[str]]:
    """Flag constants in touched files whose literal value is ALSO hardcoded elsewhere.

    Deliberately not diff-based: what matters is base-independent and true at any moment —
    a constant exists, and its value is also written out by hand somewhere else.
    """
    notes: List[str] = []
    found = False

    for path in python_files:
        if not (REPO_ROOT / path).exists():
            continue
        for raw in (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace").splitlines():
            match = CONSTANT.match(raw.strip())
            if not match:
                continue
            name, literal = match.group(1), match.group(2).strip()
            if not (literal.startswith(('"', "'")) and literal[0] == literal[-1]):
                continue
            needle = literal[1:-1]
            if len(needle) < 4:
                continue
            # This file talks ABOUT the repo, so its prose mentions constants
            # by value constantly. Counting that as duplication buries the real
            # finding under the checker's own documentation.
            hits = []
            for hit in git_grep(needle):
                parts = hit.split(":", 2)
                if len(parts) < 2:
                    continue
                file_part, line_part = parts[0], parts[1]
                if file_part == path and name in hit:
                    continue
                # This file talks ABOUT the repo, so its prose names constants
                # by value. Subtract its own hits rather than skipping the
                # constant: skipping suppressed the REAL duplications
                # elsewhere along with the noise.
                if file_part == "scripts/preflight.py":
                    continue
                hits.append(f"{file_part}:{line_part}")

            if hits:
                found = True
                notes.append(
                    f"{path}: {name} = {literal} -- that literal is also written out by hand "
                    f"at {', '.join(hits[:6])}{' ...' if len(hits) > 6 else ''}. "
                    f"Those will NOT follow if the constant changes."
                )

    if not notes:
        notes.append("no constant in the touched files has its literal duplicated elsewhere")
    return (REVIEW if found else PASS), notes


# ---------------------------------------------------------------------------
# Check 5 -- were the failure paths you added actually executed?
# ---------------------------------------------------------------------------

def failure_path_lines(path: str) -> Set[int]:
    """Line numbers of real failure-handling CODE, found via the AST rather than grep.

    This repo is unusually comment-dense — matching `except`/`timeout`/`retry` against raw
    text would report its own docstrings as dozens of untested failure paths, and a checker
    that cries wolf gets deleted.
    """
    source_path = REPO_ROOT / path
    if not source_path.exists():
        return set()
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()

    def spanned(node: ast.AST) -> Set[int]:
        return {
            line
            for child in ast.walk(node)
            if isinstance(line := getattr(child, "lineno", None), int)
        }

    lines: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            lines |= spanned(node)
        elif isinstance(node, ast.Raise):
            lines.add(node.lineno)
        elif isinstance(node, ast.Try) and node.finalbody:
            for statement in node.finalbody:
                lines |= spanned(statement)
        elif isinstance(node, ast.Call) and ast.unparse(node.func) in ("warnings.warn", "warn"):
            lines.add(node.lineno)
    return lines


def check_failure_paths(
    base: str, python_files: Sequence[str], tests: Sequence[str]
) -> Tuple[str, List[str]]:
    """Run the selected tests under coverage and report added failure paths never executed."""
    targets = []
    # Product code only. `--cov=evoctx` cannot measure `tests/` or `scripts/`,
    # so including them guaranteed a permanent "not measured — check by hand"
    # note inside a green verdict, which is how a check gets learned and then
    # ignored.
    for path in [p for p in python_files if p.startswith("src/evoctx/")]:
        handled = failure_path_lines(path)
        added = {n for n, _ in added_source(base, path)}
        if (both := sorted(handled & added)):
            targets.append((path, both))
    if not targets:
        return SKIP, ["no failure-handling lines added"]
    if not tests:
        return REVIEW, [f"{sum(len(l) for _, l in targets)} added failure path(s), no tests selected"]

    try:
        import pytest_cov  # noqa: F401
    except ImportError:
        # Degrade loudly. Reporting PASS here would be the exact substitution this tool
        # exists to prevent: an unmeasured branch reported as a measured one.
        return REVIEW, [
            f"{sum(len(l) for _, l in targets)} added failure path(s) in "
            f"{', '.join(p for p, _ in targets)} were NOT measured — pytest-cov is not "
            f"installed. Install it to make this check real: "
            f".venv\\Scripts\\python.exe -m pip install pytest-cov",
        ]

    proc = subprocess.run(
        [PYTHON, "-m", "pytest", *tests, "-q", "--cov=evoctx",
         "--cov-report=json:.preflight-coverage.json"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    report_path = REPO_ROOT / ".preflight-coverage.json"

    if proc.returncode != 0:
        report_path.unlink(missing_ok=True)
        tail = [l for l in proc.stdout.splitlines()
                if " passed" in l or " failed" in l or " error" in l]
        return FAIL, ["the coverage run did not pass, so its measurements mean nothing",
                      *(tail[-1:] or ["no summary produced"])]

    if not report_path.exists():
        return REVIEW, ["coverage produced no report; check failure paths by hand"]

    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    finally:
        report_path.unlink(missing_ok=True)

    notes: List[str] = []
    uncovered_total = 0
    files = data.get("files", {})
    for path, lines in targets:
        entry = files.get(path) or files.get(path.replace("/", "\\"))
        if entry is None:
            # REVIEW, not a silent PASS. "I could not measure it" and "I
            # measured it and it was covered" are different answers, and this
            # check exists precisely to stop the second being reported for the
            # first.
            uncovered_total += 1
            notes.append(f"{path}: NOT MEASURED by this test selection -- check by hand")
            continue
        missing = sorted(set(lines) & set(entry.get("missing_lines", [])))
        if missing:
            uncovered_total += len(missing)
            notes.append(f"{path}: added failure path(s) NEVER EXECUTED at line(s) "
                         f"{', '.join(map(str, missing))}")

    if not notes:
        notes.append(f"every added failure path was executed "
                     f"({sum(len(l) for _, l in targets)} line(s))")
    return (REVIEW if uncovered_total else PASS), notes


# ---------------------------------------------------------------------------
# Check 7 -- _init() and MIGRATIONS must build the SAME schema  (evoctx-specific)
# ---------------------------------------------------------------------------

#: The oldest schema any released evoctx wrote — enough of it that
#: `Database.__init__` sees an existing store and runs the migrations rather
#: than taking the fresh-store branch. Only `notes` matters for that decision;
#: the rest is here so the migrations have something real to alter.
_OLDEST_SCHEMA = """
    CREATE TABLE notes (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
        project TEXT, tags TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    INSERT INTO notes VALUES
        ('seed', 'a note from before', 'body', NULL, '["#seed"]', 't', 't');
"""


def _recording(migration, index: int, log: List[str]):
    """Wrap a migration so the check can prove it actually ran."""
    def run(conn):
        log.append(f"{index:03d}")
        return migration(conn)
    return run


def _schema_of(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    """(type, name, comparable definition) for everything in the store.

    Ordinary tables are compared as a SORTED SET of columns, not as SQL text.
    Column ORDER legitimately differs between the two paths — a migration adds
    a column with `ALTER TABLE`, which appends it, while `_init()` declares it
    wherever it reads best — and `db.py`'s own comment on `notes.is_global`
    says so explicitly: "Nothing depends on the order — every INSERT here names
    its columns — so this is tidiness, not a contract." A check that fails on
    it is a check contributors learn to override.

    What DOES matter is a column present on one path and missing on the other,
    or declared with a different type or constraint — which this still catches.
    Triggers, indexes, views and virtual tables keep the literal comparison:
    their text is their behaviour, and a trigger's column list was the thing
    migration 006 had to get right.
    """
    out: List[Tuple[str, str, str]] = []
    rows = conn.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    for kind, name, sql in rows:
        flat = " ".join(sql.split())
        if kind == "table" and not flat.upper().startswith("CREATE VIRTUAL"):
            cols = sorted(
                f"{r[1]} {r[2]} notnull={r[3]} default={r[4]} pk={r[5]}"
                for r in conn.execute(f'PRAGMA table_info("{name}")')
            )
            out.append((kind, name, " | ".join(cols)))
        else:
            out.append((kind, name, flat))
    return out


def check_schema_drift() -> Tuple[str, List[str]]:
    """A fresh store and a fully migrated old store must end up identical.

    `_init()` creates the CURRENT schema for new stores while MIGRATIONS walk an old one
    forward, and nothing in the codebase forces the two to agree — they are two hand-written
    descriptions of one thing. Every release that touches the schema is a chance for them to
    diverge, and the divergence is invisible until somebody's store behaves differently from
    everybody else's.

    Not every table has a migration: one no older release reads is created by `_init()` alone,
    with IF NOT EXISTS (see MIGRATIONS in db.py). Both paths run `_init()`, so such a table is
    compared here like any other object.
    """
    src = REPO_ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from evoctx.db import MIGRATIONS, Database
    except Exception as exc:                       # noqa: BLE001 - report, never crash the gate
        return FAIL, [f"could not import evoctx.db: {exc}"]

    ran: List[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        fresh_dir = Path(tmp) / "fresh"
        fresh_dir.mkdir()
        fresh = Database(fresh_dir)
        fresh_schema = _schema_of(fresh.conn)
        fresh_version = fresh.conn.execute("PRAGMA user_version").fetchone()[0]
        fresh.conn.close()

        # A store with a REAL pre-migration schema in it, not merely
        # `user_version = 0`.
        #
        # `Database.__init__` decides freshness by whether a `notes` table
        # exists, not by the version — so an empty file took the `_init()`
        # branch and this check compared `_init()` against `_init()`. It
        # printed PASS and could not fail, which is the shape of bug it exists
        # to find. The recorder below is the guard against that returning: the
        # check now fails if no migration actually executed.
        old_dir = Path(tmp) / "old"
        old_dir.mkdir()
        seed = sqlite3.connect(old_dir / "store.db")
        seed.executescript(_OLDEST_SCHEMA)
        seed.execute("PRAGMA user_version = 0")
        seed.commit()
        seed.close()

        original = list(MIGRATIONS)
        try:
            for i, migration in enumerate(original, start=1):
                MIGRATIONS[i - 1] = _recording(migration, i, ran)
            migrated = Database(old_dir)
        finally:
            MIGRATIONS[:] = original
        migrated_schema = _schema_of(migrated.conn)
        migrated_version = migrated.conn.execute("PRAGMA user_version").fetchone()[0]
        migrated.conn.close()

    notes: List[str] = []
    if not ran:
        return FAIL, [
            "no migration ran, so this check compared _init() with itself and "
            "cannot fail. Fix the fixture, not the assertion."
        ]
    if len(ran) != len(MIGRATIONS):
        notes.append(f"only {len(ran)} of {len(MIGRATIONS)} migrations ran: "
                     f"{', '.join(ran)}")
    if fresh_version != len(MIGRATIONS):
        notes.append(f"a fresh store reports user_version {fresh_version}, "
                     f"but MIGRATIONS has {len(MIGRATIONS)} entries")
    if fresh_version != migrated_version:
        notes.append(f"fresh store is v{fresh_version}, migrated store is v{migrated_version}")

    fresh_map = {(t, n): sql for t, n, sql in fresh_schema}
    migrated_map = {(t, n): sql for t, n, sql in migrated_schema}
    for key in sorted(set(fresh_map) | set(migrated_map)):
        in_fresh, in_migrated = fresh_map.get(key), migrated_map.get(key)
        if in_fresh is None:
            notes.append(f"{key[0]} {key[1]}: built by MIGRATIONS but NOT by _init()")
        elif in_migrated is None:
            notes.append(f"{key[0]} {key[1]}: built by _init() but NOT by MIGRATIONS")
        elif in_fresh != in_migrated:
            notes.append(f"{key[0]} {key[1]}: definitions DIFFER between _init() and MIGRATIONS")
            notes.append(f"    _init()    : {in_fresh[:110]}")
            notes.append(f"    MIGRATIONS : {in_migrated[:110]}")

    if not notes:
        notes.append(f"_init() and {len(ran)} executed migration(s) agree on "
                     f"{len(fresh_schema)} object(s), both at v{fresh_version}")
        return PASS, notes
    return FAIL, notes


# ---------------------------------------------------------------------------
# Check 8 -- the instruction channels move together  (evoctx-specific)
# ---------------------------------------------------------------------------

def check_instruction_channels(paths: Sequence[str]) -> Tuple[str, List[str]]:
    """The same assistant-facing guidance ships in three files. If one moved, all should.

    Not a style rule. Three separate releases shipped with one of these stale, and the
    stale one is the copy people actually paste — a wrong instruction reaches every future
    session and is read BEFORE any runtime message can correct it.
    """
    touched = [c for c in INSTRUCTION_CHANNELS if c in paths]
    if not touched:
        return SKIP, ["no instruction channel touched"]
    missing = [c for c in INSTRUCTION_CHANNELS if c not in paths]
    if not missing:
        return PASS, [f"all {len(INSTRUCTION_CHANNELS)} channels changed together"]
    return REVIEW, [
        f"changed: {', '.join(touched)}",
        f"NOT changed: {', '.join(missing)}",
        "If the guidance actually changed, the untouched channel is now stale. If only code "
        "moved, say so explicitly — this is REVIEW, not FAIL, because a channel can "
        "legitimately be unaffected.",
    ]


# ---------------------------------------------------------------------------

def run_tests(tests: Sequence[str]) -> Tuple[str, List[str]]:
    """Run the discovered tests and report the summary line verbatim."""
    if not tests:
        return SKIP, ["no tests matched the touched modules"]
    proc = subprocess.run(
        [PYTHON, "-m", "pytest", *tests, "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    summary = [l for l in proc.stdout.splitlines() if " passed" in l or " failed" in l]
    return (PASS if proc.returncode == 0 else FAIL), (summary[-1:] or ["no summary produced"])


def emit(title: str, marker: str, notes: Iterable[str]) -> None:
    print(f"[{marker}] {title}")
    for note in notes:
        if note:
            print(f"         {note}")


def main() -> int:
    global REPO_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    # NOT `HEAD`. CONTRIBUTING asks for this report before opening a PR — i.e.
    # after committing — and against HEAD a clean committed branch has no
    # changed files, so every check is skipped and the run exits 0 printing
    # "nothing changed". A report that ran nothing is indistinguishable from a
    # report that passed, which is the failure this whole file exists to
    # remove. The merge base with the default branch is what "this PR" means.
    parser.add_argument("--base", default=_default_base(),
                        help="git ref to diff against (default: merge-base with main)")
    parser.add_argument("--repo", default=None,
                        help="repo root, when running this file from outside scripts/")
    args = parser.parse_args()
    if args.repo:
        REPO_ROOT = Path(args.repo).resolve()

    paths = changed_files(args.base)
    python_files = [p for p in paths if p.endswith(".py")]

    print("=" * 78)
    print(f"PREFLIGHT  (repo: {REPO_ROOT.name}, base: {args.base}, "
          f"tree: {tree_fingerprint(paths)})")
    print("Regenerate this immediately before requesting review -- the tree fingerprint is")
    print("what makes a stale paste detectable.")
    print("=" * 78)
    emit("1. files touched, derived from git",
         PASS if paths else REVIEW,
         paths or [f"nothing changed against '{args.base}' — no check below ran. "
                   f"Is that the base you meant? Try --base main, or the ref "
                   f"this branch forked from."])
    if not paths:
        # Deliberately not `return 0` in silence: an empty report reads as a
        # clean one, and this is the most likely way to hand over a report that
        # measured nothing at all.
        print("=" * 78)
        print("PREFLIGHT CHECKED NOTHING -- fix the base before quoting this.")
        return 0

    tests = discover_tests(paths)
    results = []

    marker, notes = check_pyright(python_files)
    emit("2. pinned pyright over exactly those files", marker, notes)
    results.append(marker)

    emit("3. tests covering the touched modules", PASS if tests else SKIP,
         tests or ["none found -- is this change tested at all?"])

    marker, notes = run_tests(tests)
    emit("4. those tests", marker, notes)
    results.append(marker)

    marker, notes = check_constants(args.base, python_files)
    emit("5. changed constants vs their old literals", marker, notes)
    results.append(marker)

    marker, notes = check_failure_paths(args.base, python_files, tests)
    emit("6. added failure paths, measured under coverage", marker, notes)
    results.append(marker)

    marker, notes = check_schema_drift()
    emit("7. _init() vs MIGRATIONS build the same schema", marker, notes)
    results.append(marker)

    marker, notes = check_instruction_channels(paths)
    emit("8. instruction channels moved together", marker, notes)
    results.append(marker)

    print("=" * 78)
    if FAIL in results:
        print("PREFLIGHT FAILED -- fix before requesting review.")
        return 1
    if REVIEW in results:
        print("PREFLIGHT PASSED WITH REVIEW ITEMS -- address or justify each one explicitly.")
        return 0
    print("PREFLIGHT CLEAN.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
