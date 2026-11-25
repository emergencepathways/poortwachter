"""The daily roster: study day boundaries, generation, carryover, the lock."""

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


# ---------------------------------------------------------------- generation


def _pick_random(budget_left, pool, taken, yesterday_ids, rng, min_fill):
    """Weighted draw that fills the remaining minutes.

    Weight is lowered for yesterday's activities and for categories that are
    already on the roster, so days stay varied across flashcards, reading,
    speaking, textbook, writing and listening."""
    picked = []
    used_ids = {t["activity_id"] for t in taken}
    while budget_left >= min_fill:
        cats = [t["category"] for t in taken + picked]
        candidates = []
        for a in pool:
            if a["id"] in used_ids:
                continue
            if a["est_min"] > budget_left + 10:  # allow a small overshoot
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
        budget_left -= choice["est_min"]
    return picked


def _task_row(day, a, source, position, mystery=False):
    return {
        "day": day, "activity_id": a["id"], "name": a["name"], "category": a["category"],
        "kind": a["kind"], "params": a["params"], "est_min": a["est_min"], "source": source,
        "mystery": 1 if mystery else 0, "position": position,
    }


def _insert_task(t):
    cur = db.connect().execute(
        "INSERT INTO tasks (day, activity_id, name, category, kind, params, est_min, source, mystery,"
        " revealed, status, counts, progress, evidence, carried_from, position)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (t["day"], t.get("activity_id"), t["name"], t["category"], t["kind"],
         json.dumps(t["params"]), t["est_min"], t["source"], t.get("mystery", 0),
         t.get("revealed", 0), t.get("status", "todo"), t.get("counts", 1),
         json.dumps(t.get("progress", {})), json.dumps(t.get("evidence", {})),
         t.get("carried_from"), t["position"]))
    return cur.lastrowid


def _previous_roster_day(day):
    row = db.connect().execute("SELECT day FROM rosters WHERE day < ? ORDER BY day DESC LIMIT 1",
                               (day,)).fetchone()
    return row["day"] if row else None


def generate_tasks(day, rng=None):
    """Draw a fresh plan for a day from the weekly template. Returns task dicts (not saved)."""
    rng = rng or random.Random()
    wd = dt.date.fromisoformat(day).weekday()
    sched = db.get_setting("schedule")[str(wd)]
    budget = int(sched.get("budget", 60))
    acts = {a["id"]: a for a in activities(enabled_only=True)}
    core = [acts[i] for i in sched.get("core", []) if i in acts]

    tasks = []
    for a in core:
        tasks.append({"activity_id": a["id"], "category": a["category"], "activity": a, "source": "core"})

    prev = _previous_roster_day(day)
    yesterday_ids = set()
    if prev:
        yesterday_ids = {r["activity_id"] for r in db.connect().execute(
            "SELECT activity_id FROM tasks WHERE day = ?", (prev,))}

    pool = [a for a in acts.values() if a["in_pool"] and a["weight"] > 0]
    left = budget - sum(a["est_min"] for a in core)
    picked = _pick_random(left, pool, tasks, yesterday_ids, rng, db.get_setting("min_fill_minutes") or 8)
    n_mystery = round(len(picked) * float(sched.get("mystery", 0)))
    mystery_idx = set(rng.sample(range(len(picked)), n_mystery)) if n_mystery else set()

    out = []
    for i, t in enumerate(tasks):
        out.append(_task_row(day, t["activity"], "core", i))
    for j, t in enumerate(picked):
        out.append(_task_row(day, t["activity"], "random", len(tasks) + j, mystery=j in mystery_idx))
    return out


def ensure_roster(day):
    """Make sure a roster exists for `day`. Today's roster is frozen on first
    sight and gets carryover added; future days stay editable."""
    conn = db.connect()
    t = today()
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


