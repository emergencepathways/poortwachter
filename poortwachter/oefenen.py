"""Oefenen: the textbook's exercises, one question at a time, in the book's order.

The exercises come from an exercise bank built from the owner's own textbook (see docs/OEFENEN.md for the format).
The bank is private: it is imported into Poortwachter's database and never lives in the repository.

One textbook task is a timed session (at least 20 minutes on weekdays, the owner's choice):

- Questions are served one at a time, in the book's order, from where the book was left off (or from a chapter or
  exercise the owner chose to focus on). Answers that the key can check are graded here, instantly; open answers go
  to Claude (the owner's own key) with the key's model answer; free writing gets feedback only.
- The task counts when the timer has reached the target, at least one question was answered in the session, and the
  exercise that is open is finished. If the timer reaches the target and nothing was answered, the whole session is
  cancelled: the time starts again from zero.
- Going on is always allowed. A session that took twice its target or more is marked "took long" (no policing). Doing
  at least one more exercise after the task was done and reaching twice the target earns a bonus hour toward the next
  free day (once per task).
- Hardcore mode: an exercise counts as done only at 70% or more; below that it comes back later.
- "Not possible" takes an exercise out (it doesn't count); "didn't work" skips one for this session only.
- An italki lesson can replace the day's textbook task, naming the exercises or pages done with the teacher. The book
  stays where it was; the next session asks whether to continue after the italki work or where the book was left.
"""

import difflib
import json
import re
import time
import unicodedata

from . import config, db, roster

PASS = 0.7
MIN_TARGET_MIN = 20


class OefenError(Exception):
    pass


# ---------------------------------------------------------------- the bank


def _book_id(book_id=None):
    bid = book_id or db.get_setting("active_textbook")
    if not bid:
        raise OefenError("Upload your textbook PDF in the Textbook tab first.")
    return int(bid)


def has_bank(book_id=None):
    try:
        bid = _book_id(book_id)
    except OefenError:
        return False
    return db.connect().execute("SELECT 1 FROM bank_units WHERE textbook_id = ? LIMIT 1", (bid,)).fetchone() is not None


QTYPES = ("choice", "text", "sentence", "open", "speak", "listen")
MODES = ("auto", "open", "listening", "speaking", "partner", "website")
NOT_SERVED = ("partner", "website")  # with a partner or teacher, or only on the book's website: never served alone


def _clean_question(q, i):
    if not isinstance(q, dict):
        raise OefenError(f"Question {i + 1} isn't an object.")
    qt = q.get("type") or ("choice" if q.get("options") else "text")
    if qt not in QTYPES:
        raise OefenError(f"Unknown question type {qt!r}.")
    ans = q.get("answer", [])
    ans = [str(a) for a in (ans if isinstance(ans, list) else [ans]) if str(a).strip()]
    return {
        "n": str(q.get("n") or i + 1)[:12],
        "prompt": str(q.get("prompt") or "")[:2000],
        "type": qt,
        "options": [str(o)[:300] for o in (q.get("options") or [])][:20],
        "answer": ans[:20],
        "model": str(q.get("model") or "")[:2000],
        "note": str(q.get("note") or "")[:500],
        "lenient": bool(q.get("lenient")),  # "sometimes more answers are possible": no match → Claude decides
    }


def _clean_unit(u, pos):
    if not isinstance(u, dict) or not u.get("id"):
        raise OefenError(f"Exercise {pos} has no id.")
    mode = u.get("mode") or "auto"
    if mode not in MODES:
        raise OefenError(f"Exercise {u['id']}: unknown mode {mode!r}.")
    qs = [_clean_question(q, i) for i, q in enumerate(u.get("questions") or [])]
    if mode not in NOT_SERVED and not qs:
        raise OefenError(f"Exercise {u['id']} has no questions.")
    pages = u.get("pages") or []
    read = u.get("read") or []
    return {
        "id": str(u["id"])[:40],
        "chapter": int(u.get("chapter") or 0),
        "chapter_title": str(u.get("chapter_title") or "")[:120],
        "section": str(u.get("section") or "")[:20],
        "section_title": str(u.get("section_title") or "")[:160],
        "title": str(u.get("title") or u["id"])[:200],
        "pages": [int(p) for p in pages][:2],
        "read": [[int(r[0]), int(r[-1])] for r in read if r][:6],
        "skill": str(u.get("skill") or "")[:30],
        "mode": mode,
        "instruction": str(u.get("instruction") or "")[:3000],
        "example": str(u.get("example") or "")[:1000],
        "words": [str(w)[:80] for w in (u.get("words") or [])][:40],
        "context": str(u.get("context") or "")[:4000],
        "audio": bool(u.get("audio")),
        "minutes": max(1, min(60, int(u.get("minutes") or 8))),
        "questions": qs,
    }


