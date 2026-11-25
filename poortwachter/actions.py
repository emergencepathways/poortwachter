"""What you can do with a task, per kind. Every rule is enforced here on the
service side, so the browser can't tick something off on its own."""

import os
import random
import shutil
import subprocess
import time

from . import config, db, dutch, roster, textbook, verify_anki, verify_boekenwurm


class ActionError(Exception):
    pass


def _today_task(task_id):
    t = roster.task(task_id)
    if not t:
        raise ActionError("No such task.")
    if t["day"] != roster.today():
        raise ActionError("Only today's tasks can be worked on.")
    if t["mystery"] and not t["revealed"]:
        raise ActionError("Reveal the mystery task first.")
    if t["status"] == "done":
        raise ActionError("Already done.")
    return t


# ---------------------------------------------------------------- automatic checks


def check(task_id):
    t = _today_task(task_id)
    p = t["params"]
    if t["kind"] == "anki":
        res = verify_anki.check(p, t["day"])
    elif t["kind"] == "bw_reading":
        res = verify_boekenwurm.check_reading(p, t["day"])
    elif t["kind"] == "bw_cards":
        res = verify_boekenwurm.check_cards(p, t["day"])
    else:
        raise ActionError("This task isn't checked automatically.")
    prog = t["progress"]
    prog.update({"last_check": time.time(), "message": res.get("message"), "value": res.get("value"),
                 "target": res.get("target")})
    roster.save_progress(task_id, prog)
    if res.get("ok"):
        roster.complete(task_id, {"checked": res.get("message")})
    return res


def auto_check_all():
    """Called by the service every few minutes so the lock opens even if the
    app isn't open."""
    day = roster.today()
    roster.ensure_roster(day)
    for t in roster.tasks_for(day):
        if t["status"] == "done" or t["kind"] not in ("anki", "bw_reading", "bw_cards"):
            continue
        if t["mystery"] and not t["revealed"]:
            continue
        try:
            check(t["id"])
        except Exception:  # noqa: BLE001 — a failing check must never stop the loop
            pass


# ---------------------------------------------------------------- timers

BEAT_GAP = 75  # seconds; browsers slow timers in background tabs to ~1/min, so allow that, but a longer gap means the panel was closed


def beat(task_id, running=True):
    """The open page sends a heartbeat every ~10 s while the timer runs. Only
    covered time counts, so closing the tab pauses the timer."""
    t = _today_task(task_id)
    if t["kind"] != "timer":
        raise ActionError("Not a timer task.")
    prog = t["progress"]
    now = time.time()
    last = prog.get("last_beat")
    secs = float(prog.get("seconds", 0))
    if running and last and now - last <= BEAT_GAP:
        secs += now - last
    prog["seconds"] = secs
    prog["last_beat"] = now if running else None
    prog["running"] = running
    roster.save_progress(task_id, prog)
    need = int(t["params"].get("minutes", 0)) * 60
    return {"seconds": int(secs), "needed": need, "timer_done": secs >= need}


def finish_timer(task_id, note):
    t = _today_task(task_id)
    need = int(t["params"].get("minutes", 0)) * 60
    secs = float(t["progress"].get("seconds", 0))
    if secs < need:
        raise ActionError(f"The timer isn't finished: {int(secs // 60)} of {need // 60} minutes.")
    min_words = int(t["params"].get("note_words", 0))
    if min_words:
        ok, msg = dutch.check(note, min_words)
        if not ok:
            raise ActionError(msg)
    return roster.complete(task_id, {"note": note, "seconds": int(secs)})


# ---------------------------------------------------------------- notes & honour


def submit_note(task_id, text):
    t = _today_task(task_id)
    if t["kind"] != "note":
        raise ActionError("Not a writing task.")
    ok, msg = dutch.check(text, int(t["params"].get("min_words", 30)))
    if not ok:
        raise ActionError(msg)
    return roster.complete(task_id, {"note": text})


def submit_honor(task_id, note, link=""):
    t = _today_task(task_id)
    if t["kind"] != "honor":
        raise ActionError("Not an honour task.")
    need = int(t["params"].get("note_words", 0))
    if need and len(dutch.words(note)) < need:
        raise ActionError(f"Write at least {need} words about it (Dutch or English).")
    return roster.complete(task_id, {"note": note, "link": link})


# ---------------------------------------------------------------- speaking


def speaking_prompt(task_id):
    t = _today_task(task_id)
    prog = t["progress"]
    if t["params"].get("prompt"):
        return t["params"]["prompt"]
    if not prog.get("prompt") or prog.get("reroll"):
        prompts = db.get_setting("speaking_prompts") or db.SPEAKING_PROMPTS
        prog["prompt"] = random.choice(prompts)
        prog["reroll"] = False
        roster.save_progress(task_id, prog)
    return prog["prompt"]


def new_prompt(task_id):
    t = _today_task(task_id)
    prog = t["progress"]
    prog["reroll"] = True
    roster.save_progress(task_id, prog)
    return speaking_prompt(task_id)