def _freeze_with_carryover(day):
    conn = db.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT frozen FROM rosters WHERE day = ?", (day,)).fetchone()
        if row["frozen"]:
            conn.execute("COMMIT")
            return
        prev = _previous_roster_day(day)
        if prev:
            unfinished = conn.execute(
                "SELECT * FROM tasks WHERE day = ? AND counts = 1 AND status != 'done' "
                "ORDER BY CASE source WHEN 'carry' THEN 0 WHEN 'core' THEN 1 WHEN 'manual' THEN 2 ELSE 3 END,"
                " position", (prev,)).fetchall()
            existing = {r["activity_id"] for r in conn.execute(
                "SELECT activity_id FROM tasks WHERE day = ?", (day,))}
            pos = 1000
            carried = 0
            for r in unfinished:
                if carried >= config.CARRYOVER_CAP:
                    break
                # A carried task whose activity is already on today's roster
                # would just be a duplicate; skip it.
                if r["activity_id"] is not None and r["activity_id"] in existing:
                    continue
                _insert_task({
                    "day": day, "activity_id": r["activity_id"], "name": r["name"],
                    "category": r["category"], "kind": r["kind"], "params": json.loads(r["params"]),
                    "est_min": r["est_min"], "source": "carry", "mystery": r["mystery"],
                    "revealed": r["revealed"], "carried_from": r["id"], "position": pos,
                })
                pos += 1
                carried += 1
            if carried:
                db.log("carryover", f"{carried} task(s) from {prev} to {day}")
        conn.execute("UPDATE rosters SET frozen = 1 WHERE day = ?", (day,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


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
    t.pop("evidence_private", None)
    if t["mystery"] and not t["revealed"]:
        return {
            "id": t["id"], "day": t["day"], "mystery": 1, "revealed": 0, "status": t["status"],
            "est_min": t["est_min"], "source": t["source"], "counts": t["counts"],
            "name": "Mystery task", "category": "mystery", "kind": "mystery", "position": t["position"],
        }
    return t


def lock_state(day=None):
    day = day or today()
    ensure_roster(day)
    conn = db.connect()
    unlock = conn.execute("SELECT * FROM unlocks WHERE day = ?", (day,)).fetchone()
    rows = conn.execute("SELECT status, est_min FROM tasks WHERE day = ? AND counts = 1", (day,)).fetchall()
    total = len(rows)
    done = sum(1 for r in rows if r["status"] == "done")
    minutes_left = sum(r["est_min"] for r in rows if r["status"] != "done")
    unlocked = done == total or unlock is not None
    return {
        "day": day,
        "unlocked": unlocked,
        "reason": ("emergency" if unlock and done < total else "complete") if unlocked else "locked",
        "done": done, "total": total, "minutes_left": minutes_left,
        "next_reset": next_reset().isoformat(timespec="minutes"),
    }


# ---------------------------------------------------------------- editing (future days only)


class EditError(Exception):
    pass


def _check_editable(day):
    if day <= today():
        raise EditError("Today's roster is locked in. You can edit tomorrow's.")


def mark_edited(day):
    db.connect().execute("UPDATE rosters SET edited = 1 WHERE day = ?", (day,))


def regenerate(day):
    _check_editable(day)
    conn = db.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM tasks WHERE day = ?", (day,))
        for t in generate_tasks(day):
            _insert_task(t)
        conn.execute("UPDATE rosters SET edited = 0 WHERE day = ?", (day,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def remove_task(task_id):
    t = task(task_id)
    if not t:
        raise EditError("No such task.")
    if t["day"] <= today():
        # Bonus tasks may always be removed; they never gate Steam.
        if t["counts"]:
            raise EditError("Today's roster is locked in.")
    db.connect().execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    mark_edited(t["day"])


def add_task(day, activity_id, bonus=False):
    a = activity(activity_id)
    if not a:
        raise EditError("No such activity.")
    if day <= today() and not bonus:
        # Adding to today is fine, and it counts: you can only make today harder.
        pass
    if day < today():
        raise EditError("That day is over.")
    pos = db.connect().execute("SELECT COALESCE(MAX(position), 0) + 1 FROM tasks WHERE day = ?",
                               (day,)).fetchone()[0]
    row = _task_row(day, a, "bonus" if bonus else "manual", pos)
    row["counts"] = 0 if bonus else 1
    tid = _insert_task(row)
    mark_edited(day)
    return tid


def add_mystery(day, category=None):
    """Add a hidden random task. Used for "surprise me" in the planner."""
    if day < today():
        raise EditError("That day is over.")
    existing = {t["activity_id"] for t in tasks_for(day)}
    pool = [a for a in activities(enabled_only=True)
            if a["in_pool"] and a["weight"] > 0 and a["id"] not in existing
            and (category is None or a["category"] == category)]
    if not pool:
        raise EditError("Every pool activity is already on that day.")
    rng = random.Random()
    a = rng.choices(pool, weights=[p["weight"] for p in pool])[0]
    pos = db.connect().execute("SELECT COALESCE(MAX(position), 0) + 1 FROM tasks WHERE day = ?",
                               (day,)).fetchone()[0]
    tid = _insert_task(_task_row(day, a, "manual", pos, mystery=True))
    mark_edited(day)
    return tid


def swap_task(task_id):
    """Swap a random task on a future day for another random one."""
    t = task(task_id)
    if not t:
        raise EditError("No such task.")
    _check_editable(t["day"])
    existing = {x["activity_id"] for x in tasks_for(t["day"])}
    pool = [a for a in activities(enabled_only=True)
            if a["in_pool"] and a["weight"] > 0 and a["id"] not in existing]
    if not pool:
        raise EditError("Nothing left in the pool to swap in.")
    # Prefer something of a similar length so the day's budget holds.
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
    if st and st["unlocked"] and st["reason"] == "complete":
        db.log("unlocked", f"{t['day']}: roster complete")
    return st


def history(days=30):
    conn = db.connect()
    out = []
    for r in conn.execute("SELECT day FROM rosters WHERE day <= ? ORDER BY day DESC LIMIT ?", (today(), days)):
        rows = conn.execute("SELECT status FROM tasks WHERE day = ? AND counts = 1", (r["day"],)).fetchall()
        unlock = conn.execute("SELECT reason FROM unlocks WHERE day = ?", (r["day"],)).fetchone()
        done = sum(1 for x in rows if x["status"] == "done")
        out.append({"day": r["day"], "done": done, "total": len(rows),
                    "complete": done == len(rows), "emergency": bool(unlock)})
    return out


def streak():
    n = 0
    for h in history(400):
        if h["day"] == today():
            if h["complete"]:
                n += 1
            continue
        if h["complete"]:
            n += 1
        else:
            break
    return n