def import_bank(book_id, data):
    """Replace the book's exercise bank. Progress on exercises that keep their id is kept."""
    bid = _book_id(book_id)
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError:
            raise OefenError("That file isn't valid JSON.") from None
    raw = data.get("units") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise OefenError("No exercises found in that file (it needs a \"units\" list).")
    units = [_clean_unit(u, i + 1) for i, u in enumerate(raw)]
    if len({u["id"] for u in units}) != len(units):
        raise OefenError("Two exercises in that file have the same id.")
    conn = db.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM bank_units WHERE textbook_id = ?", (bid,))
        for i, u in enumerate(units):
            conn.execute("INSERT INTO bank_units (textbook_id, id, position, data) VALUES (?,?,?,?)",
                         (bid, u["id"], i, json.dumps(u)))
        if isinstance(data.get("page_offset"), int):
            conn.execute("UPDATE textbooks SET page_offset = ? WHERE id = ?", (data["page_offset"], bid))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    db.log("oefenen", f"bank imported: {len(units)} exercises")
    return {"units": len(units), "questions": sum(len(u["questions"]) for u in units),
            "auto": sum(1 for u in units for q in u["questions"] if q["type"] in ("choice", "text"))}


def units(book_id=None):
    bid = _book_id(book_id)
    return [json.loads(r["data"]) for r in db.connect().execute(
        "SELECT data FROM bank_units WHERE textbook_id = ? ORDER BY position", (bid,))]


def unit(unit_id, book_id=None):
    bid = _book_id(book_id)
    r = db.connect().execute("SELECT data FROM bank_units WHERE textbook_id = ? AND id = ?", (bid, unit_id)).fetchone()
    return json.loads(r["data"]) if r else None


def states(book_id=None):
    bid = _book_id(book_id)
    return {r["unit_id"]: dict(r) for r in db.connect().execute(
        "SELECT * FROM unit_state WHERE textbook_id = ?", (bid,))}


def _set_state(bid, unit_id, status, correct=None, total=None, seconds=None):
    db.connect().execute(
        "INSERT INTO unit_state (textbook_id, unit_id, status, correct, total, seconds, ts) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(textbook_id, unit_id) DO UPDATE SET status = excluded.status, "
        "correct = COALESCE(excluded.correct, correct), total = COALESCE(excluded.total, total), "
        "seconds = COALESCE(seconds, 0) + COALESCE(excluded.seconds, 0), ts = excluded.ts",
        (bid, unit_id, status, correct, total, seconds or 0.0, time.time()))


CLOSED = ("done", "italki", "unsupported")


def overview(book_id=None):
    """The whole bank with each exercise's status, for the Textbook tab."""
    bid = _book_id(book_id)
    st = states(bid)
    out = []
    for u in units(bid):
        s = st.get(u["id"], {})
        out.append({k: u[k] for k in ("id", "chapter", "chapter_title", "section", "section_title", "title", "pages",
                                      "read", "mode", "audio", "minutes", "skill")}
                   | {"questions": len(u["questions"]), "status": s.get("status") or "open",
                      "correct": s.get("correct"), "total": s.get("total"), "seconds": round(s.get("seconds") or 0),
                      "has_audio": audio_path(bid, u["id"]) is not None})
    return {"textbook_id": bid, "units": out, "focus": db.get_setting("tb_focus") or {},
            "next": (_next_unit(bid, set()) or {}).get("id")}


