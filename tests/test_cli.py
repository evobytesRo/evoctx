"""CLI smoke tests — invoke evoctx main() with argv against a temp store."""
import os
import time

import pytest

from conftest import run


def test_write_note_and_list_projects(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "new-project", "p1", "--overview", "test project")
    run(cli, monkeypatch, "write-note", "T1", "--project", "p1", "--text", "body", "--tags", "#x")
    run(cli, monkeypatch, "list-projects")
    out = capsys.readouterr().out
    assert "p1" in out
    assert "1 project(s)" in out


def test_search_and_show_roundtrip(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "add", "Findable title", "--text", "unique keyword here")
    capsys.readouterr()  # drain the add output
    run(cli, monkeypatch, "search", "unique")
    out = capsys.readouterr().out
    assert "Findable title" in out
    note_id = out.strip().splitlines()[0].split()[0]
    run(cli, monkeypatch, "get-by-id", note_id)
    assert "unique keyword here" in capsys.readouterr().out


def test_get_project_output(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "new-project", "p1", "--overview", "the overview")
    run(cli, monkeypatch, "get-project", "p1")
    out = capsys.readouterr().out
    assert "the overview" in out
    assert "Open questions" in out


def test_session_lifecycle(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "start-session", "--project", "p1", "--intent", "cli test")
    out = capsys.readouterr().out
    assert "Started session" in out

    run(cli, monkeypatch, "recent-sessions")
    out = capsys.readouterr().out
    assert "OPEN" in out
    assert "cli test" in out

    run(cli, monkeypatch, "end-session", "wrapped up")
    assert "Closed session" in capsys.readouterr().out

    run(cli, monkeypatch, "recent-sessions")
    out = capsys.readouterr().out
    assert "closed" in out
    assert "wrapped up" in out


def test_end_session_without_open_exits_nonzero(cli, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        run(cli, monkeypatch, "end-session", "nothing to close")


def test_delete_force_refuses_an_ambiguous_prefix(cli, monkeypatch, capsys):
    """--force skips the confirmation prompt, so an ambiguous prefix must not
    resolve to an arbitrary note — that would be an unrecoverable delete of
    something the user never named."""
    run(cli, monkeypatch, "add", "first", "--text", "x")
    run(cli, monkeypatch, "add", "second", "--text", "y")
    cli.db.conn.execute("UPDATE notes SET id = 'dupe-' || id")
    cli.db.conn.commit()
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "delete", "dupe-", "--force")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Ambiguous id" in out
    assert "matches 2 records" in out
    assert len(cli.db.list_notes()) == 2, "nothing may be deleted on an ambiguous prefix"


def test_end_session_refuses_an_ambiguous_prefix(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "start-session", "--project", "p1", "--intent", "one")
    run(cli, monkeypatch, "start-session", "--project", "p2", "--intent", "two")
    cli.db.conn.execute("UPDATE sessions SET id = 'dupe-' || id")
    cli.db.conn.commit()
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "end-session", "summary", "--id", "dupe-")
    assert exc.value.code == 1
    sessions = cli.db.recent_sessions()
    assert "Ambiguous id" in capsys.readouterr().out
    assert len(sessions) == 2  # or the all() below is vacuously true
    assert all(s["open"] for s in sessions), "neither may be closed"


# ── grants ────────────────────────────────────────────────────────────────────

def _replace_grants(tmp_path, body: str):
    path = tmp_path / "grants.yaml"
    path.write_text(body, encoding="utf-8")
    # 1s mtime granularity on some filesystems would hide the change from the
    # store's hot-reload check
    stamp = time.time() + 2
    os.utime(path, (stamp, stamp))


def test_grant_activate_then_list(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "grant", "activate", "everything")
    assert "expires" in capsys.readouterr().out

    run(cli, monkeypatch, "grant", "list")
    out = capsys.readouterr().out
    assert "* everything" in out
    assert "Active: everything" in out


def test_grant_activate_already_expired_explains_and_exits_nonzero(
    cli, monkeypatch, capsys, tmp_path
):
    """Writing active_grant.json is not the same as having a usable grant.

    A grant whose expiry has already passed activates fine and then denies
    every call. Before this was handled, the success line dereferenced the
    None expiry and died with an AttributeError instead of saying so.
    """
    _replace_grants(
        tmp_path,
        'grants:\n  - name: stale\n    tools: [get_by_id]\n    expires: "+0m"\n',
    )
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "grant", "activate", "stale")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "not usable" in out
    assert "expired" in out
    assert "grants.yaml" in out  # points at the file to fix


