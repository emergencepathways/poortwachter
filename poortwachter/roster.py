"""The daily roster: study day boundaries, generation, carryover, the lock.

Rules in one place:
- A study day runs 02:00 → 02:00.
- Today's roster is fixed once the day starts: tasks can be added, not removed.
- Future days (up to a week ahead) are editable.
- A day's required work fills its minute budget: planned sessions first, then
  core tasks, then a weighted random draw. Category caps (textbook ≤ 45 min,
  reading ≤ 45 min by default) are never exceeded by the draw or the planner.
- When sessions fill the budget, only the "event minimum" task stays required.
- Free days (install day, planned PTO, emergency key with a banked day) have
  no required tasks.
- Up to 2 unfinished tasks carry over to the next working day.
"""

import datetime as dt
import json
import random
import time

from . import config, db

# ---------------------------------------------------------------- study days


def study_day(now=None):
    """The study day a moment belongs to. Before 02:00 it is still yesterday."""
    now = now or dt.datetime.now()
    return (now - dt.timedelta(hours=config.RESET_HOUR)).date()


def today():
    return study_day().isoformat()


def tomorrow():
    return (study_day() + dt.timedelta(days=1)).isoformat()


def max_plan_day():
    ahead = int(db.get_setting("plan_days_ahead") or 7)
    return (study_day() + dt.timedelta(days=ahead)).isoformat()


def plan_days():
    """Tomorrow through a week ahead."""
    ahead = int(db.get_setting("plan_days_ahead") or 7)
    return [(study_day() + dt.timedelta(days=i)).isoformat() for i in range(1, ahead + 1)]


def day_start(day):
    """Local datetime when a study day begins (02:00 on that date)."""
    d = dt.date.fromisoformat(day)
    return dt.datetime.combine(d, dt.time(config.RESET_HOUR))


def day_start_ts(day):
    return day_start(day).timestamp()


def next_reset():
    return day_start(tomorrow())


# ---------------------------------------------------------------- activities


def activities(enabled_only=False):
    q = "SELECT * FROM activities"
    if enabled_only:
        q += " WHERE enabled = 1"
    q += " ORDER BY category, name"
    return [db.row_to_dict(r) for r in db.connect().execute(q)]


def activity(aid):
    return db.row_to_dict(db.connect().execute("SELECT * FROM activities WHERE id = ?", (aid,)).fetchone())


def caps():
    c = {k: int(v) for k, v in (db.get_setting("category_caps") or {}).items() if v}
    if "textbook" in c:  # the textbook cap never undercuts the chosen session length
        c["textbook"] = max(c["textbook"], int(db.get_setting("textbook_minutes") or 20))
    return c


def _textbook_minutes(a):
    """A textbook task takes as long as the next set in the queue."""
    if a["kind"] != "textbook":
        return a["est_min"]
    from . import oefenen, textbook
    bid = a["params"].get("textbook_id") or db.get_setting("active_textbook")
    if bid and oefenen.has_bank(bid):
        return oefenen.target_seconds() // 60
    s = textbook.next_set(bid) if bid else None
    est = s["est_min"] if s else a["est_min"]
    cap = caps().get("textbook")
    return min(est, cap) if cap else est


def _with_minutes(a):
    a = dict(a)
    a["est_min"] = _textbook_minutes(a)
    return a


# ---------------------------------------------------------------- generation


def _fits_cap(category, minutes, cat_minutes):
    cap = caps().get(category)
    return cap is None or cat_minutes.get(category, 0) + minutes <= cap


