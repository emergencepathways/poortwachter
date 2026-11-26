"""Free days, earned like (stingy) paid time off.

- Every completed study day earns 1 hour. 24 hours = 1 free day.
- A missed day wipes the hours built toward the next free day; whole banked
  days are kept.
- Taking a free day pauses the chain (no hours, no reset).
- The cap is 15 days in the first year, +1 each year after install, up to 20.
  At the cap, nothing more accrues.
- Free days are planned ahead (not on the day itself). The emergency key spends
  a banked day if you have one, so the chain isn't broken.

Everything is kept in whole hours in a ledger, so the history is readable and
there's no rounding drift."""

import datetime as dt
import time

from . import db

HOURS_PER_DAY = 24
BASE_CAP_DAYS = 15
MAX_CAP_DAYS = 20


class PTOError(Exception):
    pass


def balance():
    return int(db.connect().execute("SELECT COALESCE(SUM(hours), 0) FROM pto_ledger").fetchone()[0])


def cap_days(day=None):
    from . import roster
    day = day or roster.today()
    installed = db.get_setting("installed_day") or day
    years = max(0, (dt.date.fromisoformat(day) - dt.date.fromisoformat(installed)).days // 365)
    return min(BASE_CAP_DAYS + years, MAX_CAP_DAYS)


def _add(day, hours, reason):
    if hours:
        db.connect().execute("INSERT INTO pto_ledger (day, hours, reason, ts) VALUES (?, ?, ?, ?)",
                             (day, int(hours), reason, time.time()))


def is_free(day):
    return db.connect().execute("SELECT source FROM free_days WHERE day = ?", (day,)).fetchone() is not None


def free_source(day):
    r = db.connect().execute("SELECT source FROM free_days WHERE day = ?", (day,)).fetchone()
    return r["source"] if r else None


def _day_complete(day):
    rows = db.connect().execute("SELECT status FROM tasks WHERE day = ? AND counts = 1", (day,)).fetchall()
    has_roster = db.connect().execute("SELECT 1 FROM rosters WHERE day = ?", (day,)).fetchone()
    return bool(has_roster) and all(r["status"] == "done" for r in rows)


def process():
    """Settle every finished day since the last run (called often; cheap)."""
    from . import roster
    today = roster.today()
    last = db.get_setting("pto_processed_through") or db.get_setting("installed_day") or today
    d = dt.date.fromisoformat(last) + dt.timedelta(days=1)
    end = dt.date.fromisoformat(today)
    ramp_changed = False
    while d < end:
        day = d.isoformat()
        if is_free(day):
            pass  # a free day pauses the chain
        elif _day_complete(day):
            ramp_changed |= roster.settle_ramp(day, True)
            bal = balance()
            room = cap_days(day) * HOURS_PER_DAY - bal
            if room > 0:
                _add(day, 1, "completed day")
        else:
            ramp_changed |= roster.settle_ramp(day, False)
            partial = balance() % HOURS_PER_DAY
            if partial:
                _add(day, -partial, "missed day: progress toward the next free day reset")
        db.set_setting("pto_processed_through", day)
        d += dt.timedelta(days=1)
    if ramp_changed:
        roster.refresh_future()


def summary():
    process()
    b = balance()
    cap = cap_days()
    from . import roster
    upcoming = [dict(r) for r in db.connect().execute(
        "SELECT * FROM free_days WHERE day > ? AND source = 'planned' ORDER BY day", (roster.today(),))]
    ledger = [dict(r) for r in db.connect().execute("SELECT * FROM pto_ledger ORDER BY ts DESC, id DESC LIMIT 60")]
    return {
        "hours": b, "days": b // HOURS_PER_DAY, "partial_hours": b % HOURS_PER_DAY,
        "cap_days": cap, "at_cap": b >= cap * HOURS_PER_DAY,
        "upcoming": upcoming, "ledger": ledger,
    }


def plan_free_day(day):
    from . import roster
    process()
    if day <= roster.today():
        raise PTOError("Free days have to be planned ahead, from tomorrow on.")
    if day > roster.max_plan_day():
        raise PTOError("You can plan up to a week ahead.")
    if is_free(day):
        raise PTOError("That day is already free.")
    if balance() < HOURS_PER_DAY:
        b = balance()
        raise PTOError(f"Not enough saved up: you have {b // 24} days {b % 24} h. "
                       "Each completed day earns 1 hour, and a free day is 24.")
    _add(day, -HOURS_PER_DAY, f"free day taken: {day}")
    db.connect().execute("INSERT INTO free_days (day, source, ts) VALUES (?, 'planned', ?)", (day, time.time()))
    db.log("free-day", f"planned {day}")
    roster.rebuild(day)


def cancel_free_day(day):
    from . import roster
    if day <= roster.today():
        raise PTOError("That day has already started.")
    if free_source(day) != "planned":
        raise PTOError("That isn't a planned free day.")
    db.connect().execute("DELETE FROM free_days WHERE day = ?", (day,))
    _add(day, HOURS_PER_DAY, f"free day cancelled: {day} (refunded)")
    db.log("free-day", f"cancelled {day}")
    roster.rebuild(day)


def spend_for_emergency(day):
    """Used by the emergency key. Returns True if a banked day covered it."""
    process()
    if balance() < HOURS_PER_DAY:
        return False
    _add(day, -HOURS_PER_DAY, f"emergency key on {day}: used a free day")
    db.connect().execute("INSERT OR REPLACE INTO free_days (day, source, ts) VALUES (?, 'emergency', ?)",
                         (day, time.time()))
    return True
