"""The local web app and its JSON API, on http://127.0.0.1:7623 only.

Two guards keep other things from poking the API: requests must carry a local
Host header (stops DNS-rebinding tricks), and every write must send the
X-Poortwachter header (a web page on another site can't add it without a
CORS preflight, which this server never approves)."""

import datetime as dt
import html
import json
import mimetypes
import os
import re
import tempfile
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import (__version__, actions, config, db, events, grammar, ical, oefenen, pto, roster, speech, textbook,
               verify_anki, verify_boekenwurm)
from .config import CATEGORIES, KINDS

MAX_JSON = 2 * 1024 * 1024
MAX_RECORDING = 60 * 1024 * 1024
MAX_PDF = 600 * 1024 * 1024

SETTABLE = {"schedule", "claude_api_key", "claude_model", "anki_collection", "bw_url", "bw_key",
            "bw_paragraphs_per_page", "speaking_prompts", "min_fill_minutes", "active_textbook",
            "category_caps", "event_minimum_activity", "plan_days_ahead", "bw_site", "languagetool",
            "ical_feeds", "ical_rules", "textbook_minutes", "textbook_hardcore", "ramp", "whisper",
            "writing_every_other_day"}
PLAN_KEYS = {"schedule", "min_fill_minutes", "category_caps", "event_minimum_activity", "active_textbook",
             "textbook_minutes", "ramp", "writing_every_other_day"}


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


ROUTES = []


def route(method, pattern):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


# ---------------------------------------------------------------- helpers


def refresh_future():
    roster.refresh_future()


def day_view(day):
    roster.ensure_roster(day)
    tasks = [roster.public_task(t) for t in roster.tasks_for(day)]
    counting = [t for t in tasks if t["counts"]]
    cat_minutes = {}
    for t in counting:
        if t["source"] != "event" and t["kind"] != "mystery":
            cat_minutes[t["category"]] = cat_minutes.get(t["category"], 0) + t["est_min"]
    return {
        "day": day,
        "editable": roster.today() < day <= roster.max_plan_day(),
        "tasks": tasks,
        "planned_minutes": sum(t["est_min"] for t in counting),
        "budget": db.get_setting("schedule")[str(dt.date.fromisoformat(day).weekday())]["budget"]
        + roster.ramp_bonus(dt.date.fromisoformat(day).weekday()),
        "free": pto.free_source(day),
        "sessions": [t for t in tasks if t["source"] == "event"],
        "category_minutes": cat_minutes,
        "caps": roster.caps(),
    }


def week_strip():
    out = []
    for d in roster.plan_days():
        v = day_view(d)
        out.append({"day": d, "planned_minutes": v["planned_minutes"], "budget": v["budget"],
                    "free": v["free"], "sessions": len(v["sessions"]),
                    "tasks": len([t for t in v["tasks"] if t["counts"]])})
    return out


def settings_view():
    bw = verify_boekenwurm.status()
    return {
        "schedule": db.get_setting("schedule"),
        "claude_model": db.get_setting("claude_model"),
        "claude_key_set": bool(db.get_setting("claude_api_key")),
        "anki_collection": db.get_setting("anki_collection") or "",
        "anki_detected": verify_anki.find_collections(),
        "anki_in_use": verify_anki.collection_path(),
        "bw_url": db.get_setting("bw_url") or "",
        "bw_key_set": bool(db.get_setting("bw_key")),
        "bw": bw,
        "bw_paragraphs_per_page": db.get_setting("bw_paragraphs_per_page"),
        "speaking_prompts": db.get_setting("speaking_prompts"),
        "min_fill_minutes": db.get_setting("min_fill_minutes"),
        "active_textbook": db.get_setting("active_textbook"),
        "watched_user": config.read_conf().get("user"),
        "category_caps": db.get_setting("category_caps"),
        "event_minimum_activity": db.get_setting("event_minimum_activity"),
        "plan_days_ahead": db.get_setting("plan_days_ahead"),
        "installed_day": db.get_setting("installed_day"),
        "languagetool": db.get_setting("languagetool"),
        "ical_feeds": db.get_setting("ical_feeds"),
        "ical_rules": db.get_setting("ical_rules"),
        "textbook_minutes": db.get_setting("textbook_minutes"),
        "textbook_hardcore": db.get_setting("textbook_hardcore"),
        "ramp": db.get_setting("ramp"),
        "ramp_state": db.get_setting("ramp_state"),
        "whisper": db.get_setting("whisper"),
        "speech": speech.status(),
        "writing_every_other_day": db.get_setting("writing_every_other_day"),
        "plan_version": db.get_setting("plan_version"),
    }