def set_status(unit_id, status, book_id=None):
    """From the overview: take an exercise out ("unsupported"), put it back ("open"), or mark it done by hand."""
    bid = _book_id(book_id)
    if status not in ("open", "unsupported", "done"):
        raise OefenError("Unknown status.")
    if not unit(unit_id, bid):
        raise OefenError("No such exercise.")
    if status == "open":
        db.connect().execute("DELETE FROM unit_state WHERE textbook_id = ? AND unit_id = ?", (bid, unit_id))
    else:
        _set_state(bid, unit_id, status)


def set_focus(unit_id, book_id=None):
    """Work from this exercise on (a chapter or a lesson that's relevant now). The book's own progress is kept:
    exercises before it stay open and come back once the focus is cleared."""
    bid = _book_id(book_id)
    if unit_id and not unit(unit_id, bid):
        raise OefenError("No such exercise.")
    db.set_setting("tb_focus", {"textbook_id": bid, "unit_id": unit_id} if unit_id else {})


def _serveable(u, st, skipped):
    s = (st.get(u["id"]) or {}).get("status")
    return u["mode"] not in NOT_SERVED and s not in CLOSED and u["id"] not in skipped


def _next_unit(bid, skipped, focus=None):
    st = states(bid)
    us = units(bid)
    focus = focus if focus is not None else (db.get_setting("tb_focus") or {})
    if focus.get("textbook_id") == bid and focus.get("unit_id"):
        ids = [u["id"] for u in us]
        if focus["unit_id"] in ids:
            start = ids.index(focus["unit_id"])
            for u in us[start:]:
                if _serveable(u, st, skipped):
                    return u
    for u in us:
        if _serveable(u, st, skipped):
            return u
    return None


# ---------------------------------------------------------------- grading


def norm(s):
    s = unicodedata.normalize("NFC", str(s or "")).lower()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("…", "...")
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(" .!?;,")


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _alternatives(ans):
    """'voordat / totdat' and ['a', 'b'] both mean: either is right."""
    out = []
    for a in ans:
        out.extend(x.strip() for x in str(a).split(" / ") if x.strip())
        if " / " in str(a):
            out.append(str(a))
    return out


def _shown(expected):
    return " / ".join(expected) if len(expected) <= 3 else expected[0]


def check(q, given):
    """Grade one answer against the key. Returns {correct, verdict, expected, hint}.
    verdict: right | accent | wrong | review (sentence that isn't an exact match: Claude decides) | open."""
    expected = q["answer"]
    if q["type"] in ("open",) or (q["type"] in ("sentence", "speak") and not expected):
        return {"correct": None, "verdict": "open", "expected": q.get("model") or ""}
    g = norm(given)
    if not g:
        return {"correct": False, "verdict": "wrong", "expected": _shown(expected)}
    alts = _alternatives(expected)
    if any(g == norm(a) for a in alts):
        return {"correct": True, "verdict": "right", "expected": _shown(expected)}
    if any(strip_accents(g) == strip_accents(norm(a)) for a in alts):
        return {"correct": True, "verdict": "accent", "expected": _shown(expected),
                "hint": "Right word; check the accents (é, ë, ï…)."}
    if q["type"] in ("sentence", "speak") or q.get("lenient"):
        best = max((difflib.SequenceMatcher(None, g, norm(a)).ratio() for a in alts), default=0)
        return {"correct": None, "verdict": "review", "expected": _shown(expected), "similarity": round(best, 2)}
    if q["type"] == "text":
        best = max((difflib.SequenceMatcher(None, g, norm(a)).ratio() for a in alts), default=0)
        if best >= 0.85 and len(g) > 4:
            return {"correct": False, "verdict": "wrong", "expected": _shown(expected),
                    "hint": "Close: check the spelling."}
    return {"correct": False, "verdict": "wrong", "expected": _shown(expected)}


# ---------------------------------------------------------------- sessions (a textbook task)


def target_seconds():
    return max(MIN_TARGET_MIN, int(db.get_setting("textbook_minutes") or MIN_TARGET_MIN)) * 60


def _task(task_id):
    t = roster.task(task_id)
    if not t:
        raise OefenError("No such task.")
    if t["day"] != roster.today():
        raise OefenError("Only today's tasks can be worked on.")
    if t["kind"] != "textbook":
        raise OefenError("Not a textbook task.")
    return t


