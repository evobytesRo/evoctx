import pytest

from evoctx.db import MAX_NOTE_BYTES, Database, NoteTooLarge
from evoctx.grants import GrantStore
from evoctx.install import _human_size, doctor


def test_note_over_limit_rejected(db):
    huge = "x" * (MAX_NOTE_BYTES + 1)
    with pytest.raises(NoteTooLarge, match="over the"):
        db.add_note("t", huge)


def test_note_at_limit_accepted(db):
    exactly = "x" * MAX_NOTE_BYTES
    note_id = db.add_note("t", exactly)
    assert db.get_note(note_id) is not None


def test_note_under_limit_accepted(db):
    note_id = db.add_note("t", "short content")
    assert db.get_note(note_id) is not None


def test_rejected_note_writes_nothing(db):
    huge = "x" * (MAX_NOTE_BYTES + 1)
    with pytest.raises(NoteTooLarge):
        db.add_note("t", huge)
    assert db.list_notes() == []


# ── human_size ────────────────────────────────────────────────────────────────

def test_human_size_formatting():
    assert _human_size(500) == "500B"
    assert _human_size(2048) == "2.0KB"
    assert _human_size(5 * 1024 * 1024) == "5.0MB"


# ── doctor: size + staleness + cloud sync ──────────────────────────────────────

def test_doctor_reports_note_count_and_size(tmp_path):
    db = Database(tmp_path)
    db.add_note("t", "some content")
    GrantStore(tmp_path)
    checks = {c: (ok, detail) for c, ok, detail in doctor(tmp_path, tmp_path)}
    ok, detail = checks["database"]
    assert ok
    assert "1 notes" in detail
    assert "B" in detail  # some byte-unit suffix present


def test_doctor_staleness_no_activity(tmp_path):
    Database(tmp_path)
    GrantStore(tmp_path)
    checks = {c: (ok, detail) for c, ok, detail in doctor(tmp_path, tmp_path)}
    ok, detail = checks["last activity"]
    assert ok is True
    assert "no notes" in detail


def test_doctor_staleness_recent_activity(tmp_path):
    db = Database(tmp_path)
    db.add_note("t", "x")
    GrantStore(tmp_path)
    checks = {c: (ok, detail) for c, ok, detail in doctor(tmp_path, tmp_path)}
    ok, detail = checks["last activity"]
    assert ok is True
    assert "0 day" in detail


def test_doctor_cloud_sync_clean_path(tmp_path):
    Database(tmp_path)
    GrantStore(tmp_path)
    checks = {c: (ok, detail) for c, ok, detail in doctor(tmp_path, tmp_path)}
    ok, detail = checks["cloud sync"]
    assert ok is True


def test_doctor_cloud_sync_flags_onedrive(tmp_path):
    store = tmp_path / "OneDrive" / "evoctx-store"
    Database(store)
    GrantStore(store)
    checks = {c: (ok, detail) for c, ok, detail in doctor(store, tmp_path)}
    ok, detail = checks["cloud sync"]
    assert ok is False
    assert "CONTEXT_STORE" in detail