# ---------------------------------------------------------------- state


@route("GET", r"/api/state")
def api_state(h, m):
    t, tm = roster.today(), roster.tomorrow()
    unlocks = db.connect().execute(
        "SELECT COUNT(*) FROM unlocks WHERE day >= date('now', '-30 days')").fetchone()[0]
    return {
        "version": __version__,
        "lock": roster.lock_state(t),
        "today": day_view(t),
        "tomorrow": day_view(tm),
        "week": week_strip(),
        "pto": pto.summary(),
        "installed_day": db.get_setting("installed_day"),
        "event_minimum": db.get_setting("event_minimum_activity"),
        "streak": roster.streak(),
        "history": roster.history(21),
        "emergency_30d": unlocks,
        "categories": CATEGORIES,
        "kinds": KINDS,
    }


@route("GET", r"/api/lock")
def api_lock(h, m):
    return roster.lock_state()


@route("GET", r"/api/day/(\d{4}-\d{2}-\d{2})")
def api_day(h, m):
    day = m.group(1)
    if day > roster.max_plan_day():
        raise HTTPError(400, "You can plan up to a week ahead.")
    if day < roster.today():
        exists = db.connect().execute("SELECT 1 FROM rosters WHERE day = ?", (day,)).fetchone()
        if not exists:
            return {"day": day, "editable": False, "tasks": [], "planned_minutes": 0, "budget": 0,
                    "free": None, "sessions": [], "category_minutes": {}, "caps": roster.caps()}
    return day_view(day)


@route("GET", r"/api/events")
def api_events(h, m):
    return [dict(r) for r in db.connect().execute("SELECT * FROM events ORDER BY ts DESC LIMIT 200")]


# ---------------------------------------------------------------- planning


@route("POST", r"/api/day/(\d{4}-\d{2}-\d{2})/regenerate")
def api_regen(h, m):
    roster.regenerate(m.group(1))
    return day_view(m.group(1))


@route("POST", r"/api/day/(\d{4}-\d{2}-\d{2})/add")
def api_add(h, m):
    b = h.json()
    day = m.group(1)
    roster.add_task(day, int(b["activity_id"]), bonus=bool(b.get("bonus")))
    return day_view(day)


@route("POST", r"/api/day/(\d{4}-\d{2}-\d{2})/mystery")
def api_add_mystery(h, m):
    b = h.json()
    day = m.group(1)
    roster.add_mystery(day, b.get("category") or None)
    return day_view(day)


@route("POST", r"/api/tasks/(\d+)/remove")
def api_remove(h, m):
    t = roster.task(int(m.group(1)))
    roster.remove_task(int(m.group(1)))
    return day_view(t["day"])


@route("POST", r"/api/tasks/(\d+)/swap")
def api_swap(h, m):
    t = roster.task(int(m.group(1)))
    roster.swap_task(int(m.group(1)))
    return day_view(t["day"])


@route("POST", r"/api/tasks/(\d+)/mystify")
def api_mystify(h, m):
    t = roster.task(int(m.group(1)))
    roster.mystify(int(m.group(1)))
    return day_view(t["day"])


# ---------------------------------------------------------------- sessions & free days


def _after_session_change():
    roster.refresh_future()
    roster._sync_events(roster.today())  # adds only: today's plan can't shrink