@pytest.fixture
def no_clients_installed(monkeypatch):
    """`grant activate --client` resolves every installed client to decide its
    exit code, and discovery reads the real home plus `cwd/.mcp.json`. Without
    pinning it, whether this suite passes depends on which assistants the
    developer happens to have installed."""
    import evoctx.install
    monkeypatch.setattr(evoctx.install, "installed_clients",
                        lambda home, include_cwd=True: [])


def installed(monkeypatch, *names):
    import evoctx.install
    monkeypatch.setattr(evoctx.install, "installed_clients",
                        lambda home, include_cwd=True: [(n, n) for n in names])


TWO_GRANTS = """\
grants:
  - name: everything
    client: "*"
    tools: ["*"]
    expires: "+30d"
  - name: read-only
    client: "cursor*"
    tools: [list_projects, get_by_id]
    expires: "+8h"
  - name: lapsed
    client: "*"
    tools: ["*"]
    expires: "+0m"
"""


def test_grant_activate_per_client_then_list(cli, monkeypatch, no_clients_installed, capsys, tmp_path):
    _replace_grants(tmp_path, TWO_GRANTS)
    run(cli, monkeypatch, "grant", "activate", "read-only", "--client", "cursor*")
    assert "Clients matching 'cursor*'" in capsys.readouterr().out

    run(cli, monkeypatch, "grant", "list")
    out = capsys.readouterr().out
    assert "Per-client grants" in out
    assert "cursor*" in out and "read-only" in out


def test_machine_wide_activate_says_the_map_still_wins(cli, monkeypatch, no_clients_installed, capsys, tmp_path):
    """"Activated" on its own reads as machine-wide when it is not — the
    entries take precedence, and a user who does not know that goes looking
    for the change they just made."""
    _replace_grants(tmp_path, TWO_GRANTS)
    run(cli, monkeypatch, "grant", "activate", "read-only", "--client", "cursor*")
    capsys.readouterr()
    run(cli, monkeypatch, "grant", "activate", "everything")
    out = capsys.readouterr().out
    assert "Per-client grants are unchanged" in out
    assert "cursor*" in out


def test_per_client_activate_of_an_expired_grant_exits_nonzero(
    cli, monkeypatch, no_clients_installed, capsys, tmp_path
):
    """The same "exit 0 means usable" contract as the machine-wide path, but
    judged against this entry's own clock. Checking the scalar here would
    report success for an activation that denies every call from the client
    it was written for."""
    _replace_grants(tmp_path, TWO_GRANTS)
    run(cli, monkeypatch, "grant", "activate", "everything")
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "grant", "activate", "lapsed", "--client", "vscode")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "would not be usable" in out and "expired" in out
    # and the failed activation left nothing behind: every check here runs
    # after the write, so a non-zero exit that kept it would mean an exit
    # code of 1 alongside an applied grant
    assert "Nothing was changed." in out
    run(cli, monkeypatch, "grant", "list")
    assert "Per-client grants" not in capsys.readouterr().out


def test_an_exact_key_shadowing_a_new_glob_is_reported_not_failed(
    cli, monkeypatch, no_clients_installed, capsys, tmp_path
):
    """Writing a usable entry is not the same as making the client usable:
    an exact key still wins for the client it names, so what was just written
    does not reach it.

    Reported, not failed — an exact override beside a family glob is the
    precedence rule this feature documents, so failing it would make a
    supported configuration permanently unreachable. And the way out never
    widens: apply the same grant to the exact key.
    """
    _replace_grants(tmp_path, TWO_GRANTS)
    run(cli, monkeypatch, "grant", "activate", "everything", "--client", "cursor")
    capsys.readouterr()
    run(cli, monkeypatch, "grant", "activate", "read-only", "--client", "cursor*")
    out = capsys.readouterr().out
    assert "does NOT apply to these clients" in out
    # the exact key, not a substring of it: "cursor" is inside "cursor*", so
    # `"cursor" in out` passes even when the message names the wrong one
    named = [line.split(":")[0].strip() for line in out.splitlines()
             if line.startswith("  ")]
    assert named == ["cursor"]
    assert "deactivate" not in out          # removal here would widen 'cursor'
    # a placeholder, not the grant just written: naming it reads as the
    # obvious completion, and completing it widens whenever that grant is
    # broader than the override — the normal case, since the asymmetry is
    # why the override exists
    assert 'evoctx grant activate <name> --client "<key>"' in out


