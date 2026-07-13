import json

from conftest import run


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
    assert data == {"projects": [], "notes": [], "sessions": []}


def test_export_json_roundtrips_via_cli(cli, monkeypatch, tmp_path):
    run(cli, monkeypatch, "add", "T1", "--project", "p1", "--text", "body text")
    out_file = tmp_path / "export.json"
    run(cli, monkeypatch, "export", str(out_file), "--format", "json")

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["notes"][0]["title"] == "T1"
    assert data["notes"][0]["content"] == "body text"


def test_export_markdown_to_stdout(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "add", "T1", "--project", "p1", "--text", "body text")
    capsys.readouterr()
    run(cli, monkeypatch, "export", "--format", "markdown")
    out = capsys.readouterr().out
    assert "# evoctx export" in out
    assert "### T1" in out
    assert "body text" in out