@route("GET", r"/api/sessions")
def api_sessions(h, m):
    return {"sessions": events.all_events(), "presets": events.PRESETS}


@route("POST", r"/api/sessions")
def api_session_create(h, m):
    events.create(h.json())
    _after_session_change()
    return api_sessions(h, m)


@route("POST", r"/api/sessions/(\d+)")
def api_session_update(h, m):
    events.update(int(m.group(1)), h.json())
    _after_session_change()
    return api_sessions(h, m)


@route("POST", r"/api/sessions/(\d+)/end")
def api_session_end(h, m):
    events.end(int(m.group(1)))
    _after_session_change()
    return api_sessions(h, m)


@route("POST", r"/api/sessions/(\d+)/skip")
def api_session_skip(h, m):
    b = h.json()
    if b.get("undo"):
        events.unskip(int(m.group(1)), b["day"])
    else:
        events.skip(int(m.group(1)), b["day"])
    _after_session_change()
    return day_view(b["day"])


@route("POST", r"/api/calendars/sync")
def api_cal_sync(h, m):
    return ical.sync()


@route("POST", r"/api/calendars/test")
def api_cal_test(h, m):
    url = (h.json().get("url") or "").strip()
    text = ical.fetch(url)
    first = roster.study_day() + dt.timedelta(days=1)
    occ = ical.expand(text, first, dt.date.fromisoformat(roster.max_plan_day()))
    rules = db.get_setting("ical_rules") or []
    return {"events": [{"summary": o["summary"], "start": o["start"].strftime("%a %d %b %H:%M"),
                        "minutes": o["minutes"], "match": (ical.match_rule(o["summary"], rules) or {}).get("kind")}
                       for o in occ[:40]]}


@route("GET", r"/api/calendars/feed")
def api_cal_feed(h, m):
    return {"local": f"http://{config.HOST}:{config.PORT}/calendar.ics",
            "status": db.get_setting("ical_status") or {}}


@route("GET", r"/api/pto")
def api_pto(h, m):
    return pto.summary()


@route("POST", r"/api/pto/free")
def api_pto_free(h, m):
    pto.plan_free_day(h.json().get("day", ""))
    return pto.summary()


@route("POST", r"/api/pto/cancel")
def api_pto_cancel(h, m):
    pto.cancel_free_day(h.json().get("day", ""))
    return pto.summary()


# ---------------------------------------------------------------- working on tasks


@route("GET", r"/api/tasks/(\d+)")
def api_task(h, m):
    t = roster.task(int(m.group(1)))
    if not t:
        raise HTTPError(404, "No such task.")
    return roster.public_task(t)


@route("POST", r"/api/tasks/(\d+)/reveal")
def api_reveal(h, m):
    roster.reveal(int(m.group(1)))
    return roster.public_task(roster.task(int(m.group(1))))


@route("POST", r"/api/tasks/(\d+)/check")
def api_check(h, m):
    return actions.check(int(m.group(1)))


@route("POST", r"/api/tasks/(\d+)/beat")
def api_beat(h, m):
    return actions.beat(int(m.group(1)), bool(h.json().get("running", True)))


@route("POST", r"/api/tasks/(\d+)/finish_timer")
def api_finish_timer(h, m):
    return actions.finish_timer(int(m.group(1)), h.json().get("note", ""))


@route("POST", r"/api/tasks/(\d+)/note")
def api_note(h, m):
    return actions.submit_note(int(m.group(1)), h.json().get("text", ""))


@route("POST", r"/api/tasks/(\d+)/honor")
def api_honor(h, m):
    b = h.json()
    return actions.submit_honor(int(m.group(1)), b.get("note", ""), b.get("link", ""))


@route("GET", r"/api/tasks/(\d+)/prompt")
def api_prompt(h, m):
    return actions.task_prompt(int(m.group(1)))


@route("POST", r"/api/tasks/(\d+)/prompt")
def api_new_prompt(h, m):
    return actions.new_prompt(int(m.group(1)))


@route("POST", r"/api/grammar")
def api_grammar(h, m):
    return grammar.check(h.json().get("text", ""))


