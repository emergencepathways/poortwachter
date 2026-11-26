"""Calendars in and out.

In: read your calendars' private iCal addresses (Google Calendar → Settings →
your calendar → "Secret address in iCal format"). Events in the coming week
whose title contains one of your keywords ("italki", "taalgroep"…) become
sessions automatically, with their real start time and length. When an event
moves or is cancelled, the session follows (from tomorrow on: today is fixed).

Out: your plan for the coming week as an iCalendar, served locally at
/calendar.ics and, when paired with Boekenwurm, published through it at a
secret address Google Calendar can subscribe to.

Standard library only; handles the iCalendar that Google, Outlook and Apple
publish: time zones, all-day events (skipped), RRULE (DAILY, WEEKLY with BYDAY,
MONTHLY with BYMONTHDAY or BYDAY like 1SA, with INTERVAL, COUNT, UNTIL),
EXDATE, moved occurrences (RECURRENCE-ID) and cancellations."""

import datetime as dt
import hashlib
import os
import re
import time
import urllib.error
import urllib.request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from . import db, events, roster

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
MAX_OCCURRENCES = 2000


class ICalError(Exception):
    pass


# ---------------------------------------------------------------- parsing


def _unfold(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]", "", text).split("\n")


def _unescape(v):
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse(text):
    """VEVENTs as dicts: {NAME: (params, value)} plus lists for EXDATE."""
    out, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"EXDATE": []}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                out.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        parts = head.split(";")
        name = parts[0].upper()
        params = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v.strip('"')
        if name == "EXDATE":
            for v in value.split(","):
                cur["EXDATE"].append((params, v))
        else:
            cur[name] = (params, value)
    return out


def _local_tz():
    """This computer's time zone, with its daylight-saving rules when they can be found."""
    if ZoneInfo:
        try:
            link = os.readlink("/etc/localtime")
            if "zoneinfo/" in link:
                return ZoneInfo(link.split("zoneinfo/", 1)[1])
        except (OSError, Exception):  # noqa: BLE001
            pass
        try:
            with open("/etc/timezone") as f:
                return ZoneInfo(f.read().strip())
        except (OSError, Exception):  # noqa: BLE001
            pass
    return dt.datetime.now().astimezone().tzinfo