def _save(task_id, prog):
    roster.save_progress(task_id, prog)


def _public_question(u, qi):
    q = u["questions"][qi]
    return {k: q[k] for k in ("n", "prompt", "type", "options", "note")} | {"index": qi, "of": len(u["questions"])}


def _public_unit(u, bid):
    b = db.connect().execute("SELECT page_offset FROM textbooks WHERE id = ?", (bid,)).fetchone()
    return {k: u[k] for k in ("id", "chapter", "chapter_title", "section", "section_title", "title", "pages", "read",
                              "mode", "skill", "instruction", "example", "words", "context", "audio", "minutes")} | {
        "has_audio": audio_path(bid, u["id"]) is not None, "page_offset": (b["page_offset"] if b else 0) or 0,
        "textbook_id": bid}


def _beat(prog, running=True):
    from .actions import BEAT_GAP
    now = time.time()
    last = prog.get("last_beat")
    if running and last and now - last <= BEAT_GAP:
        prog["seconds"] = float(prog.get("seconds", 0)) + now - last
        if prog.get("unit"):
            prog["unit_seconds"] = float(prog.get("unit_seconds", 0)) + now - last
    prog["last_beat"] = now if running else None


def _void_if_idle(prog, target):
    """The timer reached the target and nothing was answered: the session doesn't count. Start again from zero."""
    if prog.get("seconds", 0) >= target and not prog.get("answered"):
        prog.update({"seconds": 0.0, "unit": None, "q": 0, "answers": {}, "unit_seconds": 0.0,
                     "voided": int(prog.get("voided", 0)) + 1})
        return True
    return False


def _maybe_complete(t, prog, target):
    """Done when the target time is reached, something was answered, and no exercise is half-done."""
    if t["status"] == "done":
        return False
    if prog.get("seconds", 0) < target or not prog.get("answered") or prog.get("unit"):
        return False
    if db.get_setting("textbook_hardcore") and not prog.get("passed_units"):
        return False
    long = prog["seconds"] >= 2 * target
    prog["completed_at_seconds"] = prog["seconds"]
    _save(t["id"], prog)
    roster.complete(t["id"], {"seconds": round(prog["seconds"]), "units": prog.get("units_done", []),
                              "score": {"correct": prog.get("correct", 0), "total": prog.get("total", 0)},
                              "long": long, "voided": prog.get("voided", 0)})
    return True


def _maybe_bonus(t, prog, target):
    """Kept going after the task was done, to twice its time or more, with at least one more exercise: +1 hour toward
    the next free day, once per task (within the cap)."""
    from . import pto
    if prog.get("bonus") or t["status"] != "done" or not prog.get("units_after_done"):
        return False
    if prog.get("seconds", 0) < 2 * target:
        return False
    prog["bonus"] = True
    pto.process()
    if pto.balance() < pto.cap_days() * pto.HOURS_PER_DAY:
        pto._add(t["day"], 1, "bonus: kept going with the textbook to twice the time")
        prog["bonus_hour"] = True
    return True


def status(task_id, beat=False, running=True):
    """The session's state for the study view (and the heartbeat)."""
    t = roster.task(task_id)
    if not t or t["kind"] != "textbook":
        raise OefenError("Not a textbook task.")
    if beat and t["day"] != roster.today():
        raise OefenError("Only today's tasks can be worked on.")
    bid = _book_id(t["params"].get("textbook_id"))
    prog = t["progress"]
    target = target_seconds()
    voided = False
    if beat:
        _beat(prog, running)
        voided = _void_if_idle(prog, target)
        if not _maybe_complete(t, prog, target):
            _save(task_id, prog)
        t = roster.task(task_id)
        if _maybe_bonus(t, prog, target):
            _save(task_id, prog)
    st = _view(t, bid, prog, target)
    st["voided_now"] = voided
    return st