def test_kill_switch_reports_what_it_cleared(cli, monkeypatch, no_clients_installed, capsys, tmp_path):
    _replace_grants(tmp_path, TWO_GRANTS)
    run(cli, monkeypatch, "grant", "activate", "read-only", "--client", "cursor*")
    capsys.readouterr()
    run(cli, monkeypatch, "grant", "deactivate")
    out = capsys.readouterr().out
    assert "Every MCP call is now denied" in out
    assert "cleared 1 per-client grant" in out


def test_deactivate_unknown_client_key_exits_nonzero(
    cli, monkeypatch, capsys, tmp_path
):
    _replace_grants(tmp_path, TWO_GRANTS)
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "grant", "deactivate", "--client", "nosuch")
    assert exc.value.code == 1
    assert "no per-client grant under key" in capsys.readouterr().out


def test_deactivate_with_an_empty_client_key_is_not_the_kill_switch(
    cli, monkeypatch, no_clients_installed, capsys, tmp_path
):
    """`--client ""` asks to remove one entry. Branching on truthiness sent it
    to the kill switch instead, clearing the machine-wide grant and every
    other entry — not a smaller version of what was asked."""
    _replace_grants(tmp_path, TWO_GRANTS)
    run(cli, monkeypatch, "grant", "activate", "read-only", "--client", "cursor*")
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "grant", "deactivate", "--client", "")
    assert exc.value.code == 1
    run(cli, monkeypatch, "grant", "list")
    assert "cursor*" in capsys.readouterr().out


def test_activate_with_an_empty_client_key_is_refused(cli, monkeypatch, capsys, tmp_path):
    _replace_grants(tmp_path, TWO_GRANTS)
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "grant", "activate", "everything", "--client", "")
    assert exc.value.code == 1
    assert "empty key" in capsys.readouterr().out


def test_glob_key_says_which_check_it_could_not_run(cli, monkeypatch, no_clients_installed, capsys, tmp_path):
    """`read-only` carries `client: "cursor*"`, so pointing key `claude*` at
    it produces an entry that resolves and then denies every claude call.
    Two globs cannot be compared in advance — exit 0 must not imply a check
    that did not happen."""
    _replace_grants(tmp_path, TWO_GRANTS)
    run(cli, monkeypatch, "grant", "activate", "read-only", "--client", "claude*")
    out = capsys.readouterr().out
    assert "only covers clients matching 'cursor*'" in out


def test_activation_that_leaves_an_installed_client_denied_exits_nonzero(
    cli, monkeypatch, capsys, tmp_path
):
    """The sound half of the check the glob note above could not run. Whether
    two globs can both match *something* is undecidable here; whether the
    clients on this machine can call is a lookup — and `read-only` carries
    `client: "cursor*"`, so key `claude*` denies claude-code outright."""
    _replace_grants(tmp_path, TWO_GRANTS)
    installed(monkeypatch, "claude-code")
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "grant", "activate", "read-only", "--client", "claude*")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "would still be denied every call" in out
    assert "claude-code" in out
    assert "Nothing was changed." in out
    run(cli, monkeypatch, "grant", "list")
    assert "Per-client grants" not in capsys.readouterr().out


def test_activation_that_creates_an_ambiguity_exits_nonzero(
    cli, monkeypatch, capsys, tmp_path
):
    """Two usable, equally specific globs deny every client they both match.
    That was silent: `shadowing_keys` skips usable keys, so the *expired*
    equally-specific case warned while the *usable* one — a guaranteed denial
    — said nothing. Caught here by resolving a real client name."""
    _replace_grants(tmp_path, TWO_GRANTS)
    installed(monkeypatch, "claude-code")
    run(cli, monkeypatch, "grant", "activate", "everything", "--client", "claude*")
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "grant", "activate", "everything", "--client", "*e-code")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "equally specific" in out
    assert "claude*" in out and "*e-code" in out


def test_a_bad_grants_file_explains_instead_of_tracebacking(
    cli, monkeypatch, capsys, tmp_path
):
    """`grant list` is what someone runs *because* something is wrong with
    grants.yaml. A timezone-less absolute `expires` parses, then raises
    `TypeError: can't compare offset-naive and offset-aware` from wherever it
    is next compared — since this release that includes `doctor` and this
    command. Refused at parse, reported here."""
    _replace_grants(tmp_path, TWO_GRANTS.replace('"+30d"', '"2027-01-01T00:00:00"'))
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "grant", "list")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "has no timezone" in out
    assert "grants.yaml" in out