@route("POST", r"/api/tasks/(\d+)/recording")
def api_recording(h, m):
    data = h.body(MAX_RECORDING)
    secs = float(h.headers.get("X-Seconds") or 0)
    ext = (h.headers.get("X-Ext") or "webm").strip(".")
    return actions.save_recording(int(m.group(1)), data, secs, ext)


@route("GET", r"/api/tasks/(\d+)/textbook")
def api_tb_target(h, m):
    return actions.textbook_target(int(m.group(1)))


@route("POST", r"/api/tasks/(\d+)/textbook")
def api_tb_submit(h, m):
    b = h.json()
    return actions.submit_textbook(int(m.group(1)), b.get("answers", ""), b.get("label"),
                                   b.get("page_from"), b.get("page_to"))


# ---------------------------------------------------------------- textbook practice (oefenen)


@route("GET", r"/api/tasks/(\d+)/oefenen")
def api_oef_status(h, m):
    return oefenen.status(int(m.group(1)))


@route("POST", r"/api/tasks/(\d+)/oefenen/beat")
def api_oef_beat(h, m):
    return oefenen.status(int(m.group(1)), beat=True, running=bool(h.json().get("running", True)))


@route("POST", r"/api/tasks/(\d+)/oefenen/next")
def api_oef_next(h, m):
    b = h.json()
    return oefenen.next_question(int(m.group(1)), skip_unit=bool(b.get("skip")), focus_unit=b.get("focus"))


@route("POST", r"/api/tasks/(\d+)/oefenen/answer")
def api_oef_answer(h, m):
    return oefenen.answer(int(m.group(1)), h.json().get("answer", ""))


@route("POST", r"/api/tasks/(\d+)/oefenen/notpossible")
def api_oef_notpossible(h, m):
    return oefenen.not_possible(int(m.group(1)))


@route("POST", r"/api/tasks/(\d+)/oefenen/didntwork")
def api_oef_didntwork(h, m):
    return oefenen.didnt_work(int(m.group(1)))


@route("POST", r"/api/tasks/(\d+)/oefenen/resume")
def api_oef_resume(h, m):
    return oefenen.resume(int(m.group(1)), h.json().get("choice"))


@route("POST", r"/api/tasks/(\d+)/oefenen/italki")
def api_oef_italki(h, m):
    b = h.json()
    return oefenen.italki(int(m.group(1)), b.get("units") or [], b.get("pages") or [], b.get("note", ""))


@route("POST", r"/api/tasks/(\d+)/oefenen/speak")
def api_oef_speak(h, m):
    data = h.body(MAX_RECORDING)
    return oefenen.speak(int(m.group(1)), data, (h.headers.get("X-Ext") or "webm").strip("."))


@route("POST", r"/api/tasks/(\d+)/focus")
def api_task_focus(h, m):
    """Plan a textbook task on a coming day to work on a chosen chapter or exercise."""
    t = roster.task(int(m.group(1)))
    if not t or t["kind"] != "textbook":
        raise HTTPError(404, "No such textbook task.")
    if t["day"] <= roster.today():
        raise HTTPError(400, "Change today's focus inside the task.")
    uid = h.json().get("unit_id") or None
    if uid and not oefenen.unit(uid):
        raise HTTPError(400, "No such exercise.")
    p = dict(t["params"])
    if uid:
        p["focus_unit"] = uid
    else:
        p.pop("focus_unit", None)
    db.connect().execute("UPDATE tasks SET params = ? WHERE id = ?", (json.dumps(p), t["id"]))
    roster.mark_edited(t["day"])
    return roster.public_task(roster.task(t["id"]))


@route("GET", r"/api/oefenen")
def api_oef_overview(h, m):
    if not db.get_setting("active_textbook"):
        return {"textbook_id": None, "units": []}
    return oefenen.overview() | {"speech": speech.status()}


