"""Scheduled sessions: italki lessons, library group study, taalcafés.

A session is either weekly (a weekday, from a start date, optionally until an
end date) or a one-off on a specific date. On a day with sessions their
minutes count toward the day's budget, and the session itself becomes a
required task you tick off afterwards (honour-based, with a short note)."""

import datetime as dt
import time

from . import db

PRESETS = {
    "italki": {"name": "italki lesson", "category": "speaking", "minutes": 60, "link": 1, "note_words": 10},
    "library": {"name": "Library group study", "category": "other", "minutes": 120, "link": 0, "note_words": 10},
    "other": {"name": "Dutch session", "category": "speaking", "minutes": 60, "link": 0, "note_words": 10},
}


class EventError(Exception):
    pass


def all_events(include_past=False):
    rows = [dict(r) for r in db.connect().execute("SELECT * FROM sessions ORDER BY COALESCE(date, ''), weekday, time")]
    if include_past:
        return rows
    from . import roster
    t = roster.today()
    return [r for r in rows if r["weekday"] is not None and (not r["end_date"] or r["end_date"] >= t)
            or r["date"] and r["date"] >= t]


def event(eid):
    r = db.connect().execute("SELECT * FROM sessions WHERE id = ?", (eid,)).fetchone()
    return dict(r) if r else None


def occurs_on(e, day):
    if not e["active"]:
        return False
    if e["date"]:
        return e["date"] == day
    if e["weekday"] is None:
        return False
    if dt.date.fromisoformat(day).weekday() != e["weekday"]:
        return False
    if e["start_date"] and day < e["start_date"]:
        return False
    if e["end_date"] and day > e["end_date"]:
        return False
    skipped = db.connect().execute("SELECT 1 FROM session_skips WHERE event_id = ? AND day = ?",
                                   (e["id"], day)).fetchone()
    return not skipped


def for_day(day):
    return [e for e in (dict(r) for r in db.connect().execute("SELECT * FROM sessions ORDER BY time"))
            if occurs_on(e, day)]


def _clean(b):
    kind = b.get("kind") if b.get("kind") in PRESETS else "other"
    preset = PRESETS[kind]
    name = (b.get("name") or preset["name"]).strip()[:120]
    minutes = int(b.get("minutes") or preset["minutes"])
    if not 5 <= minutes <= 600:
        raise EventError("A session lasts between 5 and 600 minutes.")
    time_s = (b.get("time") or "").strip()[:5]
    weekday = b.get("weekday")
    date = b.get("date") or None
    if (weekday is None or weekday == "") and not date:
        raise EventError("Pick a date, or a weekday for a weekly session.")
    if date:
        dt.date.fromisoformat(date)
        weekday = None
    else:
        weekday = int(weekday)
        if not 0 <= weekday <= 6:
            raise EventError("Bad weekday.")
    for k in ("start_date", "end_date"):
        if b.get(k):
            dt.date.fromisoformat(b[k])
    return {
        "name": name, "kind": kind, "category": b.get("category") or preset["category"], "minutes": minutes,
        "weekday": weekday, "date": date, "time": time_s,
        "start_date": b.get("start_date") or None, "end_date": b.get("end_date") or None,
        "note_words": int(b.get("note_words", preset["note_words"]) or 0),
        "link": 1 if b.get("link", preset["link"]) else 0, "place": (b.get("place") or "")[:200],
    }


def _check_window(e):
    """New sessions can't be dropped onto today to dodge work: today is fixed.
    (Adding a session to today would only add a required task, so it's fine.)"""
    from . import roster
    if e["date"] and e["date"] < roster.today():
        raise EventError("That date is in the past.")


def create(b):
    from . import roster
    e = _clean(b)
    _check_window(e)
    if e["weekday"] is not None and not e["start_date"]:
        e["start_date"] = roster.today()
    cols = ", ".join(e)
    cur = db.connect().execute(f"INSERT INTO sessions ({cols}, created_at) VALUES ({', '.join('?' * len(e))}, ?)",
                               (*e.values(), time.time()))
    db.log("session", f"added {e['name']}")
    return cur.lastrowid


def update(eid, b):
    old = event(eid)
    if not old:
        raise EventError("No such session.")
    e = _clean(b)
    _check_window(e)
    sets = ", ".join(f"{k} = ?" for k in e)
    db.connect().execute(f"UPDATE sessions SET {sets} WHERE id = ?", (*e.values(), eid))


def end(eid):
    """Stop a weekly session from tomorrow on, or delete a one-off. Today's
    roster keeps it if it's already on there."""
    from . import roster
    e = event(eid)
    if not e:
        raise EventError("No such session.")
    if e["date"] or (e["start_date"] and e["start_date"] > roster.today()):
        db.connect().execute("DELETE FROM sessions WHERE id = ?", (eid,))
    else:
        db.connect().execute("UPDATE sessions SET end_date = ? WHERE id = ?", (roster.today(), eid))
    db.log("session", f"ended {e['name']}")


def skip(eid, day):
    """Skip one occurrence of a weekly session (it isn't on this week)."""
    from . import roster
    if day <= roster.today():
        raise EventError("Today's plan is fixed. You can skip future dates only.")
    db.connect().execute("INSERT OR IGNORE INTO session_skips (event_id, day) VALUES (?, ?)", (eid, day))


def unskip(eid, day):
    db.connect().execute("DELETE FROM session_skips WHERE event_id = ? AND day = ?", (eid, day))