def test_the_kill_switch_works_with_an_unparseable_grants_file(
    cli, monkeypatch, capsys, tmp_path
):
    """`deactivate` has never needed `grants.yaml` to parse, and gating it on
    that left one bad line making the emergency control unreachable while
    `active_grant.json` still declared full access — recoverable only by
    hand-editing YAML under pressure."""
    _replace_grants(tmp_path, TWO_GRANTS)
    run(cli, monkeypatch, "grant", "activate", "everything")
    _replace_grants(tmp_path, TWO_GRANTS.replace('"+30d"', '"2027-01-01T00:00:00"'))
    capsys.readouterr()
    run(cli, monkeypatch, "grant", "deactivate")
    assert "Every MCP call is now denied" in capsys.readouterr().out


def test_the_kill_switch_does_not_recommend_the_broadest_grant(
    cli, monkeypatch, capsys, tmp_path
):
    """`_default_grant_name` fell back to `everything` whenever the recorded
    name was null — which is exactly what `deactivate` writes. So pulling the
    switch on a narrow grant made `grant list` and `doctor` both answer "run:
    evoctx grant activate everything": the store's broadest grant, named, as
    the repair for a deliberate lockdown."""
    _replace_grants(tmp_path, TWO_GRANTS)
    run(cli, monkeypatch, "grant", "activate", "read-only")
    run(cli, monkeypatch, "grant", "deactivate")
    capsys.readouterr()
    run(cli, monkeypatch, "grant", "list")
    out = capsys.readouterr().out
    assert "Run: evoctx grant activate <name>" in out
    assert "activate everything" not in out


def test_client_flag_is_refused_on_actions_that_ignore_it(
    cli, monkeypatch, capsys, tmp_path
):
    """argparse hangs --client on the whole `grant` subcommand, so
    `grant upgrade --client cursor` parsed fine and silently did nothing with
    it — while reading exactly like "upgrade this client's grant"."""
    _replace_grants(tmp_path, TWO_GRANTS)
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "grant", "upgrade", "--client", "cursor*")
    assert exc.value.code == 1
    assert "--client applies to activate and deactivate" in capsys.readouterr().out


def test_grant_list_shows_a_client_that_is_denied(
    cli, monkeypatch, capsys, tmp_path
):
    """An entry can read healthy row-by-row and still deny every call — a glob
    key pointed at a grant whose own `client` glob excludes it. That is a
    property of a client, not of an entry, so no row can show it. `grant list`
    was green on a config `doctor` failed, and it is the surface people check
    first."""
    _replace_grants(tmp_path, TWO_GRANTS)
    installed(monkeypatch, "claude-code")
    with pytest.raises(SystemExit):
        run(cli, monkeypatch, "grant", "activate", "read-only", "--client", "claude*")
    capsys.readouterr()
    # write it anyway, the way a hand-edit would
    run(cli, monkeypatch, "grant", "activate", "everything", "--client", "claude*")
    capsys.readouterr()
    _replace_grants(tmp_path, TWO_GRANTS.replace(
        '  - name: everything\n    client: "*"', '  - name: everything\n    client: "cursor*"'))
    run(cli, monkeypatch, "grant", "list")
    out = capsys.readouterr().out
    assert "claude-code is denied every call" in out
    assert "run: evoctx grant activate" in out


def test_a_pre_existing_unrelated_denial_does_not_fail_the_activation(
    cli, monkeypatch, capsys, tmp_path
):
    """The gate reports the clients this key is about, not every denied one.
    Judging all of them made a pulled kill switch or a lapsed machine-wide
    grant fail an activation that was entirely correct — and this command's
    exit code is documented as meaning "what you wrote is usable"."""
    _replace_grants(tmp_path, TWO_GRANTS)
    installed(monkeypatch, "vscode")
    run(cli, monkeypatch, "grant", "deactivate")          # vscode now denied
    capsys.readouterr()
    run(cli, monkeypatch, "grant", "activate", "read-only", "--client", "cursor*")
    out = capsys.readouterr().out
    assert "now use grant 'read-only'" in out
    assert "denied" not in out


