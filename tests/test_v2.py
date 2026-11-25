"""Tests for the v2 planning features: install day, week planning, sessions,
category caps and earned free days."""

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_core import Base  # noqa: E402


def d(s):
    return dt.date.fromisoformat(s)


class InstallDayTests(Base):
    def test_install_day_is_free_and_lock_starts_at_two(self):
        from poortwachter import db, pto
        r = self.roster
        # Fresh install "today" (undo the Base's pretend-old install).
        db.set_setting("installed_day", None)
        db.connect().execute("DELETE FROM free_days")
        conn = db.connect()
        db.set_setting("installed_day", r.today())
        db.set_setting("pto_processed_through", r.today())
        conn.execute("INSERT INTO free_days (day, source, ts) VALUES (?, 'install', 0)", (r.today(),))
        st = r.lock_state()
        self.assertTrue(st["unlocked"])
        self.assertEqual(st["reason"], "install_day")
        self.assertEqual(st["total"], 0, "nothing is required on install day")
        self.set_now(dt.datetime(2026, 11, 26, 1, 59))
        self.assertTrue(r.lock_state()["unlocked"])
        self.set_now(dt.datetime(2026, 11, 26, 2, 0))
        st = r.lock_state()
        self.assertFalse(st["unlocked"])
        self.assertGreater(st["total"], 0)
        self.assertEqual(pto.balance(), 0, "install day earns nothing")

    def test_real_init_marks_install_day(self):
        from poortwachter import db
        db.connect().execute("DELETE FROM settings WHERE key = 'installed_day'")
        db.init()
        day = db.get_setting("installed_day")
        self.assertIsNotNone(day)
        src = db.connect().execute("SELECT source FROM free_days WHERE day = ?", (day,)).fetchone()
        self.assertEqual(src["source"], "install")


class WeekPlanningTests(Base):
    def test_plan_a_week_ahead(self):
        r = self.roster
        days = r.plan_days()
        self.assertEqual(len(days), 7)
        self.assertEqual(days[0], "2026-11-26")
        self.assertEqual(days[-1], "2026-12-02")
        r.add_task("2026-12-02", 10)
        with self.assertRaises(r.EditError):
            r.add_task("2026-12-03", 10)
        r.ensure_roster("2026-11-30")
        t = r.tasks_for("2026-11-30")[-1]
        r.remove_task(t["id"])
        with self.assertRaises(r.EditError):
            r.regenerate("2026-12-03")


class SessionTests(Base):
    def test_italki_counts_sixty_minutes_of_budget(self):
        from poortwachter import events
        r = self.roster
        events.create({"kind": "italki", "weekday": 3, "time": "19:00"})  # Thursdays
        for _ in range(20):
            r.rebuild("2026-11-26")
            tasks = r.tasks_for("2026-11-26")
            sess = [t for t in tasks if t["source"] == "event"]
            self.assertEqual(len(sess), 1)
            self.assertEqual(sess[0]["est_min"], 60)
            self.assertEqual(sess[0]["time"], "19:00")
            total = sum(t["est_min"] for t in tasks if t["counts"])
            self.assertLessEqual(total, 90 + 10)
            self.assertGreaterEqual(total, 90 - 8)
        # Not on Friday.
        r.rebuild("2026-11-27")
        self.assertFalse([t for t in r.tasks_for("2026-11-27") if t["source"] == "event"])

    def test_session_filling_budget_leaves_only_the_minimum(self):
        from poortwachter import events
        r = self.roster
        events.create({"kind": "library", "date": "2026-11-28", "minutes": 120, "time": "13:00",
                       "place": "Centrale Bibliotheek"})  # Saturday, budget 45
        r.rebuild("2026-11-28")
        req = [t for t in r.tasks_for("2026-11-28") if t["counts"]]
        self.assertEqual(sorted(t["source"] for t in req), ["core", "event"])
        self.assertEqual(next(t for t in req if t["source"] == "core")["name"], "Anki — daily reviews")

    def test_skip_and_end_weekly_session(self):
        from poortwachter import events, server
        r = self.roster
        eid = events.create({"kind": "library", "weekday": 3, "minutes": 90})
        r.ensure_roster("2026-11-26")
        # Hand-edit Thursday, then skip the session that week: it still disappears.
        r.add_task("2026-11-26", 10)
        events.skip(eid, "2026-11-26")
        server._after_session_change()
        self.assertFalse([t for t in r.tasks_for("2026-11-26") if t["source"] == "event"])
        with self.assertRaises(events.EventError):
            events.skip(eid, r.today())
        events.end(eid)
        self.assertFalse(events.occurs_on(events.event(eid) or {"active": 0}, "2026-12-03"))

    def test_session_added_for_today_is_required(self):
        from poortwachter import events, server
        r = self.roster
        before = r.lock_state()["total"]
        events.create({"kind": "other", "name": "Taalcafé", "date": r.today(), "minutes": 60})
        server._after_session_change()
        self.assertEqual(r.lock_state()["total"], before + 1)

    def test_session_task_cannot_be_removed_from_planner(self):
        from poortwachter import events
        r = self.roster
        events.create({"kind": "italki", "date": "2026-11-27"})
        r.ensure_roster("2026-11-27")
        t = next(t for t in r.tasks_for("2026-11-27") if t["source"] == "event")
        with self.assertRaises(r.EditError):
            r.remove_task(t["id"])


