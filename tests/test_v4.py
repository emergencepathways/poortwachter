"""Tests for v4: textbook practice (oefenen) one question at a time, its timer rules, italki, focus, the new default
plan and the weekday ramp. The exercise bank here is invented for the tests (the owner's textbook never enters the
repository)."""

import datetime as dt
import importlib
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_core import Base, FakeResp  # noqa: E402

BANK = {
    "format": "poortwachter-oefenen/1",
    "units": [
        {"id": "h1-o1", "chapter": 1, "section": "1.2", "title": "Opdracht 1", "pages": [10, 10], "read": [[8, 9]],
         "mode": "auto", "instruction": "Vul het juiste woord in.", "words": ["loopt", "fietst", "café"],
         "minutes": 5,
         "questions": [
             {"n": "1", "prompt": "Hij … elke dag naar zijn werk.", "type": "text", "answer": ["loopt"]},
             {"n": "2", "prompt": "Zij … naar school.", "type": "text", "answer": ["fietst / rijdt"]},
             {"n": "3", "prompt": "We drinken koffie in het …", "type": "text", "answer": ["café"]},
         ]},
        {"id": "h1-o2", "chapter": 1, "section": "1.2", "title": "Opdracht 2 | Samen", "pages": [10, 10],
         "mode": "partner", "instruction": "Interview elkaar.", "questions": []},
        {"id": "h1-o3", "chapter": 1, "section": "1.3", "title": "Opdracht 3", "pages": [11, 11], "mode": "auto",
         "instruction": "Wie zegt het?", "minutes": 4,
         "questions": [
             {"n": "1", "prompt": "Ik woon in een groot huis.", "type": "choice", "options": ["Anna", "Bram"],
              "answer": ["Anna"]},
             {"n": "2", "prompt": "Ik heb een hond.", "type": "choice", "options": ["Anna", "Bram"], "answer": ["Bram"]},
         ]},
        {"id": "h1-o4", "chapter": 1, "section": "1.4", "title": "Opdracht 4", "pages": [12, 12], "read": [[12, 12]],
         "mode": "open", "instruction": "Maak een zin met inversie.", "minutes": 6,
         "questions": [{"n": "1", "prompt": "Morgen …", "type": "sentence", "answer": ["Morgen ga ik naar huis."]}]},
        {"id": "h2-o1", "chapter": 2, "section": "2.1", "title": "Opdracht 1 | Luisteren", "pages": [20, 20],
         "mode": "listening", "audio": True, "instruction": "Luister en kies.", "minutes": 5,
         "questions": [{"n": "1", "prompt": "Waar is de man?", "type": "choice", "options": ["thuis", "op werk"],
                        "answer": ["thuis"]}]},
    ],
}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class OefenBase(Base):
    def setUp(self):
        super().setUp()
        from poortwachter import oefenen, pto, speech
        for m in (speech, oefenen, pto):
            importlib.reload(m)
        self.oef, self.pto = oefenen, pto
        c = self.db.connect()
        c.execute("INSERT INTO textbooks (id, name, file, pages, page_offset, created_at) VALUES (1, 'Boek', '', 40, 0, 0)")
        self.db.set_setting("active_textbook", 1)
        oefenen.import_bank(1, BANK)
        self.clock = Clock()
        self.patch = mock.patch("poortwachter.oefenen.time.time", self.clock)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def task(self):
        """Today's textbook task (a weekday has one in the plan), or another one as a bonus."""
        r = self.roster
        r.ensure_roster(r.today())
        open_ = [t for t in r.tasks_for(r.today()) if t["kind"] == "textbook" and t["status"] != "done"]
        if open_:
            return open_[0]["id"]
        return r.add_task(r.today(), next(a["id"] for a in r.activities() if a["kind"] == "textbook"), bonus=True)

    def tick(self, tid, seconds, step=10):
        """The open page's heartbeat, every `step` seconds."""
        st = None
        for _ in range(int(seconds // step)):
            self.clock.t += step
            st = self.oef.status(tid, beat=True)
        return st


class CheckTests(OefenBase):
    def test_key_checks(self):
        q = {"type": "text", "answer": ["café"], "model": ""}
        self.assertEqual(self.oef.check(q, " Café. ")["verdict"], "right")
        self.assertEqual(self.oef.check(q, "cafe")["verdict"], "accent")
        self.assertTrue(self.oef.check(q, "cafe")["correct"])
        self.assertEqual(self.oef.check(q, "kafee")["verdict"], "wrong")
        alt = {"type": "text", "answer": ["voordat / totdat"], "model": ""}
        self.assertTrue(self.oef.check(alt, "totdat")["correct"])
        self.assertTrue(self.oef.check(alt, "voordat")["correct"])
        s = {"type": "sentence", "answer": ["Morgen ga ik naar huis."], "model": ""}
        self.assertEqual(self.oef.check(s, "morgen ga ik naar huis")["verdict"], "right")
        self.assertEqual(self.oef.check(s, "Morgen ik ga naar huis.")["verdict"], "review")  # Claude decides
        self.assertEqual(self.oef.check({"type": "open", "answer": [], "model": "x"}, "iets")["verdict"], "open")

    def test_import_keeps_partner_out_and_progress(self):
        ov = self.oef.overview()
        self.assertEqual(len(ov["units"]), 5)
        self.assertEqual(ov["next"], "h1-o1")
        self.oef.set_status("h1-o1", "done")
        self.assertEqual(self.oef.overview()["next"], "h1-o3")  # h1-o2 is with a partner: never served
        self.oef.import_bank(1, BANK)  # re-import keeps progress
        self.assertEqual(self.oef.overview()["next"], "h1-o3")
        with self.assertRaises(self.oef.OefenError):
            self.oef.import_bank(1, {"units": [{"id": "x", "questions": [{"type": "nope"}]}]})


class SessionTests(OefenBase):
    def test_one_question_at_a_time_and_the_twenty_minute_rule(self):
        tid = self.task()
        st = self.oef.next_question(tid)
        self.assertEqual(st["unit"]["id"], "h1-o1")
        self.assertEqual(st["question"]["prompt"], "Hij … elke dag naar zijn werk.")
        self.assertNotIn("answer", st["question"])  # the key never goes to the browser
        self.assertEqual(st["unit"]["read"], [[8, 9]])
        r = self.oef.answer(tid, "loopt")
        self.assertTrue(r["result"]["correct"])
        self.assertEqual(r["state"]["question"]["n"], "2")
        self.oef.answer(tid, "rijdt")
        self.tick(tid, 21 * 60)  # past 20 minutes, but exercise 1 is still open
        self.assertEqual(self.roster.task(tid)["status"], "todo")
        r = self.oef.answer(tid, "cafe")
        self.assertEqual(r["unit_result"]["correct"], 3)  # the accent slip counts, with a hint
        self.assertEqual(self.roster.task(tid)["status"], "done")
        ev = self.roster.task(tid)["evidence"]
        self.assertEqual(ev["units"], ["h1-o1"])
        self.assertFalse(ev["long"])
        self.assertEqual(self.oef.overview()["units"][0]["status"], "done")

    def test_nothing_answered_cancels_the_session(self):
        tid = self.task()
        self.oef.next_question(tid)
        st = self.tick(tid, 20 * 60 + 10)
        self.assertEqual(st["voided"], 1)
        self.assertLess(st["seconds"], 60)
        self.assertEqual(self.roster.task(tid)["status"], "todo")

    def test_closing_the_page_pauses_the_timer(self):
        tid = self.task()
        self.oef.next_question(tid)
        self.tick(tid, 60)
        self.clock.t += 3600  # an hour away
        st = self.oef.status(tid, beat=True)
        self.assertLess(st["seconds"], 120)

    def test_not_possible_and_didnt_work(self):
        tid = self.task()
        self.oef.next_question(tid)
        self.oef.answer(tid, "loopt")
        st = self.oef.not_possible(tid)
        self.assertEqual(st["unit"]["id"], "h1-o3")
        self.assertEqual(st["answered"], 0)  # answers in a taken-out exercise don't count
        self.assertEqual({u["id"]: u["status"] for u in self.oef.overview()["units"]}["h1-o1"], "unsupported")
        st = self.oef.didnt_work(tid)
        self.assertEqual(st["unit"]["id"], "h1-o4")  # h1-o3 skipped today only
        self.assertEqual({u["id"]: u["status"] for u in self.oef.overview()["units"]}["h1-o3"], "open")

    def test_long_session_is_flagged_and_keeping_going_earns_a_bonus_hour(self):
        tid = self.task()
        self.oef.next_question(tid)
        for a in ("loopt", "fietst", "café"):
            self.oef.answer(tid, a)
        self.tick(tid, 20 * 60)
        self.assertEqual(self.roster.task(tid)["status"], "done")
        before = self.pto.balance()
        st = self.oef.next_question(tid)  # keep going
        self.assertEqual(st["unit"]["id"], "h1-o3")
        self.oef.answer(tid, "Anna")
        self.oef.answer(tid, "Bram")
        self.tick(tid, 21 * 60)
        self.assertEqual(self.pto.balance(), before + 1)
        self.tick(tid, 10 * 60)
        self.assertEqual(self.pto.balance(), before + 1, "once per task")

    def test_slow_session_is_marked_long(self):
        tid = self.task()
        self.oef.next_question(tid)
        self.oef.answer(tid, "loopt")
        self.oef.answer(tid, "fietst")
        self.tick(tid, 45 * 60)
        self.oef.answer(tid, "café")
        self.assertTrue(self.roster.task(tid)["evidence"]["long"])

    def test_hardcore_counts_only_seventy_percent(self):
        self.db.set_setting("textbook_hardcore", True)
        tid = self.task()
        self.oef.next_question(tid)
        for a in ("x", "y", "café"):  # 1 of 3
            self.oef.answer(tid, a)
        self.tick(tid, 21 * 60)
        self.assertEqual(self.roster.task(tid)["status"], "todo")
        self.assertEqual({u["id"]: u["status"] for u in self.oef.overview()["units"]}["h1-o1"], "retry")
        st = self.oef.next_question(tid)
        self.assertEqual(st["unit"]["id"], "h1-o3")
        self.oef.answer(tid, "Anna")
        self.oef.answer(tid, "Bram")
        self.assertEqual(self.roster.task(tid)["status"], "done")
        self.assertEqual(self.oef.overview()["next"], "h1-o1", "the failed exercise comes back")

    def test_open_answers_go_to_claude_with_the_key(self):
        self.db.set_setting("claude_api_key", "sk-test")
        self.oef.set_focus("h1-o4")
        tid = self.task()
        self.oef.next_question(tid)
        reply = {"items": [{"n": "1", "correct": False, "corrected": "Morgen ga ik naar huis.",
                            "feedback": "Inversion: the verb comes second."}], "summary": "Goed bezig."}
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResp(json.dumps({"content": [{"type": "text", "text": json.dumps(reply)}]}).encode())) as up:
            r = self.oef.answer(tid, "Morgen ik ga naar huis.")
        sent = json.loads(up.call_args[0][0].data)["messages"][0]["content"]
        self.assertIn("Morgen ga ik naar huis.", sent)  # the key goes along
        self.assertEqual(r["unit_result"]["correct"], 0)
        self.assertEqual(r["unit_result"]["results"][0]["corrected"], "Morgen ga ik naar huis.")

    def test_target_never_under_twenty_minutes(self):
        self.db.set_setting("textbook_minutes", 10)
        self.assertEqual(self.oef.target_seconds(), 20 * 60)
        self.db.set_setting("textbook_minutes", 35)
        self.assertEqual(self.oef.target_seconds(), 35 * 60)


class FocusItalkiTests(OefenBase):
    def test_focus_keeps_the_book_progress(self):
        self.oef.set_focus("h1-o4")
        tid = self.task()
        self.assertEqual(self.oef.next_question(tid)["unit"]["id"], "h1-o4")
        self.oef.set_focus(None)
        self.assertEqual(self.oef.overview()["next"], "h1-o1")

    def test_planned_focus_for_a_day(self):
        r = self.roster
        tid = self.task()
        p = r.task(tid)["params"] | {"focus_unit": "h1-o3"}
        self.db.connect().execute("UPDATE tasks SET params = ? WHERE id = ?", (json.dumps(p), tid))
        self.assertEqual(self.oef.next_question(tid)["unit"]["id"], "h1-o3")

    def test_italki_replaces_the_task_and_asks_where_to_continue(self):
        tid = self.task()
        with self.assertRaises(self.oef.OefenError):
            self.oef.italki(tid, ["h1-o3"], [], "kort")
        self.oef.italki(tid, ["h1-o3"], [], "Met mijn docent heb ik opdracht drie gedaan.")
        self.assertEqual(self.roster.task(tid)["status"], "done")
        self.assertEqual({u["id"]: u["status"] for u in self.oef.overview()["units"]}["h1-o3"], "italki")
        self.assertEqual(self.oef.overview()["next"], "h1-o1", "the book stays where it was")
        tid2 = self.task()
        st = self.oef.status(tid2)
        self.assertEqual(st["resume_choice"]["italki"]["id"], "h1-o4")
        self.assertEqual(st["resume_choice"]["book"]["id"], "h1-o1")
        st = self.oef.resume(tid2, "italki")
        self.assertEqual(st["unit"]["id"], "h1-o4")

    def test_italki_by_pages(self):
        tid = self.task()
        r = self.oef.italki(tid, [], [11, 12], "We hebben pagina elf en twaalf samen gedaan.")
        self.assertEqual(r["units"], ["h1-o3", "h1-o4"])


class AudioTests(OefenBase):
    def test_listening_audio_per_exercise(self):
        self.oef.set_focus("h2-o1")
        tid = self.task()
        st = self.oef.next_question(tid)
        self.assertTrue(st["unit"]["audio"])
        self.assertFalse(st["unit"]["has_audio"])
        self.oef.save_audio("h2-o1", b"ID3" + b"\0" * 2000, "mp3")
        self.assertTrue(self.oef.status(tid)["unit"]["has_audio"])
        with self.assertRaises(self.oef.OefenError):
            self.oef.save_audio("h2-o1", b"x" * 2000, "exe")

    def test_speaking_without_whisper_offers_to_skip(self):
        tid = self.task()
        self.oef.next_question(tid)
        r = self.oef.speak(tid, b"\x1aE\xdf\xa3" + b"\0" * 4000, "webm")
        self.assertTrue(r["speech_failed"])
        self.assertIn("poortwachter-whisper-setup", r["message"])

    def test_speaking_with_whisper(self):
        from poortwachter import speech
        tid = self.task()
        self.oef.next_question(tid)
        with mock.patch.object(speech, "transcribe", return_value="loopt"):
            r = self.oef.speak(tid, b"\x1aE\xdf\xa3" + b"\0" * 4000, "webm")
        self.assertTrue(r["result"]["correct"])
        self.assertEqual(r["result"]["transcript"], "loopt")


class PlanTests(Base):
    def test_recommended_plan(self):
        r = self.roster
        kinds = lambda day: [t["kind"] for t in r.generate_tasks(day) if t["source"] == "core"]  # noqa: E731
        thu, wed, sat, sun = "2026-12-03", "2026-12-02", "2026-12-05", "2026-12-06"
        self.assertEqual(kinds(thu)[:3], ["anki", "textbook", "bw_reading"])
        self.assertIn("bw_cards", kinds(wed))
        self.assertNotIn("anki", kinds(wed))
        self.assertNotIn("textbook", kinds(sat))
        self.assertIn("bw_cards", kinds(sat))
        self.assertEqual(kinds(sun)[:2], ["anki", "bw_reading"])
        diary = [d for d in (thu, wed, sat, sun) if "bw_diary" in kinds(d)]
        self.assertEqual(len(diary), 2, "writing every other day")
        self.assertTrue(all(dt.date.fromisoformat(d).toordinal() % 2 == 0 for d in diary))
        cards = next(a for a in r.activities() if a["kind"] == "bw_cards")
        self.assertEqual(cards["params"]["cards"], 30)

    def test_changed_plans_are_left_alone(self):
        sched = self.db.get_setting("schedule")
        sched["1"]["budget"] = 70
        self.db.set_setting("schedule", sched)
        self.db.set_setting("plan_version", None)
        self.db._migrate_plan(self.db.connect())
        self.assertEqual(self.db.get_setting("schedule")["1"]["budget"], 70)
        self.assertEqual(self.db.get_setting("plan_version"), 3)


class RampTests(Base):
    def test_ramp(self):
        r = self.roster
        self.db.set_setting("ramp", {"enabled": True, "step": 5, "after": 7, "max": 120})
        days = [dt.date(2026, 12, 1) + dt.timedelta(days=i) for i in range(30)]
        weekdays = [d for d in days if d.weekday() < 5]
        for d in weekdays[:7]:
            r.settle_ramp(d.isoformat(), True)
        self.assertEqual(r.ramp_bonus(0), 0, "seven in a row: not yet")
        r.settle_ramp(weekdays[7].isoformat(), True)
        self.assertEqual(r.ramp_bonus(0), 5)
        self.assertFalse(r.settle_ramp("2026-12-12", False), "a Saturday doesn't count")
        r.settle_ramp(weekdays[8].isoformat(), True)
        self.assertEqual(r.ramp_bonus(2), 10)
        self.assertEqual(r.ramp_bonus(5), 0, "weekends stay at 45")
        r.settle_ramp(weekdays[9].isoformat(), False)
        self.assertEqual(r.ramp_bonus(0), 10, "a miss keeps the minutes reached, but restarts the count")
        for d in weekdays[10:17]:
            r.settle_ramp(d.isoformat(), True)
        self.assertEqual(r.ramp_bonus(0), 10)
        for d in weekdays[17:22]:
            r.settle_ramp(d.isoformat(), True)
        self.assertEqual(r.ramp_bonus(0), 30, "never past 120")
        self.assertEqual(sum(t["est_min"] for t in r.generate_tasks("2026-12-30")) >= 110, True)

    def test_ramp_off_changes_nothing(self):
        for i in range(12):
            self.roster.settle_ramp((dt.date(2026, 12, 1) + dt.timedelta(days=i)).isoformat(), True)
        self.assertEqual(self.roster.ramp_bonus(0), 0)


if __name__ == "__main__":
    unittest.main()
