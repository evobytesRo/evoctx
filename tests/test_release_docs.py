"""The release documents agree with the version being released.

A PyPI version can never be reused, and the tagged commit is what publishes,
so the documents in that commit are the ones the release ships with. Once
`pyproject.toml` names the version at the top of the CHANGELOG, the commit is
a release commit: every CHANGELOG heading carries a date, the README status
line names the version, and no document still calls anything unreleased.
While the CHANGELOG is ahead of `pyproject.toml` — work in progress — the top
entry may say "unreleased". Any `## ` heading not in release form is reported
in both states, since it would otherwise drop out of the version comparison.
Pre-release versions (rc, dev) are not supported: a pyproject version that is
not plain X.Y.Z fails.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HEADING = re.compile(r"## (\d+)\.(\d+)\.(\d+) — (\S+)(?: — .*)?")
VERSION_LINE = re.compile(r"^##[ \t].*$", re.M)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
PENDING = re.compile(r"\bunreleased\b|\bnot (?:yet )?released\b", re.I)
#: Repository documents checked for pending wording, besides the CHANGELOG
#: headings; `doc_names()` adds the markdown under examples/ and templates/.
#: The package source is left out on purpose: its text uses "released" in the
#: technical sense ("the lock is not released until commit") and would flag.
DOCS = ("README.md", "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md",
        "docs/clients.md", "docs/instructions-snippet.md")


def doc_names() -> list[str]:
    extra = sorted(p.relative_to(ROOT).as_posix()
                   for d in ("examples", "templates") for p in (ROOT / d).glob("*.md"))
    return [*DOCS, *extra]


def release_doc_problems(pyproject: str, changelog: str, readme: str,
                         docs: dict[str, str]) -> list[str]:
    m = re.search(r'^version = "(\d+)\.(\d+)\.(\d+)"', pyproject, re.M)
    if not m:
        return ["pyproject.toml has no X.Y.Z version"]
    version = tuple(int(x) for x in m.groups())
    v = ".".join(m.groups())
    lines = [m.group(0).rstrip() for m in VERSION_LINE.finditer(changelog)]
    malformed = [line for line in lines if not HEADING.fullmatch(line)]
    if malformed:
        return [f"CHANGELOG.md heading not in '## X.Y.Z — <date or unreleased> "
                f"[— <title>]' form: {line}" for line in malformed]
    headings = [m for line in lines if (m := HEADING.fullmatch(line))]
    if not headings:
        return ["CHANGELOG.md has no '## X.Y.Z — <date> — <title>' heading"]
    top = tuple(int(x) for x in headings[0].groups()[:3])
    if top > version:
        return []
    if top < version:
        return [f"pyproject.toml says {v} but the newest CHANGELOG.md entry is "
                f"{'.'.join(map(str, top))}"]
    problems = [f"CHANGELOG.md heading {'.'.join(h.groups()[:3])} says "
                f"'{h.group(4)}', not a date"
                for h in headings if not DATE.fullmatch(h.group(4))]
    if f"**Status: v{v}.**" not in readme:
        problems.append(f"README.md status line does not name v{v}")
    for name, text in docs.items():
        for line_no, line in enumerate(text.splitlines(), 1):
            if PENDING.search(line):
                problems.append(f"{name}:{line_no} still says: {line.strip()}")
    return problems


def test_the_repository_release_documents_agree():
    read = {name: (ROOT / name).read_text(encoding="utf-8") for name in doc_names()}
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert release_doc_problems(pyproject, read["CHANGELOG.md"],
                                read["README.md"], read) == []


def test_the_scan_covers_the_markdown_under_templates():
    assert "templates/fastapi.md" in doc_names()


RELEASE = 'version = "1.2.0"\n'
DATED = "# Changelog\n\n## 1.2.0 — 2026-10-27 — x\n\n## 1.1.0 — 2026-01-01 — y\n"
STATUS = "> **Status: v1.2.0.** Works.\n"


@pytest.mark.parametrize("changelog", [DATED, DATED.replace(" — x\n", "\n")])
def test_a_clean_release_commit_passes(changelog):
    assert release_doc_problems(RELEASE, changelog, STATUS, {"README.md": STATUS}) == []


@pytest.mark.parametrize("top", ["## 1.3.0 — unreleased — z", "## 1.3.0 — unreleased"])
def test_work_in_progress_may_say_unreleased(top):
    wip = top + "\n\n" + DATED
    assert release_doc_problems(RELEASE, wip, STATUS, {"CHANGELOG.md": wip}) == []


@pytest.mark.parametrize("changelog, readme, docs, expect", [
    (DATED.replace("2026-10-27", "unreleased"), STATUS, {}, "says 'unreleased', not a date"),
    (DATED.replace("2026-01-01", "TBD"), STATUS, {}, "1.1.0 says 'TBD'"),
    (DATED, STATUS.replace("1.2.0", "1.1.0"), {}, "status line does not name v1.2.0"),
    (DATED, STATUS, {"SECURITY.md": "ok\nThis is not yet released.\n"},
     "SECURITY.md:2 still says"),
    ("## 1.1.0 — 2026-01-01 — y\n", STATUS, {}, "newest CHANGELOG.md entry is 1.1.0"),
    ("## 1.2.0\n\n## 1.1.0 — 2026-01-01 — y\n", STATUS, {}, "not in '## X.Y.Z"),
    ("## 1.2.0 - 2026-10-27 - x\n\n## 1.1.0 — 2026-01-01 — y\n", STATUS, {},
     "form: ## 1.2.0 - 2026-10-27 - x"),
    ("## 1.3.0\n\n" + DATED, STATUS, {}, "form: ## 1.3.0"),
    ("## v1.3.0 — unreleased — z\n\n" + DATED, STATUS, {}, "form: ## v1.3.0"),
    ("## [1.3.0] — 2026-10-28 — z\n\n" + DATED, STATUS, {}, "form: ## [1.3.0]"),
    ("##  1.2.0 — 2026-10-27 — x\n", STATUS, {}, "form: ##  1.2.0"),
])
def test_a_release_commit_with_stale_documents_fails(changelog, readme, docs, expect):
    problems = release_doc_problems(RELEASE, changelog, readme, docs)
    assert any(expect in p for p in problems), problems