def _view(t, bid, prog, target):
    done = t["status"] == "done"
    u = unit(prog["unit"], bid) if prog.get("unit") else None
    out = {
        "task_id": t["id"], "status": t["status"], "seconds": int(prog.get("seconds", 0)), "target": target,
        "answered": prog.get("answered", 0), "correct": prog.get("correct", 0), "total": prog.get("total", 0),
        "units_done": prog.get("units_done", []), "voided": prog.get("voided", 0),
        "long": prog.get("seconds", 0) >= 2 * target, "bonus_hour": bool(prog.get("bonus_hour")),
        "hardcore": bool(db.get_setting("textbook_hardcore")), "done": done,
        "resume_choice": _resume_choice(bid) if not prog.get("started") else None,
        "unit": _public_unit(u, bid) if u else None,
        "question": _public_question(u, prog.get("q", 0)) if u and prog.get("q", 0) < len(u["questions"]) else None,
        "unit_results": prog.get("results", []) if u else [],
        "last_unit": prog.get("last_unit"),
    }
    out["can_finish"] = (prog.get("seconds", 0) >= target and bool(prog.get("answered")) and not prog.get("unit"))
    return out


def _resume_choice(bid):
    """After italki work: continue after it, or where the book was left off?"""
    r = db.get_setting("tb_resume") or {}
    if r.get("textbook_id") != bid or not r.get("pending"):
        return None
    after = unit(r.get("after_unit"), bid) if r.get("after_unit") else None
    main = _next_unit(bid, set(), focus={})
    if not after or not main or after["id"] == main["id"]:
        return None
    return {"italki": {"id": after["id"], "title": f"{after['section']} {after['title']}"},
            "book": {"id": main["id"], "title": f"{main['section']} {main['title']}"}}


def resume(task_id, choice):
    t = _task(task_id)
    bid = _book_id(t["params"].get("textbook_id"))
    r = db.get_setting("tb_resume") or {}
    if choice == "italki" and r.get("after_unit"):
        set_focus(r["after_unit"], bid)
    elif choice == "book":
        set_focus(None, bid)
    db.set_setting("tb_resume", {**r, "pending": False})
    return next_question(task_id)


def next_question(task_id, skip_unit=False, focus_unit=None):
    """Start the session, or move to the next exercise (skip_unit: "didn't work" or "skip for now")."""
    t = roster.task(task_id)
    if not t or t["kind"] != "textbook" or t["day"] != roster.today():
        raise OefenError("Only today's textbook task can be worked on.")
    bid = _book_id(t["params"].get("textbook_id"))
    prog = t["progress"]
    if not prog.get("started"):
        prog.update({"started": time.time(), "seconds": float(prog.get("seconds", 0)), "answered": 0,
                     "correct": 0, "total": 0, "units_done": [], "skipped": [], "passed_units": 0})
        focus = t["params"].get("focus_unit")
        if focus and unit(focus, bid):
            set_focus(focus, bid)
        r = db.get_setting("tb_resume") or {}
        if r.get("pending"):
            db.set_setting("tb_resume", {**r, "pending": False})
    if focus_unit:
        if not unit(focus_unit, bid):
            raise OefenError("No such exercise.")
        set_focus(focus_unit, bid)
        if prog.get("unit") and not prog.get("answers"):
            prog["unit"] = None
    if skip_unit and prog.get("unit"):
        if prog.get("answers"):
            raise OefenError("You've started this exercise: finish it, or press Not possible.")
        prog.setdefault("skipped", []).append(prog["unit"])
        prog["unit"] = None
    if not prog.get("unit"):
        u = _next_unit(bid, set(prog.get("skipped", [])))
        if not u:
            _save(task_id, prog)
            return _view(t, bid, prog, target_seconds()) | {"empty": True}
        prog.update({"unit": u["id"], "q": 0, "answers": {}, "results": [], "unit_seconds": 0.0})
    prog["last_beat"] = time.time()
    _save(task_id, prog)
    return _view(roster.task(task_id), bid, prog, target_seconds())