def to_dt(params, value):
    """A DTSTART/DTEND value as an aware local datetime, or a date for all-day values."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value[:8], "%Y%m%d").date()
    utc = value.endswith("Z")
    naive = dt.datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        aware = naive.replace(tzinfo=dt.timezone.utc)
    elif params.get("TZID") and ZoneInfo:
        try:
            aware = naive.replace(tzinfo=ZoneInfo(params["TZID"]))
        except Exception:  # noqa: BLE001 — unknown zone names: take it as local time
            aware = naive.replace(tzinfo=_local_tz())
    else:
        aware = naive.replace(tzinfo=_local_tz())
    # Kept in the event's own zone, so repeats keep their wall-clock time across daylight-saving changes.
    return aware


def _duration(value):
    m = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m:
        return dt.timedelta(hours=1)
    w, d, h, mi, s = (int(x or 0) for x in m.groups())
    return dt.timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def _rrule(value):
    return dict(p.split("=", 1) for p in value.split(";") if "=" in p)


def _nth_weekday(year, month, n, wd):
    first = dt.date(year, month, 1)
    if n > 0:
        d = first + dt.timedelta(days=(wd - first.weekday()) % 7 + 7 * (n - 1))
    else:
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        last = nxt - dt.timedelta(days=1)
        d = last - dt.timedelta(days=(last.weekday() - wd) % 7 + 7 * (-n - 1))
    return d if d.month == month else None


def occurrences(start, rule, until_limit):
    """Start datetimes of a recurring event, up to until_limit (aware datetime)."""
    freq = rule.get("FREQ", "")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        u = to_dt({}, rule["UNTIL"])
        until = (dt.datetime.combine(u, dt.time(23, 59, 59), tzinfo=start.tzinfo)
                 if isinstance(u, dt.date) and not isinstance(u, dt.datetime) else u)
    stop = min(until_limit, until) if until else until_limit
    out = []
    n = 0

    def add(when):
        nonlocal n
        if when < start:
            return True
        if when > stop or (count is not None and n >= count) or len(out) >= MAX_OCCURRENCES:
            return False
        out.append(when)
        n += 1
        return True

    t = start.timetz()
    if freq == "DAILY":
        d = start.date()
        while True:
            if not add(dt.datetime.combine(d, t)):
                break
            d += dt.timedelta(days=interval)
    elif freq == "WEEKLY":
        days = [WEEKDAYS.index(x[-2:]) for x in rule.get("BYDAY", "").split(",") if x[-2:] in WEEKDAYS] \
            or [start.weekday()]
        week = start.date() - dt.timedelta(days=start.weekday())
        going = True
        while going:
            for wd in sorted(days):
                if not add(dt.datetime.combine(week + dt.timedelta(days=wd), t)):
                    going = False
                    break
            week += dt.timedelta(weeks=interval)
    elif freq == "MONTHLY":
        y, mth = start.year, start.month
        going = True
        while going:
            ds = []
            if rule.get("BYDAY"):
                for x in rule["BYDAY"].split(","):
                    m = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", x)
                    if m:
                        d = _nth_weekday(y, mth, int(m.group(1) or 1), WEEKDAYS.index(m.group(2)))
                        if d:
                            ds.append(d)
            else:
                for x in (rule.get("BYMONTHDAY") or str(start.day)).split(","):
                    try:
                        ds.append(dt.date(y, mth, int(x)))
                    except ValueError:
                        pass
            for d in sorted(ds):
                if not add(dt.datetime.combine(d, t)):
                    going = False
                    break
            mth += interval
            y += (mth - 1) // 12
            mth = (mth - 1) % 12 + 1
            if y > until_limit.year + 1:
                break
    else:
        add(start)
    return out


def expand(text, day_from, day_to):
    """Timed event occurrences starting on study days day_from..day_to (dates):
    [{"uid", "summary", "start" (aware), "minutes", "location"}]."""
    evs = parse(text)
    limit = dt.datetime.combine(day_to + dt.timedelta(days=1), dt.time(23, 59), tzinfo=_local_tz())
    moved = {}
    for e in evs:
        if "RECURRENCE-ID" in e and "UID" in e:
            rid = to_dt(*e["RECURRENCE-ID"])
            moved[(e["UID"][1], rid)] = e
    out = []

    def emit(e, start, length):
        if isinstance(start, dt.date) and not isinstance(start, dt.datetime):
            return  # all-day events aren't sessions
        if (e.get("STATUS", ({}, ""))[1].upper() == "CANCELLED"):
            return
        start = start.astimezone(_local_tz())
        sday = roster.study_day(start.replace(tzinfo=None))
        if day_from <= sday <= day_to:
            out.append({
                "uid": e.get("UID", ({}, ""))[1] or hashlib.sha1(repr(e).encode()).hexdigest(),
                "summary": _unescape(e.get("SUMMARY", ({}, ""))[1]).strip(),
                "location": _unescape(e.get("LOCATION", ({}, ""))[1]).strip(),
                "start": start, "minutes": max(5, min(600, int(length.total_seconds() // 60))),
            })

    for e in evs:
        if "DTSTART" not in e or "RECURRENCE-ID" in e:
            continue
        start = to_dt(*e["DTSTART"])
        if "DTEND" in e:
            end = to_dt(*e["DTEND"])
            length = (end - start) if isinstance(start, dt.datetime) and isinstance(end, dt.datetime) else dt.timedelta(hours=1)
        elif "DURATION" in e:
            length = _duration(e["DURATION"][1])
        else:
            length = dt.timedelta(hours=1)
        if not isinstance(start, dt.datetime):
            continue
        if "RRULE" in e:
            ex = set()
            for p, v in e["EXDATE"]:
                x = to_dt(p, v)
                ex.add(x if isinstance(x, dt.datetime) else x)
            for when in occurrences(start, _rrule(e["RRULE"][1]), limit):
                if when in ex or when.date() in ex:
                    continue
                if (e["UID"][1] if "UID" in e else None, when) in moved:
                    continue  # replaced by its moved version below
                emit(e, when, length)
        else:
            emit(e, start, length)
    for (_uid, _rid), e in moved.items():
        start = to_dt(*e["DTSTART"])
        if "DTEND" in e:
            length = to_dt(*e["DTEND"]) - start
        elif "DURATION" in e:
            length = _duration(e["DURATION"][1])
        else:
            length = dt.timedelta(hours=1)
        emit(e, start, length)
    out.sort(key=lambda o: o["start"])
    return out


# ---------------------------------------------------------------- import → sessions


def fetch(url, timeout=20):
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith("https://"):
        raise ICalError("Calendar addresses start with https:// (or webcal://).")
    req = urllib.request.Request(url, headers={"User-Agent": "Poortwachter/3 (+calendar import)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(5_000_000)
    except urllib.error.HTTPError as e:
        raise ICalError(f"The calendar answered {e.code}. Check the address.") from None
    except urllib.error.URLError as e:
        raise ICalError(f"Couldn't reach the calendar: {e.reason}") from None
    text = data.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text:
        raise ICalError("That address didn't return a calendar (iCal).")
    return text


def match_rule(summary, rules):
    low = summary.lower()
    for r in rules:
        kw = (r.get("keyword") or "").strip().lower()
        if kw and kw in low:
            return r
    return None


def sync(fetcher=None):
    """Turn matching calendar events from tomorrow to a week ahead into one-off sessions; remove ones whose event is
    gone. Today is left alone (fixed). Returns a short report."""
    fetcher = fetcher or fetch
    feeds = [f for f in (db.get_setting("ical_feeds") or []) if f.get("url")]
    rules = db.get_setting("ical_rules") or []
    conn = db.connect()
    first = roster.study_day() + dt.timedelta(days=1)
    last = dt.date.fromisoformat(roster.max_plan_day())
    found, errors = {}, []
    for f in feeds:
        try:
            text = fetcher(f["url"])
        except ICalError as e:
            errors.append(f"{f.get('name') or 'calendar'}: {e}")
            continue
        for o in expand(text, first, last):
            r = match_rule(o["summary"], rules)
            if not r:
                continue
            day = roster.study_day(o["start"].replace(tzinfo=None)).isoformat()
            key = f"{o['uid']}|{o['start'].isoformat()}"
            found[key] = (day, o, r)
    if errors and not found and feeds and len(errors) == len(feeds):
        # Every calendar failed: change nothing rather than deleting sessions.
        db.set_setting("ical_status", {"at": time.time(), "errors": errors, "sessions": None})
        return {"added": 0, "updated": 0, "removed": 0, "errors": errors}
    existing = {r["ical_key"]: dict(r) for r in conn.execute(
        "SELECT * FROM sessions WHERE ical_key IS NOT NULL AND date >= ?", (first.isoformat(),))}
    added = updated = removed = 0
    for key, (day, o, r) in found.items():
        preset = events.PRESETS.get(r.get("kind"), events.PRESETS["other"])
        row = {"name": (r.get("name") or o["summary"] or preset["name"])[:120], "kind": r.get("kind") or "other",
               "category": preset["category"], "minutes": o["minutes"], "date": day,
               "time": o["start"].strftime("%H:%M"), "place": o["location"][:200],
               "link": preset["link"], "note_words": preset["note_words"]}
        if key in existing:
            e = existing[key]
            if any(str(e[k]) != str(row[k]) for k in ("name", "minutes", "date", "time", "place", "kind")):
                conn.execute("UPDATE sessions SET name=?, kind=?, category=?, minutes=?, date=?, time=?, place=? "
                             "WHERE id=?", (row["name"], row["kind"], row["category"], row["minutes"], row["date"],
                                            row["time"], row["place"], e["id"]))
                updated += 1
        else:
            conn.execute("INSERT INTO sessions (name, kind, category, minutes, date, time, place, link, note_words,"
                         " ical_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (row["name"], row["kind"], row["category"], row["minutes"], row["date"], row["time"],
                          row["place"], row["link"], row["note_words"], key, time.time()))
            added += 1
    failed_feeds = bool(errors)
    for key, e in existing.items():
        if key not in found and not failed_feeds:
            conn.execute("DELETE FROM sessions WHERE id = ?", (e["id"],))
            removed += 1
    db.set_setting("ical_status", {"at": time.time(), "errors": errors,
                                   "sessions": len(found), "added": added, "updated": updated, "removed": removed})
    if added or updated or removed:
        db.log("calendar", f"sessions from calendars: +{added} ~{updated} -{removed}")
        roster.refresh_future()
    return {"added": added, "updated": updated, "removed": removed, "errors": errors}


# ---------------------------------------------------------------- export: the plan as a calendar


def _esc(s):
    return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line):
    out, b = [], line.encode()
    while len(b) > 74:
        cut = 74
        while cut > 0 and (b[cut] & 0xC0) == 0x80:
            cut -= 1
        out.append(b[:cut].decode())
        b = b" " + b[cut:]
    out.append(b.decode())
    return "\r\n".join(out)


def plan_ics(now=None):
    """Today and the planned days as all-day "Dutch study" events, plus timed sessions. Mystery tasks stay hidden."""
    from . import pto
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Poortwachter//Study plan//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:Poortwachter: Dutch study", "X-PUBLISHED-TTL:PT1H", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]
    for day in [roster.today()] + roster.plan_days():
        roster.ensure_roster(day)
        d = dt.date.fromisoformat(day)
        tasks = [t for t in roster.tasks_for(day)]
        req = [t for t in tasks if t["counts"]]
        free = pto.free_source(day)
        if free:
            title = "🏖️ Free day (Poortwachter)"
            desc = "No Dutch study required today."
        else:
            mins = sum(t["est_min"] for t in req if t["source"] != "event")
            title = f"🇳🇱 Dutch study · {mins} min · {len([t for t in req if t['source'] != 'event'])} tasks"
            desc = "\n".join(
                ("✓ " if t["status"] == "done" else "• ")
                + ("Mystery task" if t["mystery"] and not t["revealed"] else t["name"]) + f" (~{t['est_min']} min)"
                for t in req if t["source"] != "event") or "Nothing planned."
        lines += ["BEGIN:VEVENT", f"UID:poortwachter-day-{day}@poortwachter", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + dt.timedelta(days=1):%Y%m%d}",
                  _fold(f"SUMMARY:{_esc(title)}"), _fold(f"DESCRIPTION:{_esc(desc)}"), "TRANSP:TRANSPARENT",
                  "END:VEVENT"]
        for t in tasks:
            if t["source"] != "event" or not t.get("time"):
                continue
            try:
                hh, mm = (int(x) for x in t["time"].split(":"))
            except ValueError:
                continue
            start = dt.datetime.combine(d, dt.time(hh, mm)).astimezone(dt.timezone.utc)
            end = start + dt.timedelta(minutes=t["est_min"])
            place = (t["params"] or {}).get("place") or ""
            lines += ["BEGIN:VEVENT", f"UID:poortwachter-session-{t['event_id']}-{day}@poortwachter",
                      f"DTSTAMP:{stamp}", f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}",
                      _fold(f"SUMMARY:{_esc(t['name'])}")]
            if place:
                lines.append(_fold(f"LOCATION:{_esc(place)}"))
            lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def publish(force_new_secret=False):
    """Publish the plan through Boekenwurm (needs pairing). Returns the subscribe address, if known."""
    from . import verify_boekenwurm as bw
    if not bw.status()["paired"]:
        raise ICalError("Pair Poortwachter with Boekenwurm first: the calendar is published through it.")
    ics = plan_ics()
    same = re.sub(r"DTSTAMP:\S+", "", ics)
    cal = db.get_setting("bw_calendar") or {}
    if same == cal.get("last_ics") and not force_new_secret and cal.get("secret"):
        return feed_url()
    try:
        secret = bw.call_rpc("put_app_calendar", {"p_token": (db.get_setting("bw_pairing") or {}).get("token"),
                                                  "p_ics": ics, "p_new_secret": bool(force_new_secret)})
    except bw.BWError as e:
        raise ICalError(str(e)) from None
    if secret:
        cal["secret"] = secret
    cal["last_ics"] = same
    cal["at"] = time.time()
    db.set_setting("bw_calendar", cal)
    return feed_url()


def feed_url():
    cal = db.get_setting("bw_calendar") or {}
    url = db.get_setting("bw_url")
    if not cal.get("secret") or not url:
        return None
    return f"{url}/functions/v1/calendar?f={cal['secret']}"