def test_the_printed_recovery_is_executed_and_fixes_the_state(
    cli, monkeypatch, no_clients_installed, capsys, tmp_path
):
    """Through the CLI, not the store API. Three rounds of review found the
    critical defect in a remediation *string*, and every one of them produced
    a correct-looking string — so the assertion has to be the state after
    running it, at the layer that prints it."""
    import json
    import shlex
    from datetime import datetime, timedelta, timezone
    _replace_grants(tmp_path, TWO_GRANTS)
    run(cli, monkeypatch, "grant", "activate", "read-only")
    capsys.readouterr()
    # backdate past its +8h, which is how a grant lapses in practice —
    # a `+0m` grant is unrenewable and would test the wrong thing
    path = tmp_path / "active_grant.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["activated_at"] = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    path.write_text(json.dumps(state), encoding="utf-8")
    # The store re-reads on mtime change. This write can land in the same
    # filesystem tick as the read the previous command just did, which leaves
    # the cache looking current and the grant looking unexpired — it passed
    # locally and failed on Windows CI, which is the definition of a race.
    # Editing the file behind the store's back is the test's own shortcut, so
    # invalidating is the test's own job.
    cli.grant_store._active_mtime = None

    run(cli, monkeypatch, "grant", "list")
    out = capsys.readouterr().out
    command = [ln for ln in out.splitlines() if ln.startswith("Run: ")][0][5:]
    assert "<" not in command, f"not runnable as printed: {command}"

    run(cli, monkeypatch, *shlex.split(command)[1:])
    capsys.readouterr()
    run(cli, monkeypatch, "grant", "list")
    assert "No active grant" not in capsys.readouterr().out


def test_a_grant_name_that_is_not_text_explains_instead_of_tracebacking(
    cli, monkeypatch, capsys, tmp_path
):
    """The parse guard proves the file is a JSON object and nothing about the
    values in it. `grants().get(["everything"])` raised `TypeError:
    unhashable type` out of every tool call — and out of `grant list` and
    `doctor`, the two commands someone runs to find out what is wrong."""
    import json
    (tmp_path / "active_grant.json").write_text(
        json.dumps({"name": ["everything"], "activated_at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8")
    # safe today (the fixture's fresh store leaves the cache invalidated), but
    # this is the same write-behind-the-cache shape that raced on Windows CI
    # one test above — pinned rather than left depending on fixture internals
    cli.grant_store._active_mtime = None
    run(cli, monkeypatch, "grant", "list")
    out = capsys.readouterr().out
    assert "not text" in out
    run(cli, monkeypatch, "grant", "activate", "everything")     # and repairs
    assert "Activated" in capsys.readouterr().out


def test_a_concrete_key_that_is_covered_says_nothing_extra(cli, monkeypatch, no_clients_installed, capsys, tmp_path):
    """The note above is for the case that cannot be checked. Printing it
    when the guard *did* run would train people to ignore it."""
    _replace_grants(tmp_path, TWO_GRANTS)
    run(cli, monkeypatch, "grant", "activate", "read-only", "--client", "cursor-1")
    out = capsys.readouterr().out
    assert "only covers clients matching" not in out


def test_help_command(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "help")
    out = capsys.readouterr().out
    assert "evoctx list-projects" in out
    assert "Examples:" in out


def test_version_subcommand(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "version")
    assert "evoctx" in capsys.readouterr().out


def test_version_flag(cli, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run(cli, monkeypatch, "--version")
    assert exc.value.code == 0
    assert "evoctx" in capsys.readouterr().out


def test_demo_seed(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "demo-seed")
    out = capsys.readouterr().out
    assert "demo-webapp" in out
    run(cli, monkeypatch, "search", "httpOnly")
    assert "JWT" in capsys.readouterr().out


def test_delete_by_id_force(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "add", "T1", "--text", "x")
    note_id = cli.db.list_notes()[0]["id"]
    run(cli, monkeypatch, "delete", note_id, "--force")
    assert "Deleted" in capsys.readouterr().out
    assert cli.db.get_note(note_id) is None


def test_delete_without_force_prompts_and_cancels(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "add", "T1", "--text", "x")
    note_id = cli.db.list_notes()[0]["id"]
    monkeypatch.setattr("builtins.input", lambda: "n")
    run(cli, monkeypatch, "delete", note_id)
    assert "Cancelled" in capsys.readouterr().out
    assert cli.db.get_note(note_id) is not None


def test_delete_requires_exactly_one_selector(cli, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        run(cli, monkeypatch, "delete")


def test_delete_project_force(cli, monkeypatch, capsys):
    run(cli, monkeypatch, "new-project", "p1", "--overview", "x")
    run(cli, monkeypatch, "delete-project", "p1", "--force")
    assert "Deleted project" in capsys.readouterr().out
    assert cli.db.get_project("p1") is None