class CapTests(Base):
    def test_generator_respects_caps(self):
        r = self.roster
        sched = self.db.get_setting("schedule")
        for wd in sched.values():
            wd["budget"] = 240  # lots of room, so only the caps hold it back
        self.db.set_setting("schedule", sched)
        for _ in range(40):
            tasks = r.generate_tasks("2026-11-30")
            for cat in ("textbook", "reading"):
                mins = sum(t["est_min"] for t in tasks if t["category"] == cat)
                self.assertLessEqual(mins, 45, (cat, tasks))

    def test_planner_and_today_respect_caps(self):
        r = self.roster
        day = "2026-11-30"
        r.ensure_roster(day)
        for t in r.tasks_for(day):
            if t["category"] == "reading":
                r.remove_task(t["id"])
        novel = next(a["id"] for a in r.activities() if a["name"].startswith("Dutch novel"))  # 25
        bw = next(a["id"] for a in r.activities() if a["name"].startswith("Boekenwurm — read"))  # 20
        news = next(a["id"] for a in r.activities() if a["name"].startswith("Dutch news"))  # 15
        r.add_task(day, novel)
        r.add_task(day, bw)
        with self.assertRaises(r.EditError):
            r.add_task(day, news)  # 25 + 20 + 15 > 45
        r.add_task(day, news, bonus=True)  # bonus is never capped


class PTOTests(Base):
    def _complete_day(self, day):
        r = self.roster
        self.set_now(dt.datetime.combine(d(day), dt.time(12)))
        r.ensure_roster(day)
        for t in r.tasks_for(day):
            if t["counts"]:
                r.complete(t["id"], {})

    def _skip_day(self, day):
        self.set_now(dt.datetime.combine(d(day), dt.time(12)))
        self.roster.ensure_roster(day)

    def test_accrual_reset_and_free_day(self):
        from poortwachter import pto
        day = d("2026-11-25")
        for i in range(30):
            self._complete_day((day + dt.timedelta(days=i)).isoformat())
        self._skip_day((day + dt.timedelta(days=30)).isoformat())
        pto.process()
        self.assertEqual(pto.balance(), 30, "30 completed days = 30 h")
        # Miss a day: the 6 partial hours go, the banked day stays.
        self._skip_day((day + dt.timedelta(days=31)).isoformat())
        pto.process()
        self.assertEqual(pto.balance(), 24)
        r = self.roster
        with self.assertRaises(pto.PTOError):
            pto.plan_free_day(r.today())
        with self.assertRaises(pto.PTOError):
            pto.plan_free_day((d(r.today()) + dt.timedelta(days=9)).isoformat())
        free = (d(r.today()) + dt.timedelta(days=2)).isoformat()
        pto.plan_free_day(free)
        self.assertEqual(pto.balance(), 0)
        with self.assertRaises(pto.PTOError):
            pto.plan_free_day((d(r.today()) + dt.timedelta(days=3)).isoformat())
        self.assertFalse([t for t in r.tasks_for(free) if t["counts"]])
        pto.cancel_free_day(free)
        self.assertEqual(pto.balance(), 24)
        self.assertTrue([t for t in r.tasks_for(free) if t["counts"]])
        pto.plan_free_day(free)
        # Build 5 hours, take the free day: the chain pauses, hours are kept.
        start = d(r.today())
        self._complete_day(start.isoformat())
        self._complete_day((start + dt.timedelta(days=1)).isoformat())
        self.set_now(dt.datetime.combine(d(free), dt.time(12)))
        st = r.lock_state()
        self.assertEqual(st["reason"], "free_day")
        self._complete_day((d(free) + dt.timedelta(days=1)).isoformat())
        self._skip_day((d(free) + dt.timedelta(days=2)).isoformat())
        pto.process()
        self.assertEqual(pto.balance(), 3, "2 h before the free day + 1 after; the free day didn't reset them")
        self.assertGreaterEqual(r.streak(), 1)

    def test_cap_grows_yearly(self):
        from poortwachter import pto
        self.db.set_setting("installed_day", "2026-11-01")
        self.assertEqual(pto.cap_days("2026-12-01"), 15)
        self.assertEqual(pto.cap_days("2027-11-01"), 16)
        self.assertEqual(pto.cap_days("2034-01-01"), 20)
        self.assertEqual(pto.cap_days("2040-01-01"), 20)

    def test_accrual_stops_at_cap(self):
        from poortwachter import pto
        pto._add("2026-11-20", 15 * 24, "test top-up")
        self._complete_day("2026-11-25")
        self._skip_day("2026-11-26")
        pto.process()
        self.assertEqual(pto.balance(), 15 * 24)

    def test_emergency_key_uses_a_banked_day(self):
        from poortwachter import pto
        r = self.roster
        pto._add("2026-11-20", 24 + 5, "test")
        r.ensure_roster(r.today())
        self.assertTrue(pto.spend_for_emergency(r.today()))
        st = r.lock_state()
        self.assertTrue(st["unlocked"])
        self.assertEqual(st["reason"], "emergency_free_day")
        self._skip_day("2026-11-26")
        pto.process()
        self.assertEqual(pto.balance(), 5, "the chain wasn't broken")
        self.assertFalse(pto.spend_for_emergency("2026-11-26"))


if __name__ == "__main__":
    unittest.main()