def _pick_random(budget_left, pool, taken, yesterday_ids, rng, min_fill, cat_minutes):
    """Weighted draw that fills the remaining minutes.

    Weight is lowered for yesterday's activities and for categories already on
    the roster, so days stay varied; category caps are respected."""
    picked = []
    used_ids = {t["activity_id"] for t in taken}
    cat_minutes = dict(cat_minutes)
    while budget_left >= min_fill:
        cats = [t["category"] for t in taken + picked]
        candidates = []
        for a in pool:
            if a["id"] in used_ids:
                continue
            if a["est_min"] > budget_left + 10:  # allow a small overshoot
                continue
            if not _fits_cap(a["category"], a["est_min"], cat_minutes):
                continue
            w = a["weight"]
            if a["id"] in yesterday_ids:
                w *= 0.35
            w *= 0.45 ** cats.count(a["category"])
            if w > 0:
                candidates.append((a, w))
        if not candidates:
            break
        total = sum(w for _, w in candidates)
        r = rng.uniform(0, total)
        acc = 0.0
        choice = candidates[-1][0]
        for a, w in candidates:
            acc += w
            if r <= acc:
                choice = a
                break
        picked.append({"activity_id": choice["id"], "category": choice["category"], "activity": choice})
        used_ids.add(choice["id"])
        cat_minutes[choice["category"]] = cat_minutes.get(choice["category"], 0) + choice["est_min"]
        budget_left -= choice["est_min"]
    return picked


def _task_row(day, a, source, position, mystery=False):
    return {
        "day": day, "activity_id": a["id"], "name": a["name"], "category": a["category"],
        "kind": a["kind"], "params": a["params"], "est_min": a["est_min"], "source": source,
        "mystery": 1 if mystery else 0, "position": position,
    }


def _event_row(day, e, position, counts=1):
    return {
        "day": day, "activity_id": None, "event_id": e["id"], "name": e["name"], "category": e["category"],
        "kind": "honor", "time": e["time"] or "",
        "params": {"note_words": e["note_words"], "link": bool(e["link"]), "event_kind": e["kind"],
                   "place": e["place"], "time": e["time"],
                   "label": "What did you practise? Who did you talk to?"},
        "est_min": e["minutes"], "source": "event", "position": position, "counts": counts,
    }


def _insert_task(t):
    cur = db.connect().execute(
        "INSERT INTO tasks (day, activity_id, event_id, time, name, category, kind, params, est_min, source,"
        " mystery, revealed, status, counts, progress, evidence, carried_from, position)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (t["day"], t.get("activity_id"), t.get("event_id"), t.get("time", ""), t["name"], t["category"],
         t["kind"], json.dumps(t["params"]), t["est_min"], t["source"], t.get("mystery", 0),
         t.get("revealed", 0), t.get("status", "todo"), t.get("counts", 1),
         json.dumps(t.get("progress", {})), json.dumps(t.get("evidence", {})),
         t.get("carried_from"), t["position"]))
    return cur.lastrowid


def _previous_work_day(day):
    """The last earlier day that had a roster and wasn't a free day."""
    row = db.connect().execute(
        "SELECT day FROM rosters WHERE day < ? AND day NOT IN (SELECT day FROM free_days) "
        "ORDER BY day DESC LIMIT 1", (day,)).fetchone()
    return row["day"] if row else None


def ramp_bonus(weekday):
    """Extra weekday minutes earned by the ramp setting (weekends never change)."""
    r = db.get_setting("ramp") or {}
    if not r.get("enabled") or weekday >= 5:
        return 0
    return int((db.get_setting("ramp_state") or {}).get("bonus", 0))


def settle_ramp(day, complete):
    """Called once per settled day: completed weekdays in a row raise the weekday budget by `step` minutes per day
    after `after` days, up to `max` minutes. A missed weekday restarts the count (the minutes reached stay); weekends
    and free days don't count either way. Returns True when the bonus changed."""
    r = db.get_setting("ramp") or {}
    st = dict(db.get_setting("ramp_state") or {"streak": 0, "bonus": 0})
    if dt.date.fromisoformat(day).weekday() >= 5:
        return False
    before = st.get("bonus", 0)
    if complete:
        st["streak"] = int(st.get("streak", 0)) + 1
        if r.get("enabled") and st["streak"] > int(r.get("after", 7)):
            base = int(db.get_setting("schedule")["0"].get("budget", 90))
            room = max(0, int(r.get("max", 120)) - base)
            st["bonus"] = min(room, int(st.get("bonus", 0)) + int(r.get("step", 5)))
    else:
        st["streak"] = 0
    st["through"] = day
    db.set_setting("ramp_state", st)
    return st.get("bonus", 0) != before