@route("POST", r"/api/oefenen/import")
def api_oef_import(h, m):
    b = h.json()
    r = oefenen.import_bank(b.get("textbook_id"), b.get("data"))
    refresh_future()
    return r


@route("POST", r"/api/oefenen/units/([\w.-]+)/status")
def api_oef_unit_status(h, m):
    oefenen.set_status(m.group(1), h.json().get("status"))
    return oefenen.overview()


@route("POST", r"/api/oefenen/focus")
def api_oef_focus(h, m):
    oefenen.set_focus(h.json().get("unit_id") or None)
    return oefenen.overview()


@route("POST", r"/api/oefenen/units/([\w.-]+)/audio")
def api_oef_audio(h, m):
    data = h.body(MAX_RECORDING)
    return oefenen.save_audio(m.group(1), data, (h.headers.get("X-Ext") or "mp3"))


@route("POST", r"/api/plan/recommended")
def api_plan_recommended(h, m):
    db.apply_recommended_plan()
    refresh_future()
    return settings_view()


# ---------------------------------------------------------------- library


@route("GET", r"/api/activities")
def api_activities(h, m):
    return roster.activities()


def _clean_activity(b):
    name = (b.get("name") or "").strip()
    if not name:
        raise HTTPError(400, "Give the activity a name.")
    if b.get("category") not in dict(CATEGORIES):
        raise HTTPError(400, "Unknown category.")
    if b.get("kind") not in KINDS:
        raise HTTPError(400, "Unknown check type.")
    params = b.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPError(400, "Bad settings for that activity.")
    return (name[:120], b["category"], b["kind"], json.dumps(params),
            max(1, min(int(b.get("est_min") or 15), 600)),
            max(0.0, min(float(b.get("weight", 1)), 10.0)),
            1 if b.get("in_pool", True) else 0, 1 if b.get("enabled", True) else 0,
            (b.get("description") or "")[:500])


@route("POST", r"/api/activities")
def api_activity_create(h, m):
    v = _clean_activity(h.json())
    db.connect().execute(
        "INSERT INTO activities (name, category, kind, params, est_min, weight, in_pool, enabled, description,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?, strftime('%s','now'))", v)
    refresh_future()
    return roster.activities()


@route("POST", r"/api/activities/(\d+)")
def api_activity_update(h, m):
    v = _clean_activity(h.json())
    db.connect().execute(
        "UPDATE activities SET name=?, category=?, kind=?, params=?, est_min=?, weight=?, in_pool=?, enabled=?,"
        " description=? WHERE id=?", (*v, int(m.group(1))))
    refresh_future()
    return roster.activities()


@route("POST", r"/api/activities/(\d+)/delete")
def api_activity_delete(h, m):
    aid = int(m.group(1))
    db.connect().execute("DELETE FROM activities WHERE id = ?", (aid,))
    sched = db.get_setting("schedule")
    for wd in sched.values():
        wd["core"] = [i for i in wd.get("core", []) if i != aid]
    db.set_setting("schedule", sched)
    refresh_future()
    return roster.activities()


# ---------------------------------------------------------------- settings


@route("GET", r"/api/settings")
def api_settings(h, m):
    return settings_view()


