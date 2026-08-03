"""Per-client grant activation — `active_grant.json`'s `clients` map.

The feature exists because a client-scoped grant used to scope the wrong
thing: activating one with `client: "cursor*"` did not narrow cursor, it
denied every *other* client. So the natural multi-assistant setup — read-only
for one, full write for another — was unreachable.

Two properties carry most of the risk and are tested hardest here:

- **an expired per-client entry denies, and never falls back to the scalar.**
  Fallback would silently promote a client from its lapsed narrow grant to
  the machine-wide one, which is privilege escalation at the moment a
  restriction expires.
- **the file stays readable by 0.1.3.** It subscripts `activated_at`
  directly, so a 0.1.4 file without the scalar keys makes an older build
  raise on every call — and this store is the kind people sync.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from evoctx.grants import (
    Grant,
    GrantError,
    GrantStore,
    client_key_is_concrete,
    client_key_specificity,
    match_client_key,
)
from evoctx.policy import PolicyDenied, PolicyEngine


TWO_GRANTS = """\
grants:
  - name: everything
    client: "*"
    tools: ["*"]
    scope: {projects: ["*"], exclude_tags: []}
    expires: "+30d"
  - name: read-only
    client: "cursor*"
    tools: [list_projects, get_project, search_context, get_by_id, list_grants]
    scope: {projects: ["*"], exclude_tags: []}
    expires: "+8h"
  - name: lapsed
    client: "*"
    tools: ["*"]
    scope: {projects: ["*"], exclude_tags: []}
    expires: "+0m"