def generate_tasks(day, rng=None):
    """Draw a fresh plan for a day. Returns task dicts (not saved)."""
    from . import events, pto
    rng = rng or random.Random()
    sessions = events.for_day(day)

    if pto.is_free(day):
        # Free day: sessions still show up, but nothing is required.
        return [_event_row(day, e, i, counts=0) for i, e in enumerate(sessions)]

    d = dt.date.fromisoformat(day)
    wd = d.weekday()
    sched = db.get_setting("schedule")[str(wd)]
    budget = int(sched.get("budget", 60)) + ramp_bonus(wd)
    acts = {a["id"]: _with_minutes(a) for a in activities(enabled_only=True)}
    out = [_event_row(day, e, i) for i, e in enumerate(sessions)]
    left = budget - sum(e["minutes"] for e in sessions)

    cat_minutes = {}
    core = []
    core_ids = [i for i in sched.get("core", []) if i in acts]
    if d.toordinal() % 2 == 0:  # every other day (writing, by default)
        core_ids += [i for i in db.get_setting("writing_every_other_day") or [] if i in acts and i not in core_ids]
    if sessions:
        # Event + small minimum: the minimum task is always required; other
        # core tasks only while there's budget left after the sessions.
        minimum = db.get_setting("event_minimum_activity")
        if minimum in acts:
            core.append(acts[minimum])
            left -= acts[minimum]["est_min"]
        for i in core_ids:
            a = acts[i]
            if a in core:
                continue
            if left >= a["est_min"]:
                core.append(a)
                left -= a["est_min"]
    else:
        core = [acts[i] for i in core_ids]
        left -= sum(a["est_min"] for a in core)
    for a in core:
        cat_minutes[a["category"]] = cat_minutes.get(a["category"], 0) + a["est_min"]

    prev = _previous_work_day(day)
    yesterday_ids = set()
    if prev:
        yesterday_ids = {r["activity_id"] for r in db.connect().execute(
            "SELECT activity_id FROM tasks WHERE day = ?", (prev,))}

    pool = [a for a in acts.values() if a["in_pool"] and a["weight"] > 0]
    taken = [{"activity_id": a["id"], "category": a["category"]} for a in core]
    picked = _pick_random(left, pool, taken, yesterday_ids, rng,
                          db.get_setting("min_fill_minutes") or 8, cat_minutes)
    n_mystery = round(len(picked) * float(sched.get("mystery", 0)))
    mystery_idx = set(rng.sample(range(len(picked)), n_mystery)) if n_mystery else set()

    pos = len(out)
    for a in core:
        out.append(_task_row(day, a, "core", pos))
        pos += 1
    for j, t in enumerate(picked):
        out.append(_task_row(day, t["activity"], "random", pos, mystery=j in mystery_idx))
        pos += 1
    return out