def answer(task_id, given, extra=None):
    """Answer the current question. Returns the verdict (and the next question)."""
    t = roster.task(task_id)
    if not t or t["day"] != roster.today() or t["kind"] != "textbook":
        raise OefenError("Only today's textbook task can be worked on.")
    bid = _book_id(t["params"].get("textbook_id"))
    prog = t["progress"]
    if not prog.get("unit"):
        raise OefenError("No exercise is open. Press Start.")
    u = unit(prog["unit"], bid)
    qi = int(prog.get("q", 0))
    if not u or qi >= len(u["questions"]):
        raise OefenError("That exercise has no more questions.")
    q = u["questions"][qi]
    given = str(given or "")[:4000]
    if not given.strip() and q["type"] not in ("listen",):
        raise OefenError("Type an answer first (or press Not possible).")
    res = check(q, given)
    if extra:
        res.update({k: v for k, v in extra.items() if k in ("transcript", "recording", "similarity", "speech_failed")})
    prog.setdefault("answers", {})[q["n"]] = given
    prog.setdefault("results", []).append({"n": q["n"], "prompt": q["prompt"], "given": given} | res)
    prog["answered"] = int(prog.get("answered", 0)) + 1
    prog["q"] = qi + 1
    out = {"result": res}
    if prog["q"] >= len(u["questions"]):
        out["unit_result"] = _finish_unit(t, bid, u, prog)
    _beat(prog, True)
    target = target_seconds()
    if not _maybe_complete(t, prog, target):
        _save(task_id, prog)
    t = roster.task(task_id)
    if _maybe_bonus(t, prog, target):
        _save(task_id, prog)
    out["state"] = _view(t, bid, prog, target)
    return out


