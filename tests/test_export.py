import json

from conftest import run

from evoctx.db import normalize_root, this_machine


def test_export_all_shape(db):
    db.upsert_project("p1", overview="ov")
    note_id = db.add_note("T1", "content here", project="p1", tags=["#x"])
    sid = db.start_session(project="p1", intent="doing stuff")
    db.end_session(sid, "done")

    data = db.export_all()
    assert data["projects"][0]["name"] == "p1"
    assert data["notes"][0]["id"] == note_id
    assert data["notes"][0]["content"] == "content here"  # full content, not a snippet
    assert data["sessions"][0]["id"] == sid
    assert data["sessions"][0]["summary"] == "done"


def test_export_all_empty_store(db):
    data = db.export_all()
    assert data == {"projects": [], "notes": [], "sessions": [], "project_roots": []}


def test_export_carries_roots_tagged_by_machine(db, tmp_path):
    """Roots must survive a backup, or a restore puts every project back to
    root-less — the exact state binding projects to directories exists to
    leave. `machine` rides along so an importer can apply only the roots that
    are true on the machine it is restoring to."""
    db.upsert_project("p1", overview="ov")
    db.add_project_root("p1", tmp_path)

    roots = db.export_all()["project_roots"]
    assert len(roots) == 1
    assert roots[0]["project"] == "p1"
    assert roots[0]["root_path"] == normalize_root(tmp_path)
    assert roots[0]["machine"] == this_machine()  # an importer filters on it
    assert roots[0]["added_at"]


def test_export_json_roundtrips_via_cli(cli, monkeypatch, tmp_path):
    run(cli, monkeypatch, "add", "T1", "--project", "p1", "--text", "body text")
    out_file = tmp_path / "export.json"
    run(cli, monkeypatch, "export", str(out_file), "--format", "json")

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["notes"][0]["title"] == "T1"
    assert data["notes"][0]["content"] == "body text"


def test_export_markdown_labels_roots_with_their_machine(cli, monkeypatch, capsys, tmp_path):
    """An export can carry roots that exist on a different machine, so an
    unlabelled path would claim to be local."""
    cli.db.upsert_project("p1", overview="ov")
    cli.db.add_project_root("p1", tmp_path, machine="other-box")
    capsys.readouterr()

    run(cli, monkeypatch, "export", "--format", "markdown")
    out = capsys.readouterr().out
    assert "**Directories**" in out
    assert "(on other-box)" in out
    assert "root_path" not in out, "the dict must be formatted, not repr'd"


def test_export_markdown_to_stdout(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "add", "T1", "--project", "p1", "--text", "body text")
    capsys.readouterr()
    run(cli, monkeypatch, "export", "--format", "markdown")
    out = capsys.readouterr().out
    assert "# evoctx export" in out
    assert "### T1" in out
    assert "body text" in out