@route("POST", r"/api/settings")
def api_settings_save(h, m):
    b = h.json()
    for key, value in b.items():
        if key not in SETTABLE:
            continue
        if key == "schedule":
            clean = {}
            for wd in range(7):
                s = value.get(str(wd)) or {}
                clean[str(wd)] = {
                    "budget": max(0, min(int(s.get("budget", 60)), 600)),
                    "mystery": max(0.0, min(float(s.get("mystery", 0)), 1.0)),
                    "core": [int(i) for i in s.get("core", [])],
                }
            value = clean
        if key == "category_caps":
            value = {k: max(5, min(int(v), 600)) for k, v in (value or {}).items()
                     if k in dict(CATEGORIES) and v not in (None, "", 0, "0")}
        if key == "plan_days_ahead":
            value = max(1, min(int(value), 14))
        if key == "event_minimum_activity":
            value = int(value) if value else None
        if key in ("claude_api_key", "bw_key") and value == "":
            continue  # empty field = keep the stored secret
        if key == "textbook_minutes":
            value = max(oefenen.MIN_TARGET_MIN, min(int(value or 20), 180))  # never under 20 minutes
        if key == "textbook_hardcore" or key == "whisper":
            value = bool(value)
        if key == "ramp":
            value = {"enabled": bool((value or {}).get("enabled")), "step": 5, "after": 7, "max": 120}
        if key == "writing_every_other_day":
            value = [int(i) for i in (value or [])]
        if key == "speaking_prompts":
            value = [p.strip() for p in value if isinstance(p, str) and p.strip()] or db.SPEAKING_PROMPTS
        db.set_setting(key, value)
    if PLAN_KEYS & set(b):
        refresh_future()
    if {"ical_feeds", "ical_rules"} & set(b):
        try:
            ical.sync()
        except ical.ICalError:
            pass
    db.log("settings", ", ".join(sorted(k for k in b if k in SETTABLE and "key" not in k)))
    return settings_view()


@route("POST", r"/api/settings/clear_claude_key")
def api_clear_key(h, m):
    db.set_setting("claude_api_key", "")
    return settings_view()


@route("GET", r"/api/anki/decks")
def api_anki_decks(h, m):
    try:
        return verify_anki.list_decks()
    except Exception as e:  # noqa: BLE001
        raise HTTPError(400, f"Couldn't read Anki: {e}") from None


@route("POST", r"/api/bw/login")
def api_bw_login(h, m):
    email = (h.json().get("email") or "").strip()
    if "@" not in email:
        raise HTTPError(400, "Enter the email address you use for Boekenwurm.")
    verify_boekenwurm.start_login(email)
    return {"sent": True}


@route("POST", r"/api/bw/unpair")
def api_bw_unpair(h, m):
    return verify_boekenwurm.unpair()


@route("POST", r"/api/bw/code")
def api_bw_code(h, m):
    verify_boekenwurm.finish_login_otp(h.json().get("code", ""))
    return verify_boekenwurm.status()


@route("POST", r"/api/bw/tokens")
def api_bw_tokens(h, m):
    b = h.json()
    verify_boekenwurm.finish_login_tokens(b.get("access_token"), b.get("refresh_token"),
                                          int(b.get("expires_in") or 3600))
    return verify_boekenwurm.status()


@route("POST", r"/api/bw/logout")
def api_bw_logout(h, m):
    verify_boekenwurm.sign_out()
    return verify_boekenwurm.status()


@route("GET", r"/api/bw/test")
def api_bw_test(h, m):
    day = roster.today()
    if verify_boekenwurm.status()["paired"]:
        return {"summary": verify_boekenwurm.summary(day)}
    return {"reading": verify_boekenwurm.reading_today(day), "cards": verify_boekenwurm.cards_today(day)}


# ---------------------------------------------------------------- textbooks


@route("GET", r"/api/textbooks")
def api_books(h, m):
    out = []
    for b in textbook.books():
        b = dict(b)
        b.pop("file", None)
        b["sets"] = textbook.sets(b["id"])
        out.append(b)
    return {"books": out, "active": db.get_setting("active_textbook")}


@route("POST", r"/api/textbooks")
def api_book_upload(h, m):
    name = urllib.parse.unquote(h.headers.get("X-Name") or "Textbook")[:200]
    length = int(h.headers.get("Content-Length") or 0)
    if length <= 0 or length > MAX_PDF:
        raise HTTPError(400, "That file is empty or too large.")
    fd, tmp = tempfile.mkstemp(prefix="pw-up-", suffix=".pdf", dir=str(config.STATE_DIR))
    with os.fdopen(fd, "wb") as f:
        remaining = length
        first = True
        while remaining:
            chunk = h.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            if first and not chunk.startswith(b"%PDF"):
                os.unlink(tmp)
                raise HTTPError(400, "That isn't a PDF.")
            first = False
            f.write(chunk)
            remaining -= len(chunk)
    textbook.add_book(name, tmp)
    return api_books(h, m)


