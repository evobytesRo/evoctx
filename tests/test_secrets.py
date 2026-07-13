from evoctx.secrets import redact_secrets


def test_openai_style_key_masked():
    clean, hits = redact_secrets("key is sk-abcdefghijklmnopqrstuvwxyz123456")
    assert "sk-abc" not in clean
    assert "[REDACTED_API_KEY]" in clean
    assert hits == 1


def test_aws_key_masked():
    clean, hits = redact_secrets("AKIAABCDEFGHIJKLMNOP is my id")
    assert "AKIAABCDEFGHIJKLMNOP" not in clean
    assert hits == 1


def test_github_token_masked():
    clean, hits = redact_secrets("token ghp_" + "a" * 36 + " here")
    assert "ghp_" + "a" * 36 not in clean
    assert "[REDACTED_GITHUB_TOKEN]" in clean


def test_private_key_block_masked():
    text = "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIBogIBAAJ...\n-----END RSA PRIVATE KEY-----\nafter"
    clean, hits = redact_secrets(text)
    assert "MIIBogIBAAJ" not in clean
    assert "before" in clean and "after" in clean
    assert hits == 1


def test_generic_password_assignment_masked():
    clean, hits = redact_secrets("config: password=hunter22 and other stuff")
    assert "hunter22" not in clean
    assert hits >= 1


def test_no_secret_passthrough():
    clean, hits = redact_secrets("just a normal sentence about the auth bug")
    assert clean == "just a normal sentence about the auth bug"
    assert hits == 0


def test_empty_string():
    clean, hits = redact_secrets("")
    assert clean == ""
    assert hits == 0


def test_multiple_secrets_all_counted():
    text = "sk-" + "a" * 25 + " and AKIAABCDEFGHIJKLMNOP together"
    clean, hits = redact_secrets(text)
    assert hits == 2


# ── db-layer enforcement (the actual fix — unconditional, every write path) ──

def test_add_note_redacts_content(db):
    note_id = db.add_note("t", "leaked key sk-" + "a" * 25 + " oops")
    note = db.get_note(note_id)
    assert "sk-aaa" not in note["content"]
    assert "[REDACTED_API_KEY]" in note["content"]


def test_add_note_redacts_title(db):
    note_id = db.add_note("AKIAABCDEFGHIJKLMNOP in title", "body")
    note = db.get_note(note_id)
    assert "AKIA" not in note["title"]


def test_start_session_redacts_intent(db):
    sid = db.start_session(intent="continuing with sk-" + "a" * 25)
    session = db.get_session(sid)
    assert "sk-aaa" not in session["intent"]


def test_end_session_redacts_summary(db):
    sid = db.start_session()
    db.end_session(sid, "found AKIAABCDEFGHIJKLMNOP in the logs")
    session = db.get_session(sid)
    assert "AKIA" not in session["summary"]


def test_clean_content_unaffected(db):
    note_id = db.add_note("Normal title", "Normal content, no secrets here.")
    note = db.get_note(note_id)
    assert note["title"] == "Normal title"
    assert note["content"] == "Normal content, no secrets here."