"""


# Any tag some grant might exclude. The set only has to be stable — the
# property compares two measurements of it, so what matters is that an
# exclusion added by a "repair" shows up as a difference.
ALL_TAGS = {"#decision", "#personal", "#finance"}


def write_grants(tmp_path, body: str):
    path = tmp_path / "grants.yaml"
    path.write_text(body, encoding="utf-8")
    mtime = time.time() + 2
    import os
    os.utime(path, (mtime, mtime))


@pytest.fixture
def store(tmp_path):
    gs = GrantStore(tmp_path)
    write_grants(tmp_path, TWO_GRANTS)
    return gs


def state_of(store) -> dict:
    return json.loads(store.active_path.read_text(encoding="utf-8"))


def run_recovery(store, command: str) -> None:
    """Execute a printed recovery command against `store`, as a user would.

    Every remediation string in this feature is tested by running it and
    checking the state afterwards, never by matching its text. Three round-2
    defects survived a full round of shape assertions: a command that deleted
    a different entry from the one it named, a second channel emitting a
    different command for the same state, and a three-way tie whose command
    left the denial in place. All three produce correct-looking strings.
    """
    import shlex
    parts = shlex.split(command)
    assert parts[:2] == ["evoctx", "grant"], f"not a grant command: {command}"
    assert "<" not in command, f"not runnable as printed: {command}"
    key = None
    if "--client" in parts:
        at = parts.index("--client")
        key, parts = parts[at + 1], parts[:at]
    if parts[2] == "activate":
        store.activate(parts[3], client_key=key)
    elif parts[2] == "deactivate":
        store.deactivate(client_key=key)
    else:
        raise AssertionError(f"unknown recovery action: {command}")


# ── key matching ─────────────────────────────────────────────────────────────

def test_exact_key_beats_every_glob():
    keys = ["cursor", "cursor*", "*"]
    assert match_client_key(keys, "cursor") == ("cursor", [])


def test_more_literal_characters_wins():
    assert match_client_key(["claude*", "*-code", "*"], "claude-code")[0] == "claude*"


def test_character_class_is_not_scored_as_literal_text():
    """`len(key.replace("*", ""))` would score `claude-code*` at 11 and the
    strictly tighter `claude-cod[ae]` at 14 — counting the brackets as text
    and then picking the looser key when they compete elsewhere. Only the
    metacharacter-excluding count orders these correctly."""
    assert client_key_specificity("claude-cod[ae]") == 12
    assert client_key_specificity("claude-code*") == 11
    assert match_client_key(["claude-cod[ae]", "claude-code*"], "claude-code")[0] \
        == "claude-cod[ae]"


def test_equally_specific_keys_are_ambiguous_not_first_wins():
    key, tied = match_client_key(["claude*", "*e-code"], "claude-code")
    assert key is None
    assert tied == ["*e-code", "claude*"]


def test_ambiguity_is_decided_against_the_client_not_between_globs():
    """`cursor*` and `vscode*` tie on specificity and can never both match
    anybody, so comparing globs in the abstract would refuse a setup that is
    perfectly well-defined."""
    assert match_client_key(["cursor*", "vscode*"], "cursor-1") == ("cursor*", [])


def test_matching_is_case_insensitive_at_both_tiers():
    assert match_client_key(["Cursor"], "cursor") == ("Cursor", [])
    assert match_client_key(["CURSOR*"], "cursor-1") == ("CURSOR*", [])


def test_two_keys_differing_only_by_case_are_ambiguous_not_first_wins():
    """The exact-match tier ties exactly as the glob tier does. Returning the
    first key walked past resolves a grant by JSON byte order, and the entry
    that loses may be the narrow one — a widening decided by file layout.
    `activate` refuses to create the second key, but this file is
    hand-editable, merged and synced, so both can exist without ever passing
    through that guard."""
    key, tied = match_client_key(["Cursor", "cursor"], "cursor")
    assert key is None
    assert tied == ["Cursor", "cursor"]
    # ...and the answer does not depend on which was written first
    assert match_client_key(["cursor", "Cursor"], "CURSOR") == (None, ["Cursor", "cursor"])


def test_closing_bracket_alone_is_not_a_glob():
    """fnmatch treats a lone `]` as an ordinary character, so a key
    containing one still names exactly one client."""
    assert client_key_is_concrete("weird]name")
    assert not client_key_is_concrete("cursor*")
    assert not client_key_is_concrete("cursor?")
    assert not client_key_is_concrete("cursor[12]")


def test_project_scope_is_case_sensitive_on_every_platform():
    """The opposite choice from `matches_client`, for the opposite reason.
    Both were on plain `fnmatch`, which normcases on Windows only. A client
    name is self-reported and folds; a project name is a store key — the
    store holds `Payments` and `payments` as two projects, so folding would
    let a scope glob reach one the grant does not name. On Windows it already
    did."""
    g = Grant(name="g", projects=["Payments"])
    assert g.allows_project("Payments")
    assert not g.allows_project("payments")


def test_grant_client_glob_casefolds_on_every_platform():
    """`fnmatch` normcases on Windows and not on Linux. Left unfixed, one
    synced store resolves a `Cursor` key to a `cursor*` grant that then
    passes this check on Windows and denies on Linux."""
    assert Grant(name="g", client="cursor*").matches_client("CURSOR-1")
    assert Grant(name="g", client="CURSOR*").matches_client("cursor-1")


# ── resolution ───────────────────────────────────────────────────────────────

def test_client_with_an_entry_resolves_to_it(store):
    store.activate("read-only", client_key="cursor*")
    active, _ = store.active_grant("cursor-1")
    assert active is not None
    assert active.grant.name == "read-only"
    assert active.client_key == "cursor*"


def test_client_without_an_entry_falls_back_to_the_machine_default(store):
    store.activate("read-only", client_key="cursor*")
    active, _ = store.active_grant("claude-code")
    assert active is not None
    assert active.grant.name == "everything"
    assert active.client_key is None


def test_no_client_answers_for_the_machine_default(store):
    store.activate("read-only", client_key="cursor*")
    active, _ = store.active_grant()
    assert active is not None and active.grant.name == "everything"


def test_expired_entry_denies_and_does_not_fall_back(store):
    """R12. Fallback here would promote cursor from a lapsed `read-only` to
    the machine-wide `everything` — a widening triggered by an expiry, which
    is the opposite of what expiring a narrow grant is for."""
    store.activate("lapsed", client_key="cursor*")
    active, reason = store.active_grant("cursor-1")
    assert active is None
    assert "expired" in reason
    assert "everything" not in reason
    # and the machine default is genuinely usable, so this is a refusal to
    # fall back rather than there being nothing to fall back to
    assert store.active_grant()[0].grant.name == "everything"


def test_expired_entry_recovery_names_the_key_not_the_client(store):
    """An entry under `cursor*` denying client `cursor-1` must not send the
    user to `--client cursor-1`: removal matches keys exactly, so that
    command silently matches nothing. And it names the entry's own grant,
    because re-activating that grant is what resets this entry's clock —
    `<name>` would be a command nobody can run."""
    store.activate("lapsed", client_key="cursor*")
    assert store.recovery_command("cursor-1") == \
        'evoctx grant activate lapsed --client "cursor*"'


def test_recovery_falls_back_to_a_placeholder_only_when_nothing_can_be_named(store, tmp_path):
    """The recorded grant is gone from grants.yaml, so there is genuinely
    nothing to re-activate and the user has to choose.

    Specifically it must not fall back to the store's default grant the way
    the machine-wide branch does: that names a grant this key was never
    pointed at, and the default is the broadest one — a widening printed as
    a repair."""
    store.activate("lapsed", client_key="cursor*")
    write_grants(tmp_path, TWO_GRANTS.replace("  - name: lapsed", "  - name: gone"))
    assert store.recovery_command("cursor-1") == \
        'evoctx grant activate <name> --client "cursor*"'


def test_a_machine_wide_grant_that_excludes_the_client_is_not_the_fix(store, tmp_path):
    """The other half of the same rule, and the branch no test covered: the
    active grant carries a `client` glob that excludes this caller, and there
    is no per-client entry at all. Naming that grant produced a *runnable*
    command — no angle brackets, so nothing marked it as needing a decision —
    which provably changed nothing and printed the identical denial again."""
    store.activate("read-only")                 # client: "cursor*"
    assert store.active_grant("claude-code")[0] is not None
    assert store.recovery_command("claude-code") == "evoctx grant activate <name>"


def test_reason_carries_no_command_so_callers_cannot_print_two(store):
    """`doctor` and `grant list` append the recovery themselves. When the
    reason carried its own copy they printed lines ending
    "...activate everything — run: evoctx grant activate everything"."""
    store.activate("lapsed", client_key="cursor*")
    _, reason = store.active_grant("cursor-1")
    assert "evoctx" not in reason


def test_ambiguous_client_is_denied_and_both_keys_named(store):
    store.activate("everything", client_key="claude*")
    store.activate("everything", client_key="*e-code")
    active, reason = store.active_grant("claude-code")
    assert active is None
    assert "claude*" in reason and "*e-code" in reason
    assert "deactivate --client" in store.recovery_command("claude-code")


def test_a_hand_written_case_clash_denies_rather_than_widening(store, db):
    """The end-to-end shape of the tier-1 tie, through the gate that actually
    grants access. Reproduced before the fix: a map holding both `Cursor` ->
    everything and `cursor` -> read-only gave client `cursor` **write**
    access, and swapping the two lines in the JSON file reversed the answer."""
    store.activate("everything", client_key="Cursor")
    state = state_of(store)
    state["clients"]["cursor"] = {"name": "read-only",
                                  "activated_at": state["activated_at"]}
    store.active_path.write_text(json.dumps(state), encoding="utf-8")
    store._active_mtime = -1.0
    engine = PolicyEngine(db, store)
    for spelling in ("cursor", "Cursor", "CURSOR"):
        with pytest.raises(PolicyDenied, match="equally specific"):
            engine.check(spelling, "write_note")


def test_malformed_clients_value_does_not_break_activation(store):
    """`grant activate` is the command someone runs *because* something is
    wrong with their setup. Reading `state["clients"]` directly raised a
    ValueError out of `dict()` on a hand-edited list, from the one command
    that could have repaired it."""
    store.active_path.write_text(
        json.dumps({"name": "everything", "activated_at": "2026-01-01T00:00:00+00:00",
                    "clients": ["cursor"]}), encoding="utf-8")
    store._active_mtime = -1.0
    assert store.client_entries() == {}
    store.activate("everything")
    assert "clients" not in state_of(store)     # normalised away, not preserved
    store.activate("read-only", client_key="cursor*")
    assert list(store.client_entries()) == ["cursor*"]


def test_malformed_activation_time_explains_instead_of_raising(store):
    store.activate("everything", client_key="cursor*")
    state = state_of(store)
    del state["clients"]["cursor*"]["activated_at"]
    store.active_path.write_text(json.dumps(state), encoding="utf-8")
    store._active_mtime = -1.0
    active, reason = store.active_grant("cursor-1")
    assert active is None
    assert "activation time" in reason


# ── writes ───────────────────────────────────────────────────────────────────

def test_machine_wide_activate_preserves_the_map(store):
    """R11's own half. 0.1.3 wrote a fresh two-key file over the whole thing;
    keeping that would make every routine `grant activate` a silent wipe of
    the per-client setup."""
    store.activate("read-only", client_key="cursor*")
    store.activate("everything")
    state = state_of(store)
    assert state["name"] == "everything"
    assert state["clients"]["cursor*"]["name"] == "read-only"


def test_per_client_activate_keeps_the_scalar_keys_present(store):
    """R10. 0.1.3 reads `state["activated_at"]` by subscript, so dropping it
    makes an older build raise on every call. Asserts *shape*, not value —
    `deactivate` legitimately writes a null name and 0.1.3 reads that fine."""
    store.deactivate()
    store.activate("read-only", client_key="cursor*")
    state = state_of(store)
    assert "name" in state and "activated_at" in state
    assert state["clients"]["cursor*"]["name"] == "read-only"


def test_each_entry_expires_on_its_own_clock(store):
    store.activate("everything")
    store.activate("read-only", client_key="cursor*")
    state = state_of(store)
    assert state["clients"]["cursor*"]["activated_at"] != state["activated_at"]


def test_activate_refuses_a_key_differing_only_by_case(store):
    store.activate("read-only", client_key="cursor*")
    with pytest.raises(GrantError, match="only by case"):
        store.activate("read-only", client_key="CURSOR*")
    assert list(store.client_entries()) == ["cursor*"]


def test_activate_refuses_a_grant_that_cannot_cover_a_concrete_key(store):
    with pytest.raises(GrantError, match="does not match"):
        store.activate("read-only", client_key="vscode")


def test_activate_skips_that_guard_when_the_key_is_a_glob(store):
    """Both sides are globs, and glob-vs-glob intersection is automata work:
    grant `client: "cursor*"` with key `cur*` covers every client either
    could mean, but no plain fnmatch says so. A real mismatch is caught per
    call instead."""
    store.activate("read-only", client_key="cur*")
    assert store.client_entries()["cur*"]["name"] == "read-only"


def test_a_concrete_key_whose_grant_excludes_it_is_not_usable(store):
    """Expiry is not the only way an entry fails to serve. An entry pointed
    at a grant carrying a narrower `client` glob resolves fine and then
    denies every call — so if "usable" meant only "unexpired", the CLI would
    exit 0, `grant list` would print an expiry and `doctor` would pass, on a
    client that can never make a call."""
    store.activate("everything", client_key="vscode")
    state = state_of(store)
    state["clients"]["vscode"]["name"] = "read-only"      # client: "cursor*"
    store.active_path.write_text(json.dumps(state), encoding="utf-8")
    store._active_mtime = -1.0
    usable, detail = store.entry_usable("vscode")
    assert usable is False
    assert "covers client 'cursor*'" in detail


def test_activate_refuses_an_empty_client_key(store):
    """It can never match a client — the server reports "unknown" for a
    nameless one — and `deactivate ""` used to fall through to the kill
    switch, so the entry was unremovable by its own key as well."""
    with pytest.raises(GrantError, match="empty key"):
        store.activate("everything", client_key="")
    assert store.client_entries() == {}


def test_bare_deactivate_clears_the_map_too(store):
    """R14. The kill switch says every call is denied. Merge-preserving the
    map — the obvious symmetry with activate — would leave every client that
    has an entry fully active after the user pulled it."""
    store.activate("read-only", client_key="cursor*")
    store.deactivate()
    assert store.client_entries() == {}
    assert store.active_grant("cursor-1")[0] is None


def test_deactivate_one_key_leaves_the_rest(store):
    store.activate("read-only", client_key="cursor*")
    store.activate("everything", client_key="claude-code")
    store.deactivate(client_key="cursor*")
    assert list(store.client_entries()) == ["claude-code"]
    # and that client now falls back rather than being denied
    assert store.active_grant("cursor-1")[0].grant.name == "everything"


def test_deactivate_matches_a_key_exactly_not_as_a_glob(store):
    """`deactivate --client "*"` must not delete every entry while looking
    like it named one."""
    store.activate("read-only", client_key="cursor*")
    with pytest.raises(GrantError, match="no per-client grant under key"):
        store.deactivate(client_key="*")
    assert list(store.client_entries()) == ["cursor*"]


def test_deactivate_unknown_key_raises_rather_than_passing_silently(store):
    with pytest.raises(GrantError, match="no per-client grant under key"):
        store.deactivate(client_key="nosuch")


def test_activation_is_visible_to_the_process_that_performed_it(store):
    """`activate` now reads the file before writing it. Filesystem mtime
    granularity is coarse enough that a read and a write in the same tick
    would leave the cache looking current, so the activation would be ignored
    by the very process that made it."""
    store.active_grant("cursor-1")            # warm the cache
    store.activate("read-only", client_key="cursor*")
    assert store.active_grant("cursor-1")[0].grant.name == "read-only"


# ── the printed recovery, executed ───────────────────────────────────────────

def test_recovery_for_an_expired_entry_actually_restores_the_client(store):
    """A `+8h` entry activated nine hours ago — the ordinary way one lapses.
    Naming the entry's own grant is what makes the printed command work:
    re-activating it resets that entry's clock."""
    from datetime import datetime, timedelta, timezone
    store.activate("read-only", client_key="cursor*")
    state = state_of(store)
    state["clients"]["cursor*"]["activated_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    store.active_path.write_text(json.dumps(state), encoding="utf-8")
    store._active_mtime = -1.0
    assert store.active_grant("cursor-1")[0] is None

    run_recovery(store, store.recovery_command("cursor-1"))
    active, _ = store.active_grant("cursor-1")
    assert active is not None
    assert active.grant.name == "read-only" and active.client_key == "cursor*"


def test_recovery_for_a_coverage_mismatch_never_widens(store):
    """This test used to assert that the recovery *restored* the client, and
    passed because the recovery removed the entry — dropping the client to
    the machine-wide `everything`. It certified a widening and called it a
    repair.

    `server.py`'s instructions tell an assistant to relay the command
    verbatim, so a denied assistant would have been honestly emitting a
    request for full access. The entry is not the mistake; the pairing is.
    """
    store.activate("everything")
    store.activate("read-only", client_key="claude*")     # grant is cursor-only
    assert store.denied_clients(["claude-code"])
    command = store.recovery_command("claude-code")
    assert "deactivate" not in command
    assert command == 'evoctx grant activate <name> --client "claude*"'
    # a placeholder, so it is not runnable as printed — and the reason the
    # user sets it against says what they need in order to fill it in
    (_name, why), = store.denied_clients(["claude-code"])
    assert "covers 'cursor*', not 'claude-code'" in why


def test_recovery_for_a_machine_wide_expiry_names_the_grant_that_lapsed(store, tmp_path):
    """A hardcoded `activate everything` told a user whose narrow grant
    expired to escalate to full access — and hard-failed on a store with no
    grant of that name."""
    write_grants(tmp_path, TWO_GRANTS.replace("  - name: everything", "  - name: work"))
    store.activate("lapsed")
    assert store.recovery_command() == "evoctx grant activate lapsed"
    run_recovery(store, store.recovery_command())
    # `lapsed` expires immediately, so re-activating cannot help — but the
    # command must at least name a grant that exists, which `everything` no
    # longer does here
    assert "everything" not in store.recovery_command()


def test_recovery_for_a_tie_refuses_to_choose_an_entry_to_delete(store):
    """No single command fixes this: which of the tied entries to drop is the
    user's call — one may be narrow and one wide — and a three-way tie leaves
    the denial standing after any one removal. So it prints a placeholder and
    the reason enumerates the keys.

    The command it used to print deleted `tied[0]`, sorted; `deactivate`
    matched keys by casefold in *file* order. On a `Cursor`/`cursor` pair
    those disagree, so the product's own remediation removed the entry it did
    not name and promoted the client from read-only to everything."""
    store.activate("everything", client_key="claude*")
    store.activate("everything", client_key="*e-code")
    command = store.recovery_command("claude-code")
    assert command == 'evoctx grant deactivate --client "<key>"'
    _, reason = store.active_grant("claude-code")
    assert "claude*" in reason and "*e-code" in reason


def test_deactivate_removes_the_key_named_not_the_one_written_first(store):
    """The other half of that defect. A hand-edited file can hold two keys
    differing only by case; removal must take the one spelled, not the one
    the JSON happens to list first."""
    store.activate("everything", client_key="Cursor")
    state = state_of(store)
    state["clients"]["cursor"] = {"name": "read-only",
                                  "activated_at": state["activated_at"]}
    store.active_path.write_text(json.dumps(state), encoding="utf-8")
    store._active_mtime = -1.0
    store.deactivate(client_key="Cursor")
    assert list(store.client_entries()) == ["cursor"]
    assert store.active_grant("cursor")[0].grant.name == "read-only"


def test_deactivate_takes_the_spelling_exactly_including_case(store):
    """The one place in this feature that does not fold case, and the reason
    is that this one deletes. Folding turns a case typo into the silent
    removal of a real access rule — and if the removed entry was the narrow
    one, the client is promoted by a typo."""
    store.activate("read-only", client_key="cursor*")
    with pytest.raises(GrantError, match="did you mean"):
        store.deactivate(client_key="CURSOR*")
    assert list(store.client_entries()) == ["cursor*"]


def test_an_unreadable_file_denies_instead_of_raising(store, capsys):
    """It used to raise JSONDecodeError out of `PolicyEngine.check`, so a
    truncated file failed every tool call with a traceback — including the
    `grant activate` that would have repaired it."""
    store.active_path.write_text("", encoding="utf-8")
    store._active_mtime = -1.0
    active, reason = store.active_grant("cursor-1")
    assert active is None and "no grant is active" in reason
    assert "cannot read" in capsys.readouterr().err
    store.activate("everything")                    # and the repair path works
    assert store.active_grant("cursor-1")[0] is not None


def test_a_top_level_json_array_denies_instead_of_raising(store, capsys):
    store.active_path.write_text("[1, 2, 3]", encoding="utf-8")
    store._active_mtime = -1.0
    assert store.active_grant("cursor-1")[0] is None
    assert store.client_entries() == {}
    assert "does not contain a JSON object" in capsys.readouterr().err
    store.activate("read-only", client_key="cursor*")
    assert store.active_grant("cursor-1")[0].grant.name == "read-only"


def test_no_remediation_this_feature_prints_can_ever_widen_a_client(store, tmp_path):
    """The invariant, tested as one property instead of one case at a time.

    Three consecutive review rounds each found the same defect in a different
    surface: a remediation string whose effect was to *remove* a narrow entry,
    handing its client back to the wider machine-wide grant. Each round fixed
    the surface it was found in, and the next round found it in the next one —
    because four places were building that text. `recovery_command` and
    `widen_command` are now the only producers, and this asserts the property
    they exist to hold: run what we print, and nobody gains a tool.

    `server.py` tells assistants to relay these commands verbatim, so a
    widening one is a denied assistant honestly asking for the access it was
    just refused.
    """
    def access_of(g):
        """Everything a grant confers, not only its tool list — a widening of
        `projects`, `exclude_tags` or `redactions` is just as much a widening,
        and measuring `effective_tools()` alone cannot see one."""
        return ({("tool", t) for t in g.effective_tools()}
                | {("project", p) for p in g.projects}
                | {("readable", t) for t in ALL_TAGS - set(g.exclude_tags)}
                | {("unredacted",) if not g.redactions else ("redacted",)})

    def access_for(client):
        active, _ = store.active_grant(client)
        if active is None or not active.grant.matches_client(client):
            return set()
        return access_of(active.grant)

    def configured_ceiling(client):
        """The access the user **configured** for this client, whether or not
        it currently works.

        The invariant cannot be "no more than before": before is often
        *nothing*, because the client is denied — that is why a remediation is
        being printed — and renewing a lapsed entry legitimately restores the
        access the user chose. Measured against `before`, the correct repair
        looks identical to the widening.

        So the ceiling is what is written down for that client: renewing it
        stays inside, and falling back to a wider grant does not. That is the
        difference between "the restriction is back in force" and "the
        restriction is gone", which is the whole distinction five review
        rounds kept turning on.
        """
        entries = store.client_entries()
        key, tied = match_client_key(entries, client)
        if tied:
            names = [entries[k].get("name") for k in tied]
        elif key is not None:
            names = [entries[key].get("name")]
        else:
            names = [store._active_state().get("name")]
        out = set()
        for n in names:
            g = store.grants().get(n) if isinstance(n, str) else None
            if g is not None:
                out |= access_of(g)
        return out

    # **Grants that can actually be repaired.** Every scenario used to be
    # built on `lapsed` (`expires: "+0m"`), which can never become usable — so
    # the invariant held vacuously: the commands restored nothing in states
    # where restoring nothing was correct. `stale` expires in 8h and is made
    # expired by backdating, which is how a grant lapses in practice and the
    # only shape where a repair grants anything at all.
    def backdate(key=None):
        state = state_of(store)
        target = state["clients"][key] if key else state
        target["activated_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
        store.active_path.write_text(json.dumps(state), encoding="utf-8")
        store._active_mtime = -1.0

    scenarios = [
        ("expired entry", lambda: (store.activate("read-only", client_key="cursor*"),
                                   backdate("cursor*"))),
        ("expired machine-wide grant", lambda: (store.activate("read-only"),
                                                backdate())),
        ("grant excludes the key's clients",
         lambda: store.activate("read-only", client_key="claude*")),
        ("machine-wide grant excludes the caller",
         lambda: store.activate("read-only")),
        ("kill switch pulled", lambda: store.deactivate()),
        ("recorded grant is gone", lambda: (
            store.activate("read-only", client_key="cursor*"),
            write_grants(tmp_path, TWO_GRANTS.replace("  - name: read-only",
                                                      "  - name: renamed")))),
        ("ambiguous keys", lambda: (
            store.activate("everything", client_key="claude*"),
            store.activate("everything", client_key="*e-code"))),
    ]
    for label, setup in scenarios:
        store.deactivate()
        write_grants(tmp_path, TWO_GRANTS)
        # A machine-wide grant WIDER than the entries, which is the setup the
        # whole defect class needs: with the two equal, removing an entry is
        # undetectable because there is nothing wider to fall back to.
        store.activate("everything")
        setup()
        for client in ("cursor-1", "claude-code"):
            before = access_for(client)
            ceiling = configured_ceiling(client) | before
            # `recovery_command(None)` too — the bare branch had its own
            # `everything` fallback, and a client-only sweep never called it
            for command in (store.recovery_command(client),
                            store.recovery_command(),
                            store.widen_command(client)):
                if "<" in command:
                    continue          # a placeholder is a decision, not an act
                snapshot = store.active_state_snapshot()
                run_recovery(store, command)
                after = access_for(client)
                store.restore_active(snapshot)
                assert not (after - ceiling), (
                    f"{label}: running {command!r} for {client!r} granted "
                    f"{sorted(after - ceiling)} — beyond what was configured "
                    f"for it"
                )


# ── usability and shadowing ──────────────────────────────────────────────────

def test_entry_usable_judges_the_entry_not_the_scalar(store):
    """`activate --client` knows a key, which may be a glob, so it cannot ask
    "what does client X resolve to". Checking the scalar instead would report
    success for an activation that denies every call from that client."""
    store.activate("everything")              # machine default: usable
    store.activate("lapsed", client_key="cursor*")
    assert store.entry_usable("cursor*")[0] is False
    assert store.active_grant()[0] is not None


def test_a_concrete_key_cannot_be_shadowed(store):
    store.activate("lapsed", client_key="cursor*")
    assert store.shadowing_keys("cursor-1") == ([], [])


def test_an_expired_exact_key_blocks_a_new_glob(store):
    """Rule-1 shadowing, and it is decidable: the exact key wins for the
    client it names, so that client stays denied however usable the new glob
    entry is."""
    store.activate("lapsed", client_key="cursor")
    blocking, _ = store.shadowing_keys("cursor*")
    assert [k for k, _why, _ok in blocking] == ["cursor"]
    # the reason travels with the key: these are no longer all expiries, and
    # "renew it" is wrong advice for a key whose grant excludes the client
    assert "expired" in blocking[0][1]
    # unusable: its client is denied and the new entry cannot reach them, so
    # the caller fails rather than warns
    assert blocking[0][2] is False


def test_an_expired_equally_specific_glob_only_warns(store):
    """Rule-2 shadowing is the glob-vs-glob intersection `match_client_key`
    declines at rule 1, so this over-approximates and says so."""
    store.activate("lapsed", client_key="cursor-*")
    blocking, warning = store.shadowing_keys("cur*")
    assert blocking == []
    assert [k for k, _why in warning] == ["cursor-*"]


def test_an_entry_its_grant_excludes_is_reported_as_blocking_not_expired(store):
    """`entry_usable` rejects more than expiries now, so the reason has to
    travel with the key — the CLI used to file every one of these under
    "expired keys ... remove or renew", and renewing an entry whose grant
    excludes the client rewrites the same exclusion."""
    store.activate("everything", client_key="vscode")
    state = state_of(store)
    state["clients"]["vscode"]["name"] = "read-only"       # client: "cursor*"
    store.active_path.write_text(json.dumps(state), encoding="utf-8")
    store._active_mtime = -1.0
    blocking, _ = store.shadowing_keys("vscode*")
    assert [k for k, _why, _ok in blocking] == ["vscode"]
    assert "covers client 'cursor*'" in blocking[0][1]
    assert "expired" not in blocking[0][1]
    assert blocking[0][2] is False


def test_a_usable_exact_key_shadows_hardest_of_all(store):
    """This test used to assert `([], [])` and certify the hole.

    An expired shadow only leaves its client denied. A **usable** one leaves
    them on whatever access it already grants — so `activate read-only
    --client "cursor*"` over an existing `cursor -> everything` reported
    nothing and exited 0, while client `cursor` kept every tool. The one
    outcome where the user believes they narrowed something and did not.
    """
    store.activate("everything", client_key="cursor")
    blocking, _ = store.shadowing_keys("cursor*")
    assert [k for k, _why, _ok in blocking] == ["cursor"]
    assert "everything" in blocking[0][1]
    # usable: the documented precedence rule working, so the caller warns
    assert blocking[0][2] is True


def test_a_concrete_key_outside_the_new_glob_is_not_shadowing(store):
    """It only wins for the client it names, and this glob is not about that
    client — warning on it would train people to ignore the check."""
    store.activate("everything", client_key="vscode")
    assert store.shadowing_keys("cursor*") == ([], [])


# ── policy ───────────────────────────────────────────────────────────────────

@pytest.fixture
def engine(db, tmp_path):
    gs = GrantStore(tmp_path)
    write_grants(tmp_path, TWO_GRANTS)
    return PolicyEngine(db, gs)


def test_two_clients_get_two_different_grants(engine):
    """The point of the release: this was impossible before, because one
    grant was active machine-wide."""
    engine.grants.activate("everything")
    engine.grants.activate("read-only", client_key="cursor*")
    engine.check("claude-code", "write_note")
    with pytest.raises(PolicyDenied, match="does not allow write_note"):
        engine.check("cursor-1", "write_note")
    engine.check("cursor-1", "search_context")


def test_denial_from_an_expired_entry_names_the_per_client_fix(engine):
    engine.grants.activate("lapsed", client_key="cursor*")
    with pytest.raises(PolicyDenied) as e:
        engine.check("cursor-1", "search_context")
    assert '--client "cursor*"' in str(e.value)
    assert "activate everything" not in str(e.value)


def test_describe_answers_for_this_client_not_the_machine_default(engine):
    """`list_grants()` exists to answer "may I call X?". Answering it from
    the machine-wide grant while the caller's real calls resolve against a
    narrower entry is the one wrong answer this tool cannot afford."""
    engine.grants.activate("everything")
    engine.grants.activate("read-only", client_key="cursor*")
    assert engine.describe("cursor-1")["active_grant"] == "read-only"
    assert engine.describe("cursor-1")["client_key"] == "cursor*"
    assert "write_note" not in engine.describe("cursor-1")["allowed_tools"]
    assert engine.describe("claude-code")["active_grant"] == "everything"


def test_describe_reports_no_access_when_the_grant_excludes_this_client(engine):
    """`read-only` carries `client: "cursor*"`. Pointing key `claude*` at it
    is allowed — two globs cannot be compared up front — and every claude
    call is then denied. Listing that grant's tools would answer "may I call
    X?" with the tools of a grant that refuses X."""
    engine.grants.activate("read-only", client_key="claude*")
    described = engine.describe("claude-code")
    assert described["active_grant"] is None
    assert "not 'claude-code'" in described["reason"]
    assert "allowed_tools" not in described
    with pytest.raises(PolicyDenied):
        engine.check("claude-code", "list_projects")
    # the fix repoints the key. Re-activating the same grant would rewrite
    # the same exclusion, and removing the entry would drop this client to
    # the wider machine-wide grant — a widening as the remedy for a
    # misconfigured narrowing.
    assert described["hint"] == 'evoctx grant activate <name> --client "claude*"'


def test_describe_hint_is_the_command_for_this_client(engine):
    """The generic `grant activate <name>` it used to return would send the
    user to the machine-wide scalar, which changes nothing for the client
    that was denied."""
    engine.grants.activate("lapsed", client_key="cursor*")
    described = engine.describe("cursor-1")
    assert described["hint"] == 'evoctx grant activate lapsed --client "cursor*"'
    with pytest.raises(PolicyDenied) as e:
        engine.check("cursor-1", "search_context")
    assert described["hint"] in str(e.value)     # one command, both channels


def test_denial_is_audited_against_the_grant_that_actually_refused(db, tmp_path):
    """Not display: a per-client denial logged under the machine default's
    name is a record of the wrong grant refusing."""
    from evoctx.audit import AuditWriter, query_audit
    gs = GrantStore(tmp_path)
    write_grants(tmp_path, TWO_GRANTS)
    engine = PolicyEngine(db, gs, AuditWriter(db.conn, "conn-1"))
    gs.activate("everything")
    gs.activate("read-only", client_key="cursor*")
    with pytest.raises(PolicyDenied):
        engine.t_write_note("cursor-1", "t", "c", None, None, None)
    entry = query_audit(db.conn, client="cursor-1")[0]
    assert entry["action"] == "denied"
    assert entry["grant"] == "read-only"