@route("POST", r"/api/textbooks/(\d+)/delete")
def api_book_delete(h, m):
    textbook.delete_book(int(m.group(1)))
    return api_books(h, m)


@route("POST", r"/api/textbooks/(\d+)/offset")
def api_book_offset(h, m):
    textbook.set_offset(int(m.group(1)), int(h.json().get("offset", 0)))
    return api_books(h, m)


@route("POST", r"/api/textbooks/(\d+)/activate")
def api_book_activate(h, m):
    db.set_setting("active_textbook", int(m.group(1)))
    return api_books(h, m)


@route("POST", r"/api/textbooks/(\d+)/sets")
def api_sets_add(h, m):
    added, bad = textbook.add_lines(int(m.group(1)), h.json().get("text", ""))
    roster.refresh_future()
    out = api_books(h, m)
    out["added"], out["bad"] = added, bad
    return out


@route("POST", r"/api/textbooks/(\d+)/sets/import")
def api_sets_import(h, m):
    b = h.json()
    n, over = textbook.import_sets(int(m.group(1)), b.get("data"), bool(b.get("replace")))
    roster.refresh_future()
    out = api_books(h, m)
    out["added"], out["over_cap"] = n, over
    return out


@route("POST", r"/api/textbooks/(\d+)/sets/clear")
def api_sets_clear(h, m):
    textbook.clear_sets(int(m.group(1)))
    roster.refresh_future()
    return api_books(h, m)


@route("POST", r"/api/sets/(\d+)/delete")
def api_set_delete(h, m):
    textbook.delete_set(int(m.group(1)))
    return api_books(h, m)


@route("POST", r"/api/sets/(\d+)/reset")
def api_set_reset(h, m):
    textbook.reset_set(int(m.group(1)))
    return api_books(h, m)


@route("POST", r"/api/textbooks/(\d+)/practice")
def api_practice(h, m):
    b = h.json()
    return actions.practice_textbook(int(m.group(1)), b.get("page_from"), b.get("page_to"),
                                     b.get("label", ""), b.get("answers", ""), b.get("set_id"))


@route("GET", r"/api/submissions")
def api_submissions(h, m):
    return textbook.submissions()


@route("GET", r"/api/recordings")
def api_recordings(h, m):
    return actions.recordings()


# ---------------------------------------------------------------- handler