def _finish_unit(t, bid, u, prog):
    """Every question answered: grade what the key couldn't (Claude), score it, record it, move on."""
    results = prog.get("results", [])
    review = [r for r in results if r["verdict"] in ("review", "open")]
    feedback = None
    if review:
        try:
            feedback = grade_open(u, results)
        except OefenError as e:
            feedback = {"error": str(e)}
        if feedback and not feedback.get("error"):
            by_n = {str(i.get("n")): i for i in feedback.get("items", [])}
            for r in results:
                f = by_n.get(str(r["n"]))
                if f and r["verdict"] in ("review", "open"):
                    if r["verdict"] == "review" or (r["verdict"] == "open" and r.get("expected")):
                        r["correct"] = bool(f.get("correct"))
                    r["feedback"] = f.get("feedback") or ""
                    r["corrected"] = f.get("corrected") or ""
    scored = [r for r in results if r.get("correct") is not None]
    correct = sum(1 for r in scored if r["correct"])
    total = len(scored)
    pct = correct / total if total else 1.0
    hardcore = bool(db.get_setting("textbook_hardcore"))
    passed = pct >= PASS
    secs = float(prog.get("unit_seconds", 0))
    status = "done" if (passed or not hardcore) else "retry"
    if status == "done":
        _set_state(bid, u["id"], "done", correct, total, secs)
        prog.setdefault("units_done", []).append(u["id"])
        if passed:
            prog["passed_units"] = int(prog.get("passed_units", 0)) + 1
        if t["status"] == "done":
            prog["units_after_done"] = int(prog.get("units_after_done", 0)) + 1
    else:
        _set_state(bid, u["id"], "retry", correct, total, secs)
        prog.setdefault("skipped", []).append(u["id"])
    prog["correct"] = int(prog.get("correct", 0)) + correct
    prog["total"] = int(prog.get("total", 0)) + total
    db.connect().execute(
        "INSERT INTO unit_attempts (textbook_id, unit_id, task_id, day, correct, total, seconds, results, ts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (bid, u["id"], t["id"], t["day"], correct, total, secs, json.dumps(results), time.time()))
    summary = {"unit_id": u["id"], "title": f"{u['section']} {u['title']}", "correct": correct, "total": total,
               "percent": round(pct * 100), "passed": passed, "status": status, "results": results,
               "minutes": round(secs / 60, 1), "long": secs >= 2 * u["minutes"] * 60,
               "summary": (feedback or {}).get("summary", ""), "feedback_error": (feedback or {}).get("error")}
    prog["last_unit"] = summary
    prog.update({"unit": None, "q": 0, "answers": {}, "results": [], "unit_seconds": 0.0})
    return summary


def not_possible(task_id):
    """This exercise can't be done here (needs a partner, a picture that's missing, a bug…): take it out. The time
    spent still counts; nothing in it counts as answered."""
    t = roster.task(task_id)
    if not t or t["day"] != roster.today() or t["kind"] != "textbook":
        raise OefenError("Only today's textbook task can be worked on.")
    bid = _book_id(t["params"].get("textbook_id"))
    prog = t["progress"]
    if not prog.get("unit"):
        raise OefenError("No exercise is open.")
    uid = prog["unit"]
    answered_here = len(prog.get("answers") or {})
    prog["answered"] = max(0, int(prog.get("answered", 0)) - answered_here)
    _set_state(bid, uid, "unsupported")
    db.log("oefenen", f"not possible: {uid}")
    prog.update({"unit": None, "q": 0, "answers": {}, "results": [], "unit_seconds": 0.0})
    _save(task_id, prog)
    return next_question(task_id)


def didnt_work(task_id):
    """A feature didn't work (audio, speech checking…): skip this exercise for today and serve the next one. Unlike
    Not possible, it comes back another day."""
    t = roster.task(task_id)
    if not t or t["day"] != roster.today() or t["kind"] != "textbook":
        raise OefenError("Only today's textbook task can be worked on.")
    prog = t["progress"]
    if not prog.get("unit"):
        raise OefenError("No exercise is open.")
    prog["answered"] = max(0, int(prog.get("answered", 0)) - len(prog.get("answers") or {}))
    prog.setdefault("skipped", []).append(prog["unit"])
    db.log("oefenen", f"didn't work: {prog['unit']}")
    prog.update({"unit": None, "q": 0, "answers": {}, "results": [], "unit_seconds": 0.0})
    _save(task_id, prog)
    return next_question(task_id)


def speak(task_id, data, ext):
    """A spoken answer: saved with the recordings, turned into text on this computer, then graded like a typed one
    (against the key's transcript or model answer)."""
    from . import speech
    t = roster.task(task_id)
    if not t or t["day"] != roster.today() or t["kind"] != "textbook":
        raise OefenError("Only today's textbook task can be worked on.")
    if len(data) < 2000:
        raise OefenError("The recording is empty.")
    ext = ext if ext in ("webm", "ogg", "m4a", "wav") else "webm"
    name = f"{t['day']}_{task_id}_oefen_{int(time.time())}.{ext}"
    path = config.RECORDING_DIR / name
    config.RECORDING_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    path.chmod(0o600)
    try:
        text = speech.transcribe(path)
    except speech.SpeechError as e:
        return {"speech_failed": True, "message": str(e), "recording": name}
    return answer(task_id, text, {"transcript": text, "recording": name})


# ---------------------------------------------------------------- italki replaces the textbook task


def italki(task_id, unit_ids, pages, note):
    """Did the textbook work with an italki teacher: those exercises (or pages) count as done, the task too. The book
    stays where it was; the next session asks where to continue."""
    t = _task(task_id)
    if t["status"] == "done":
        raise OefenError("Already done.")
    bid = _book_id(t["params"].get("textbook_id"))
    from . import dutch
    if len(dutch.words(note or "")) < 5:
        raise OefenError("Write a few words about what you did with your teacher (at least 5).")
    us = units(bid)
    chosen = [u for u in us if u["id"] in set(unit_ids or [])]
    if pages:
        try:
            a, b = (int(x) for x in (list(pages) + list(pages))[:2])
        except (TypeError, ValueError):
            raise OefenError("Pages should be numbers.") from None
        lo, hi = min(a, b), max(a, b)
        chosen += [u for u in us if u["pages"] and u["pages"][0] <= hi and (u["pages"][-1]) >= lo and u not in chosen]
    if not chosen:
        raise OefenError("Choose the exercises (or pages) you did with your teacher.")
    for u in chosen:
        _set_state(bid, u["id"], "italki")
    ids = [u["id"] for u in us]
    last = max(ids.index(u["id"]) for u in chosen)
    after = next((u["id"] for u in us[last + 1:] if u["mode"] not in NOT_SERVED), None)
    db.set_setting("tb_resume", {"textbook_id": bid, "after_unit": after, "pending": bool(after)})
    roster.complete(task_id, {"italki": True, "units": [u["id"] for u in chosen], "note": note})
    return {"ok": True, "units": [u["id"] for u in chosen]}


# ---------------------------------------------------------------- open answers: Claude


OPEN_GRADER = """You are a warm, precise Dutch teacher. A learner (level A2 going to B1) did this exercise from their
textbook, one question at a time.

Exercise: {title}
Instruction (from the book): {instruction}
{context}
The learner's answers, with the answer key where the book has one ("key": the expected or a possible answer):
{rows}

For every row marked CHECK: decide whether the learner's answer is correct. When a key answer is given, the answer is
correct if it means the same and is grammatically right (word order, verb forms, spelling); small punctuation and
capitalisation slips are fine. When the key says "possible answer" or there is no key, judge whether it is a correct,
sensible answer to the question.
For every row marked FEEDBACK (free writing or speaking): don't mark it, give short feedback.

Reply with JSON only:
{{"items": [{{"n": "1", "correct": true, "corrected": "the corrected Dutch, if needed",
  "feedback": "one or two sentences in English: what's wrong and the rule, with the Dutch term"}}],
 "summary": "One or two encouraging sentences in English."}}"""


def grade_open(u, results):
    from . import textbook
    key = db.get_setting("claude_api_key")
    if not key:
        raise OefenError("Add your Claude API key in Settings to have open answers checked. "
                         "Until then, compare with the key shown.")
    rows = []
    for r in results:
        if r["verdict"] not in ("review", "open"):
            continue
        q = next((q for q in u["questions"] if q["n"] == r["n"]), {})
        has_key = bool(q.get("answer"))
        mark = "CHECK" if (r["verdict"] == "review" or q.get("answer") or q.get("model")) else "FEEDBACK"
        keytxt = (" / ".join(q.get("answer") or []) or (("possible answer: " + q["model"]) if q.get("model") else "none"))
        rows.append(f"- n={r['n']} [{mark}] question: {q.get('prompt', '')}\n  learner: {r['given']}\n"
                    f"  key: {keytxt}{'' if has_key else ''}")
    body = {
        "model": db.get_setting("claude_model") or "claude-sonnet-5-5",
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": OPEN_GRADER.format(
            title=f"{u['section']} {u['title']}", instruction=u["instruction"],
            context=("Context: " + u["context"]) if u.get("context") else "", rows="\n".join(rows))}],
    }
    import urllib.error
    import urllib.request
    req = urllib.request.Request(textbook.API_URL, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise OefenError(f"Claude API error {e.code}.") from None
    except urllib.error.URLError as e:
        raise OefenError(f"Couldn't reach the Claude API: {e.reason}") from None
    text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
    try:
        return textbook.parse_result(text)
    except textbook.TextbookError as e:
        raise OefenError(str(e)) from None


# ---------------------------------------------------------------- audio (listening exercises)

AUDIO_EXT = ("mp3", "m4a", "ogg", "wav", "webm", "aac", "flac")


def _audio_dir(bid):
    d = config.STATE_DIR / "audio" / str(bid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def audio_path(bid, unit_id):
    d = config.STATE_DIR / "audio" / str(bid)
    if not d.exists():
        return None
    safe = re.sub(r"[^\w.-]", "_", unit_id)
    for ext in AUDIO_EXT:
        p = d / f"{safe}.{ext}"
        if p.exists():
            return p
    return None


def save_audio(unit_id, data, ext, book_id=None):
    """The book's audio track for a listening or speaking exercise (downloaded by the owner from the book's site)."""
    bid = _book_id(book_id)
    if not unit(unit_id, bid):
        raise OefenError("No such exercise.")
    ext = (ext or "mp3").lower().lstrip(".")
    if ext not in AUDIO_EXT:
        raise OefenError("Use an audio file: mp3, m4a, ogg, wav, webm, aac or flac.")
    if len(data) < 1000:
        raise OefenError("That file is empty.")
    old = audio_path(bid, unit_id)
    if old:
        old.unlink()
    safe = re.sub(r"[^\w.-]", "_", unit_id)
    p = _audio_dir(bid) / f"{safe}.{ext}"
    with open(p, "wb") as f:
        f.write(data)
    p.chmod(0o600)
    return {"ok": True}