def _duration(path):
    if shutil.which("ffprobe"):
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "default=nw=1:nk=1", path],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            if out and out != "N/A":
                return float(out)
            # WebM from MediaRecorder often lacks a duration header; decode it.
            out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-",
                                  "-progress", "pipe:1"], capture_output=True, text=True, timeout=120).stdout
            ms = [int(line.split("=")[1]) for line in out.splitlines()
                  if line.startswith("out_time_ms=") and line.split("=")[1].isdigit()]
            if ms:
                return max(ms) / 1_000_000
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return None


def save_recording(task_id, data, client_seconds, ext="webm"):
    t = _today_task(task_id)
    if t["kind"] != "recording":
        raise ActionError("Not a speaking task.")
    if len(data) < 2000:
        raise ActionError("The recording is empty.")
    name = f"{t['day']}_{task_id}_{int(time.time())}.{ext if ext in ('webm', 'ogg', 'm4a', 'wav') else 'webm'}"
    path = config.RECORDING_DIR / name
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)
    secs = _duration(str(path))
    if secs is None:
        # No ffprobe: trust the browser's clock, but sanity-check the size
        # (Opus speech is at least ~1.5 kB per second).
        secs = min(float(client_seconds or 0), len(data) / 1500)
    prompt = speaking_prompt(task_id)
    db.connect().execute("INSERT INTO recordings (task_id, file, seconds, prompt, ts) VALUES (?,?,?,?,?)",
                         (task_id, name, secs, prompt, time.time()))
    need = int(t["params"].get("min_seconds", 60))
    if secs < need:
        return {"ok": False, "seconds": round(secs),
                "message": f"Saved, but it's {round(secs)} s. Record at least {need} s in one go."}
    roster.complete(task_id, {"recording": name, "seconds": round(secs), "prompt": prompt})
    return {"ok": True, "seconds": round(secs), "message": "Goed gedaan! Recording saved."}


def recordings(limit=60):
    return [dict(r) for r in db.connect().execute(
        "SELECT r.*, t.name AS task_name FROM recordings r LEFT JOIN tasks t ON t.id = r.task_id "
        "ORDER BY r.ts DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- textbook


def textbook_target(task_id):
    """Which set this task is about: the next one in the queue, fixed once
    chosen so it doesn't move under you."""
    t = _today_task(task_id)
    prog = t["progress"]
    if prog.get("set_id"):
        return prog
    bid = t["params"].get("textbook_id") or db.get_setting("active_textbook")
    if not bid or not textbook.book(bid):
        return {"error": "Upload your textbook PDF in the Textbook tab first."}
    s = textbook.next_set(bid)
    if s:
        prog.update({"textbook_id": bid, "set_id": s["id"], "title": s["title"], "items": s["items"],
                     "est_min": s["est_min"]})
        roster.save_progress(task_id, prog)
        return prog
    return {"textbook_id": bid, "free": True,
            "message": "Your set queue is empty: choose the pages yourself."}


def submit_textbook(task_id, answers, label=None, page_from=None, page_to=None):
    t = _today_task(task_id)
    if t["kind"] != "textbook":
        raise ActionError("Not a textbook task.")
    target = textbook_target(task_id)
    if target.get("error"):
        raise ActionError(target["error"])
    bid = target["textbook_id"]
    set_id = target.get("set_id")
    if set_id:
        items, title = target["items"], target["title"]
    else:
        if not page_from:
            raise ActionError("Enter the page(s) of the exercise.")
        page_from = int(page_from)
        page_to = int(page_to or page_from)
        title = label or f"p. {page_from}"
        items = [{"label": title, "page_from": page_from, "page_to": page_to}]
        # A free-choice exercise must be substantial.
        if len(dutch.words(answers)) < 15:
            raise ActionError("Answer the whole exercise (at least 15 words) before submitting.")
    try:
        result = textbook.grade(bid, items, answers, title)
    except textbook.TextbookError as e:
        raise ActionError(str(e)) from None
    textbook.save_submission(task_id, bid, set_id, title, items[0]["page_from"], items[-1]["page_to"],
                             answers, result)
    roster.complete(task_id, {"label": title, "score": result.get("score")})
    return result


def practice_textbook(book_id, page_from, page_to, label, answers, set_id=None):
    """Extra textbook practice outside the roster (doesn't count for the lock).
    Practising a queued set does NOT tick it off the queue."""
    if set_id:
        s = textbook.get_set(int(set_id))
        if not s:
            raise ActionError("No such set.")
        items, title = s["items"], s["title"]
    else:
        if not page_from:
            raise ActionError("Enter the page(s) of the exercise.")
        items = [{"label": label or f"p. {page_from}", "page_from": int(page_from),
                  "page_to": int(page_to or page_from)}]
        title = items[0]["label"]
    try:
        result = textbook.grade(book_id, items, answers, title)
    except textbook.TextbookError as e:
        raise ActionError(str(e)) from None
    textbook.save_submission(None, book_id, None, title + " (practice)", items[0]["page_from"],
                             items[-1]["page_to"], answers, result)
    return result