class Handler(BaseHTTPRequestHandler):
    server_version = f"Poortwachter/{__version__}"

    def log_message(self, fmt, *args):  # keep the journal quiet
        pass

    # -- guards
    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_JSON:
            raise HTTPError(413, "Too much data.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise HTTPError(400, "Bad JSON.") from None

    def body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise HTTPError(413, "That file is too large.")
        return self.rfile.read(length)

    # -- responses
    def send_json(self, obj, status=200):
        data = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype=None, inline_name=None):
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_json({"error": "Not found."}, 404)
            return
        ctype = ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        if inline_name:
            self.send_header("Content-Disposition", f'inline; filename="{inline_name}"')
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                self.wfile.write(chunk)

    def send_page(self, title, message, ok=True):
        """A small page of its own, for the Boekenwurm handoff (outside the app, in the browser)."""
        colour = "#1d7a46" if ok else "#a12c2c"
        data = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                f'<title>Poortwachter</title><body style="font:17px/1.5 system-ui,sans-serif;max-width:34em;margin:12vh auto;padding:0 1em">'
                f'<h1 style="color:{colour};font-size:1.5em">{html.escape(title)}</h1><p>{html.escape(message)}</p>'
                f'<p><a href="/#settings">Open Poortwachter</a></p></body></html>').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    # -- dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        if not self._host_ok():
            self.send_json({"error": "Forbidden."}, 403)
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if method == "POST" and self.headers.get("X-Poortwachter") != "1":
                raise HTTPError(403, "Missing request header.")
            if method == "GET" and not path.startswith("/api/"):
                self._static(path)
                return
            for mth, rx, fn in ROUTES:
                if mth == method:
                    mm = rx.match(path)
                    if mm:
                        result = fn(self, mm)
                        if result is not None:
                            self.send_json(result)
                        return
            raise HTTPError(404, "Not found.")
        except HTTPError as e:
            self.send_json({"error": e.message}, e.status)
        except (roster.EditError, actions.ActionError, textbook.TextbookError,
                verify_boekenwurm.BWError, events.EventError, pto.PTOError, grammar.GrammarError,
                ical.ICalError, oefenen.OefenError, ValueError) as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            db.log("error", traceback.format_exc()[-1500:])
            self.send_json({"error": f"Something went wrong: {e}"}, 500)

    def _static(self, path):
        if path == "/bw/callback":
            self._bw_callback()
            return
        if path in ("/boekenwurm/connect", "/boekenwurm/linked"):
            self._bw_link(path)
            return
        mp = re.match(r"^/files/textbook/(\d+)/p/(\d+)\.png$", path)
        if mp:
            try:
                self.send_file(textbook.render_page(int(mp.group(1)), int(mp.group(2))), "image/png")
            except textbook.TextbookError as e:
                self.send_json({"error": str(e)}, 404)
            return
        ma = re.match(r"^/files/audio/([\w.-]+)$", path)
        if ma:
            bid = db.get_setting("active_textbook")
            p = oefenen.audio_path(int(bid), ma.group(1)) if bid else None
            if not p:
                self.send_json({"error": "Not found."}, 404)
                return
            self.send_file(p)
            return
        if path == "/calendar.ics":
            data = ical.plan_ics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/files/textbook/"):
            bid = path.rsplit("/", 1)[-1].split(".")[0]
            b = textbook.book(int(bid)) if bid.isdigit() else None
            if not b:
                self.send_json({"error": "Not found."}, 404)
                return
            self.send_file(b["file"], "application/pdf", "textbook.pdf")
            return
        if path.startswith("/files/recording/"):
            name = os.path.basename(path)
            if not re.match(r"^[\w.-]+$", name):
                self.send_json({"error": "Not found."}, 404)
                return
            self.send_file(config.RECORDING_DIR / name)
            return
        if path in ("/", "/index.html"):
            path = "/index.html"
        target = (config.WEB_DIR / path.lstrip("/")).resolve()
        if config.WEB_DIR.resolve() not in target.parents or not target.is_file():
            target = config.WEB_DIR / "index.html"  # single-page app
        self.send_file(target)

    def _bw_link(self, path):
        """Connecting to Boekenwurm in the browser (verify_boekenwurm.link_start / link_finish)."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
        try:
            if path == "/boekenwurm/connect":
                self.redirect(verify_boekenwurm.link_start(one("site") or None))
                return
            if one("error"):
                self.send_page("Not connected", "Boekenwurm didn't connect. Start again when you're ready.", ok=False)
                return
            st = verify_boekenwurm.link_finish(one("code"), one("state"))
            self.send_page("Connected to Boekenwurm",
                           f"Poortwachter is connected as “{st['paired_name']}”. Boekenwurm tasks now check today's "
                           "numbers by themselves. You can close this tab.")
        except verify_boekenwurm.BWError as e:
            self.send_page("Not connected", str(e), ok=False)

    def _bw_callback(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in q:
            try:
                verify_boekenwurm.finish_login_code(q["code"][0])
                self.redirect("/#settings?bw=ok")
            except verify_boekenwurm.BWError as e:
                self.redirect("/#settings?bw=" + urllib.parse.quote(str(e)))
            return
        if "error_description" in q:
            self.redirect("/#settings?bw=" + urllib.parse.quote(q["error_description"][0]))
            return
        # Implicit flow: tokens arrive in the URL fragment, which only the browser sees.
        self.send_file(config.WEB_DIR / "bw-callback.html")


def make_server():
    db.init()
    return ThreadingHTTPServer((config.HOST, config.PORT), Handler)
