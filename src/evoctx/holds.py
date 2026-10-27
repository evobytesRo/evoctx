"""When the Stop hook reminds about an open session, and what a hold is.

The Stop hook runs at the end of every assistant turn, so a session someone
deliberately keeps open across many turns would be flagged at every one of
them. A hold says "this is still being worked in": a held session stays quiet
while it shows activity, and is flagged again once it has been idle for longer
than its hold.

One rule, here, used by the hook and by every response that reports a hold
(`hold.active`). Two copies of it — say SQL date arithmetic in the hook and
Python in the responses — are how the hook and `recent_sessions()` would come
to disagree about the same session.

Pure: imports only the standard library, so db, policy and cli can all use it
without an import cycle.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: Idle window a hold gets when the caller names none.
HOLD_DEFAULT_HOURS = 2.0
#: Longest idle window a hold may ask for. Longer than an overnight slice is a
#: session that should be closed and a new one started.
HOLD_MAX_HOURS = 24.0
#: How long the reminder runs once a session is due: from its start for an
#: unheld session, from the moment its hold lapsed for a held one. After that
#: the session is treated as abandoned and the reminder goes quiet, so a
#: session forgotten weeks ago does not nag every window forever.
REMINDER_WINDOW_HOURS = 12.0
#: Stamps further than this in the future are not counted as activity. A
#: clock-skewed machine writing to a synced store would otherwise keep a hold
#: quiet for the skew plus the hold.
FUTURE_TOLERANCE = timedelta(minutes=5)

QUIET = "quiet"            # nothing to say about this session
HELD = "held"              # held, and active within its hold
DUE_UNHELD = "due-unheld"  # unheld, within the reminder window of its start
DUE_LAPSED = "due-lapsed"  # held, idle past its hold, within the reminder window
SKIP = "skip"              # started_at unreadable: nothing can be judged


def parse_utc(value: object) -> datetime | None:
    """An ISO timestamp as an aware UTC datetime, or None if it is not one.

    Naive strings are read as UTC: every writer stores `_now()`, which is
    aware UTC, and a naive value can only come from a store written by hand.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def validate_hold_hours(value: object) -> float:
    """The hold window as a float, or ValueError naming the accepted range.

    Refused rather than clamped: clamping silently changes what the caller
    asked for. `bool` is refused explicitly because Python counts it as an int.
    Over MCP the SDK's argument validation runs first and coerces `true` and
    `"2"` to floats (observed 2026-10-27 on the pinned SDK), so there the
    type checks never see them; the range check still applies.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(_hours_error(value))
    hours = float(value)
    if not math.isfinite(hours) or not 0 < hours <= HOLD_MAX_HOURS:
        raise ValueError(_hours_error(value))
    return hours


def _hours_error(value: object) -> str:
    return (f"hours must be a number greater than 0 and at most "
            f"{format_hours(HOLD_MAX_HOURS)}, got {value!r}.")


def format_hours(hours: float) -> str:
    """`2h`, `0.5h`, `1.3h` — never `2.0h`, and never `0h` for a short hold."""
    if hours < 0.1:
        return f"{hours:.2g}h"
    return f"{round(hours, 1):g}h"


@dataclass(frozen=True)
class Reminder:
    state: str
    #: hold window in hours, when the session holds a valid hold
    hours: float | None = None
    last_activity: datetime | None = None
    expires: datetime | None = None
    #: hours since last activity, for a lapsed hold
    idle_hours: float | None = None


def reminder_state(*, started_at: object, held_at: object, hold_hours: object,
                   note_stamps: Iterable[object], now: datetime) -> Reminder:
    """Whether the reminder is due for one open session at `now`.

    Unheld: due while `now - started_at` is at most the reminder window.
    Held: last activity is the newest of started_at, held_at and the
    session's notes, ignoring stamps more than FUTURE_TOLERANCE ahead — so
    every note stamp is passed, not just the newest: a future-dated newest
    note must not hide an older real one. Quiet while
    `now <= last activity + hours`, due for the reminder window after that,
    quiet again once that has passed. The start-age limit does not apply to a
    held session: a hold says someone was working in it, so going idle
    afterwards is exactly the forgotten case, however old the session is.

    A hold whose stored values are unusable — hours out of range, held_at
    unreadable, no countable stamp — is judged as unheld, never skipped: a
    skipped row is a silent one.
    """
    started = parse_utc(started_at)
    if started is None:
        return Reminder(SKIP)
    window = timedelta(hours=REMINDER_WINDOW_HOURS)
    hours = _usable_hours(hold_hours)
    if hours is not None and parse_utc(held_at) is not None:
        last = last_activity((started_at, held_at, *note_stamps), now)
        if last is not None:
            expires = last + timedelta(hours=hours)
            if now <= expires:
                return Reminder(HELD, hours, last, expires)
            if now <= expires + window:
                idle = (now - last).total_seconds() / 3600
                return Reminder(DUE_LAPSED, hours, last, expires, idle)
            return Reminder(QUIET, hours, last, expires)
    if now - started <= window:
        return Reminder(DUE_UNHELD)
    return Reminder(QUIET)


def last_activity(stamps: tuple[object, ...], now: datetime) -> datetime | None:
    limit = now + FUTURE_TOLERANCE
    parsed = (parse_utc(s) for s in stamps)
    return max((p for p in parsed if p is not None and p <= limit), default=None)


def _usable_hours(value: object) -> float | None:
    try:
        return validate_hold_hours(value)
    except ValueError:
        return None


def hold_view(*, open_: bool, started_at: object, held_at: object,
              hold_hours: object, note_stamps: Iterable[object],
              now: datetime) -> dict | None:
    """The `hold` block a session response carries, or None if never held.

    Present for closed sessions too, as the record of the hold; `active` is
    false for any session that is not open.
    """
    if held_at is None and hold_hours is None:
        return None
    r = reminder_state(started_at=started_at, held_at=held_at,
                       hold_hours=hold_hours, note_stamps=note_stamps, now=now)
    return {
        "held_at": held_at,
        "hours": _usable_hours(hold_hours),
        "last_activity_at": r.last_activity.isoformat() if r.last_activity else None,
        "expires_at": r.expires.isoformat() if r.expires else None,
        "active": open_ and r.state == HELD,
    }