"""The reminder rule itself — `holds.reminder_state`, with `now` fixed.

Every boundary is tested on both sides, one microsecond apart: a rule whose
edges are only tested from one side can move them without a test noticing.
"""
import math
from datetime import datetime, timedelta, timezone

import pytest

from evoctx.holds import (
    DUE_LAPSED,
    DUE_UNHELD,
    HELD,
    QUIET,
    SKIP,
    format_hours,
    hold_view,
    reminder_state,
    validate_hold_hours,
)

NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
US = timedelta(microseconds=1)


def at(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


def state(started, held=None, hours=None, notes=()):
    return reminder_state(started_at=started, held_at=held, hold_hours=hours,
                          note_stamps=list(notes), now=NOW)


# ── unheld ────────────────────────────────────────────────────────────────────

def test_unheld_is_due_through_exactly_12h_and_quiet_after():
    assert state(at(timedelta(hours=12))).state == DUE_UNHELD
    assert state(at(timedelta(hours=12) + US)).state == QUIET


# ── held ──────────────────────────────────────────────────────────────────────

def test_held_is_quiet_through_exactly_its_hours_then_lapsed():
    two = timedelta(hours=2)
    assert state(at(two), at(two), 2).state == HELD
    lapsed = state(at(two + US), at(two + US), 2)
    assert lapsed.state == DUE_LAPSED
    assert lapsed.idle_hours == pytest.approx(2.0)


def test_a_lapsed_hold_is_due_until_hours_plus_12h_then_quiet():
    fourteen = timedelta(hours=14)
    assert state(at(fourteen), at(fourteen), 2).state == DUE_LAPSED
    assert state(at(fourteen + US), at(fourteen + US), 2).state == QUIET


def test_an_old_held_session_with_a_recent_note_is_held():
    """The start-age limit does not apply to a held session, and a note is
    activity: three days old, held, a note thirty minutes ago — still held."""
    three_days = timedelta(days=3)
    r = state(at(three_days), at(three_days), 2, [at(timedelta(minutes=30))])
    assert r.state == HELD


def test_an_old_held_session_gone_idle_is_flagged_not_silenced():
    """Started 48h ago, held, idle 3h on a 2h hold: the start age would have
    silenced it as an unheld session. A hold must end in a reminder."""
    r = state(at(timedelta(hours=48)), at(timedelta(hours=3)), 2)
    assert r.state == DUE_LAPSED


def test_future_stamps_count_up_to_the_tolerance_and_not_beyond():
    held = at(timedelta(hours=3))
    inside = (NOW + timedelta(minutes=5)).isoformat()
    beyond = (NOW + timedelta(minutes=5) + US).isoformat()
    r = state(held, held, 2, [inside])
    assert r.state == HELD and r.last_activity == NOW + timedelta(minutes=5)
    r = state(held, held, 2, [beyond])
    assert r.state == DUE_LAPSED and r.last_activity == NOW - timedelta(hours=3)


def test_a_future_dated_note_does_not_hide_an_older_real_one():
    """Clock skew on a synced machine: the newest stamp is a day ahead and is
    discarded — the real note before it still counts."""
    held = at(timedelta(hours=3))
    r = state(held, held, 2, [at(timedelta(minutes=30)),
                              (NOW + timedelta(days=1)).isoformat()])
    assert r.state == HELD


@pytest.mark.parametrize("hours", [0, -1, 24.0001, math.nan, math.inf, True, "2", None])
def test_an_unusable_hold_is_judged_as_unheld_never_skipped(hours):
    """Skipped would mean silent. Unusable values fall back to the start-age
    rule, which still reminds about a fresh session."""
    held = at(timedelta(minutes=1))
    assert state(at(timedelta(hours=1)), held, hours).state == DUE_UNHELD
    assert state(at(timedelta(hours=13)), held, hours).state == QUIET
    view = hold_view(open_=True, started_at=at(timedelta(hours=1)), held_at=held,
                     hold_hours=hours, note_stamps=[], now=NOW)
    assert view is not None and view["hours"] is None and view["active"] is False


def test_an_unreadable_held_at_is_judged_as_unheld():
    assert state(at(timedelta(hours=1)), "garbage", 2).state == DUE_UNHELD


def test_an_unreadable_started_at_is_skipped():
    """The one row the rule cannot judge at all."""
    held = at(timedelta(minutes=1))
    assert state("not-a-date", held, 2, [held]).state == SKIP


def test_naive_and_offset_stamps_are_read_as_the_same_instant():
    naive = (NOW - timedelta(hours=12)).replace(tzinfo=None).isoformat()
    offset = (NOW - timedelta(hours=12)).astimezone(timezone(timedelta(hours=2))).isoformat()
    assert state(naive).state == DUE_UNHELD
    assert state(offset).state == DUE_UNHELD


# ── hours ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ok", [24, 24.0, 1, 0.5, 1e-9])
def test_validate_accepts_the_range(ok):
    assert validate_hold_hours(ok) == float(ok)


@pytest.mark.parametrize("bad", [0, -0.0, -1, 24.0001, math.nan, math.inf, -math.inf,
                                 True, False, "2", None])
def test_validate_refuses_everything_else_naming_the_range(bad):
    with pytest.raises(ValueError) as exc:
        validate_hold_hours(bad)
    assert "at most 24h" in str(exc.value)


def test_format_hours_never_prints_a_trailing_zero_or_zero_hours():
    assert format_hours(2.0) == "2h"
    assert format_hours(0.5) == "0.5h"
    assert format_hours(0.04) == "0.04h"


# ── the hold block ────────────────────────────────────────────────────────────

def test_hold_view_is_none_for_a_session_never_held():
    assert hold_view(open_=True, started_at=at(timedelta(hours=1)), held_at=None,
                     hold_hours=None, note_stamps=[], now=NOW) is None


def test_hold_view_is_never_active_for_a_closed_session():
    held = at(timedelta(minutes=1))
    view = hold_view(open_=False, started_at=held, held_at=held, hold_hours=2,
                     note_stamps=[], now=NOW)
    assert view is not None and view["active"] is False and view["hours"] == 2.0