def ensure_roster(day):
    """Make sure a roster exists for `day`. Today's roster is frozen on first
    sight and gets carryover added; future days stay editable."""
    conn = db.connect()
    t = today()
    if day == t:
        from . import pto
        pto.process()
    row = conn.execute("SELECT * FROM rosters WHERE day = ?", (day,)).fetchone()
    if row is None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM rosters WHERE day = ?", (day,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO rosters (day, created_at, frozen) VALUES (?, ?, 0)",
                             (day, time.time()))
                for task in generate_tasks(day):
                    _insert_task(task)
                db.log("roster", f"generated {day}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        row = conn.execute("SELECT * FROM rosters WHERE day = ?", (day,)).fetchone()

    if day == t and not row["frozen"]:
        _freeze_with_carryover(day)
    return day


def _category_minutes(day):
    out = {}
    for r in db.connect().execute("SELECT category, est_min FROM tasks WHERE day = ? AND counts = 1 "
                                  "AND source != 'event'", (day,)):
        out[r["category"]] = out.get(r["category"], 0) + r["est_min"]
    return out


def _freeze_with_carryover(day):
    from . import pto
    conn = db.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT frozen FROM rosters WHERE day = ?", (day,)).fetchone()
        if row["frozen"]:
            conn.execute("COMMIT")
            return
        _sync_events(day)
        prev = _previous_work_day(day)
        if prev and not pto.is_free(day):
            unfinished = conn.execute(
                "SELECT * FROM tasks WHERE day = ? AND counts = 1 AND status != 'done' AND source != 'event' "
                "ORDER BY CASE source WHEN 'carry' THEN 0 WHEN 'core' THEN 1 WHEN 'manual' THEN 2 ELSE 3 END,"
                " position", (prev,)).fetchall()
            existing = {r["activity_id"] for r in conn.execute(
                "SELECT activity_id FROM tasks WHERE day = ?", (day,))}
            cat_minutes = _category_minutes(day)
            pos = 1000
            carried = 0
            for r in unfinished:
                if carried >= config.CARRYOVER_CAP:
                    break
                # Already on today's roster: carrying it would just duplicate it.
                if r["activity_id"] is not None and r["activity_id"] in existing:
                    continue
                if not _fits_cap(r["category"], r["est_min"], cat_minutes):
                    continue
                _insert_task({
                    "day": day, "activity_id": r["activity_id"], "name": r["name"],
                    "category": r["category"], "kind": r["kind"], "params": json.loads(r["params"]),
                    "est_min": r["est_min"], "source": "carry", "mystery": r["mystery"],
                    "revealed": r["revealed"], "carried_from": r["id"], "position": pos,
                })
                cat_minutes[r["category"]] = cat_minutes.get(r["category"], 0) + r["est_min"]
                pos += 1
                carried += 1
            if carried:
                db.log("carryover", f"{carried} task(s) from {prev} to {day}")
        conn.execute("UPDATE rosters SET frozen = 1 WHERE day = ?", (day,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _sync_events(day):
    """Hand-edited future days don't get redrawn, but their sessions must still
    match the schedule: add new ones, drop removed ones."""
    from . import events, pto
    conn = db.connect()
    want = {e["id"]: e for e in events.for_day(day)}
    have = {r["event_id"]: r["id"] for r in conn.execute(
        "SELECT id, event_id FROM tasks WHERE day = ? AND event_id IS NOT NULL", (day,))}
    frozen = conn.execute("SELECT frozen FROM rosters WHERE day = ?", (day,)).fetchone()
    for eid, tid in have.items():
        if eid not in want and not (frozen and frozen["frozen"]):
            conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    counts = 0 if pto.is_free(day) else 1
    for eid, e in want.items():
        if eid not in have:
            _insert_task(_event_row(day, e, -1, counts=counts))


# ---------------------------------------------------------------- reading


def task(task_id):
    return db.row_to_dict(db.connect().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def tasks_for(day):
    return [db.row_to_dict(r) for r in db.connect().execute(
        "SELECT * FROM tasks WHERE day = ? ORDER BY counts DESC, position, id", (day,))]


def public_task(t):
    """What the browser may see. Unrevealed mystery tasks hide everything but
    their rough length."""
    t = dict(t)
    if t["mystery"] and not t["revealed"]:
        return {
            "id": t["id"], "day": t["day"], "mystery": 1, "revealed": 0, "status": t["status"],
            "est_min": t["est_min"], "source": t["source"], "counts": t["counts"],
            "name": "Mystery task", "category": "mystery", "kind": "mystery", "position": t["position"],
        }
    return t


def lock_state(day=None):
    from . import pto
    day = day or today()
    ensure_roster(day)
    conn = db.connect()
    unlock = conn.execute("SELECT * FROM unlocks WHERE day = ?", (day,)).fetchone()
    free = pto.free_source(day)
    rows = conn.execute("SELECT status, est_min FROM tasks WHERE day = ? AND counts = 1", (day,)).fetchall()
    total = len(rows)
    done = sum(1 for r in rows if r["status"] == "done")
    minutes_left = sum(r["est_min"] for r in rows if r["status"] != "done")
    if free:
        reason = {"install": "install_day", "planned": "free_day", "emergency": "emergency_free_day"}[free]
    elif done == total:
        reason = "complete"
    elif unlock is not None:
        reason = "emergency"
    else:
        reason = "locked"
    return {
        "day": day,
        "unlocked": reason != "locked",
        "reason": reason,
        "done": done, "total": total, "minutes_left": minutes_left,
        "next_reset": next_reset().isoformat(timespec="minutes"),
    }


# ---------------------------------------------------------------- editing


class EditError(Exception):
    pass


def _check_editable(day):
    if day <= today():
        raise EditError("Today's roster is locked in. You can edit the days ahead.")
    if day > max_plan_day():
        raise EditError("You can plan up to a week ahead.")


def mark_edited(day):
    db.connect().execute("UPDATE rosters SET edited = 1 WHERE day = ?", (day,))


def rebuild(day):
    """Redraw a future day from scratch (or create it)."""
    conn = db.connect()
    if day <= today():
        raise EditError("Today's roster is locked in.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM tasks WHERE day = ?", (day,))
        conn.execute("INSERT OR IGNORE INTO rosters (day, created_at, frozen) VALUES (?, ?, 0)",
                     (day, time.time()))
        for t in generate_tasks(day):
            _insert_task(t)
        conn.execute("UPDATE rosters SET edited = 0 WHERE day = ?", (day,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def regenerate(day):
    _check_editable(day)
    rebuild(day)


def refresh_future():
    """Plan changes apply from tomorrow: redraw future rosters you haven't
    hand-edited, and keep sessions in sync on the ones you have."""
    for r in db.connect().execute("SELECT day, edited FROM rosters WHERE day > ?", (today(),)).fetchall():
        if r["edited"]:
            _sync_events(r["day"])
        else:
            rebuild(r["day"])


def remove_task(task_id):
    t = task(task_id)
    if not t:
        raise EditError("No such task.")
    if t["day"] <= today():
        # Bonus tasks may always be removed; they never gate Steam.
        if t["counts"]:
            raise EditError("Today's roster is locked in.")
    elif t["event_id"]:
        raise EditError("This is a planned session. Skip or end it under Sessions.")
    db.connect().execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    mark_edited(t["day"])


def _check_cap(day, category, minutes):
    if not _fits_cap(category, minutes, _category_minutes(day)):
        cap = caps()[category]
        raise EditError(f"That would put {category} over its {cap}-minute daily limit. "
                        "Add it as a bonus task instead, or change the limit in Settings.")


def add_task(day, activity_id, bonus=False):
    a = activity(activity_id)
    if not a:
        raise EditError("No such activity.")
    if day < today():
        raise EditError("That day is over.")
    if day > max_plan_day():
        raise EditError("You can plan up to a week ahead.")
    a = _with_minutes(a)
    if not bonus:
        _check_cap(day, a["category"], a["est_min"])
    ensure_roster(day)
    pos = db.connect().execute("SELECT COALESCE(MAX(position), 0) + 1 FROM tasks WHERE day = ?",
                               (day,)).fetchone()[0]
    row = _task_row(day, a, "bonus" if bonus else "manual", pos)
    row["counts"] = 0 if bonus else 1
    tid = _insert_task(row)
    if day > today():
        mark_edited(day)
    return tid


def _pool_for(day, exclude_task=None):
    existing = {t["activity_id"] for t in tasks_for(day)}
    cat_minutes = _category_minutes(day)
    if exclude_task:
        cat_minutes[exclude_task["category"]] -= exclude_task["est_min"]
    return [a for a in (_with_minutes(x) for x in activities(enabled_only=True))
            if a["in_pool"] and a["weight"] > 0 and a["id"] not in existing
            and _fits_cap(a["category"], a["est_min"], cat_minutes)]


def add_mystery(day, category=None):
    """Add a hidden random task. Used for "surprise me" in the planner."""
    if day < today():
        raise EditError("That day is over.")
    if day > max_plan_day():
        raise EditError("You can plan up to a week ahead.")
    ensure_roster(day)
    pool = [a for a in _pool_for(day) if category is None or a["category"] == category]
    if not pool:
        raise EditError("Nothing left that fits: every matching activity is on that day already, "
                        "or its category is at its daily limit.")
    rng = random.Random()
    a = rng.choices(pool, weights=[p["weight"] for p in pool])[0]
    pos = db.connect().execute("SELECT COALESCE(MAX(position), 0) + 1 FROM tasks WHERE day = ?",
                               (day,)).fetchone()[0]
    tid = _insert_task(_task_row(day, a, "manual", pos, mystery=True))
    if day > today():
        mark_edited(day)
    return tid


def swap_task(task_id):
    """Swap a random task on a future day for another, similar in length."""
    t = task(task_id)
    if not t:
        raise EditError("No such task.")
    _check_editable(t["day"])
    if t["event_id"]:
        raise EditError("Sessions can't be swapped.")
    pool = _pool_for(t["day"], exclude_task=t)
    if not pool:
        raise EditError("Nothing left in the pool to swap in.")
    similar = [a for a in pool if abs(a["est_min"] - t["est_min"]) <= 8]
    pool = similar or pool
    a = random.Random().choices(pool, weights=[p["weight"] for p in pool])[0]
    db.connect().execute(
        "UPDATE tasks SET activity_id=?, name=?, category=?, kind=?, params=?, est_min=? WHERE id=?",
        (a["id"], a["name"], a["category"], a["kind"], json.dumps(a["params"]), a["est_min"], task_id))
    mark_edited(t["day"])


def mystify(task_id, on=True):
    t = task(task_id)
    if not t:
        raise EditError("No such task.")
    _check_editable(t["day"])
    if t["event_id"]:
        raise EditError("Sessions can't be a mystery: you need to know when to show up!")
    if not on and t["mystery"]:
        raise EditError("Mystery tasks stay a mystery until you start them.")
    db.connect().execute("UPDATE tasks SET mystery = 1, revealed = 0 WHERE id = ?", (task_id,))
    mark_edited(t["day"])


def reveal(task_id):
    t = task(task_id)
    if not t:
        raise EditError("No such task.")
    if t["day"] != today():
        raise EditError("Mystery tasks are revealed on the day itself.")
    db.connect().execute("UPDATE tasks SET revealed = 1 WHERE id = ?", (task_id,))


# ---------------------------------------------------------------- progress


def save_progress(task_id, progress):
    db.connect().execute("UPDATE tasks SET progress = ? WHERE id = ?", (json.dumps(progress), task_id))


def complete(task_id, evidence):
    t = task(task_id)
    db.connect().execute(
        "UPDATE tasks SET status = 'done', completed_at = ?, evidence = ? WHERE id = ?",
        (time.time(), json.dumps(evidence), task_id))
    db.log("done", f"{t['day']}: {t['name']}")
    st = lock_state(t["day"]) if t["day"] == today() else None
    if st and st["reason"] == "complete" and t["counts"]:
        db.log("unlocked", f"{t['day']}: roster complete")
    return st


def history(days=30):
    from . import pto
    conn = db.connect()
    out = []
    for r in conn.execute("SELECT day FROM rosters WHERE day <= ? ORDER BY day DESC LIMIT ?", (today(), days)):
        rows = conn.execute("SELECT status FROM tasks WHERE day = ? AND counts = 1", (r["day"],)).fetchall()
        unlock = conn.execute("SELECT reason FROM unlocks WHERE day = ?", (r["day"],)).fetchone()
        done = sum(1 for x in rows if x["status"] == "done")
        free = pto.free_source(r["day"])
        out.append({"day": r["day"], "done": done, "total": len(rows), "free": free,
                    "complete": done == len(rows) and not free, "emergency": bool(unlock) or free == "emergency"})
    return out


def streak():
    """Completed days in a row. Free days pause the streak; they don't break it."""
    n = 0
    for h in history(400):
        if h["free"]:
            continue
        if h["day"] == today():
            if h["complete"]:
                n += 1
            continue
        if h["complete"]:
            n += 1
        else:
            break
    return n
