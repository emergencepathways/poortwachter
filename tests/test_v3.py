"""Tests for v3: Boekenwurm pairing and its daily summary, several Anki decks, the diary prompts, LanguageTool hints,
calendars in (iCal → sessions) and out (the plan as a feed), and textbook pages as images."""

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_core import Base, FakeResp, tiny_pdf  # noqa: E402

TOKEN = "t" * 90


class FakeBoekenwurm(BaseHTTPRequestHandler):
    """connect.json (the site) and the three RPCs (the database), in one fake server."""
    log_message = lambda *a: None  # noqa: E731
    summary = {}
    calls = []
    revoked = False

    def _send(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/connect.json":
            port = self.server.server_port
            return self._send({"api": 1, "supabase_url": f"http://127.0.0.1:{port}", "publishable_key": "pub"})
        self._send({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeBoekenwurm.calls.append((self.path, body, self.headers.get("apikey")))
        if self.path == "/rest/v1/rpc/claim_pairing_code":
            if body["p_code"].replace("-", "").upper() != "K7QM3XPA":
                return self._send({"message": "That code isn't valid or has expired. Make a new one in Boekenwurm."}, 400)
            return self._send({"token": TOKEN, "connection_id": "c1", "app": "poortwachter"})
        if body.get("p_token") != TOKEN or FakeBoekenwurm.revoked:
            return self._send({"message": "This app is no longer connected to Boekenwurm. Pair it again."}, 401)
        if self.path == "/rest/v1/rpc/connection_summary":
            return self._send(FakeBoekenwurm.summary)
        if self.path == "/rest/v1/rpc/put_app_calendar":
            return self._send("s" * 60 if not body.get("p_new_secret") and len(FakeBoekenwurm.calls) < 50 else "n" * 60)
        self._send({}, 404)


class BoekenwurmPairingTests(Base):
    def setUp(self):
        super().setUp()
        FakeBoekenwurm.calls = []
        FakeBoekenwurm.revoked = False
        FakeBoekenwurm.summary = {"version": 1, "sessions": 2, "paragraphs": 30, "paragraphs_at_level": 20,
                                  "pages": 9, "pages_at_level": 3, "active_minutes": 31.5,
                                  "active_minutes_at_level": 12.0, "cards_reviewed": 25, "diary_done": True,
                                  "rank": "A2"}
        self.srv = HTTPServer(("127.0.0.1", 0), FakeBoekenwurm)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.site = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def test_pair_and_checks(self):
        bw = self.bw
        with self.assertRaises(bw.BWError) as cm:
            bw.pair("ZZZZ-ZZZZ", self.site)
        self.assertIn("didn't work", str(cm.exception))
        st = bw.pair("k7qm-3xpa", self.site)
        self.assertTrue(st["paired"])
        self.assertTrue(st["paired_name"].startswith("Poortwachter on "))
        path, body, key = FakeBoekenwurm.calls[-1]
        self.assertEqual(key, "pub")
        day = self.roster.today()
        # 3 pages at level < 5, 12 min at level < 20: not yet
        r = bw.check_reading({"pages": 5, "minutes": 20, "at_level": True}, day)
        self.assertFalse(r["ok"], r)
        self.assertIn("below your level don't count", r["message"])
        # Counting everything: 9 pages >= 5
        bw._cache.clear()
        self.assertTrue(bw.check_reading({"pages": 5, "minutes": 20, "at_level": False}, day)["ok"])
        # Minutes alone can do it: stuck on a hard page
        bw._cache.clear()
        self.assertTrue(bw.check_reading({"pages": 50, "minutes": 10, "at_level": True}, day)["ok"])
        # An older activity ({metric, target}) still works
        bw._cache.clear()
        self.assertTrue(bw.check_reading({"metric": "pages", "target": 9}, day)["ok"])
        self.assertTrue(bw.check_cards({"cards": 25}, day)["ok"])
        self.assertTrue(bw.check_diary({}, day)["ok"])
        # The window is the study day, 02:00 to 02:00, and the diary day is the study day.
        sent = [c for c in FakeBoekenwurm.calls if c[0].endswith("connection_summary")][-1][1]
        start = dt.datetime.fromisoformat(sent["p_from"])
        end = dt.datetime.fromisoformat(sent["p_to"])
        self.assertEqual(end - start, dt.timedelta(days=1))
        self.assertEqual(start.astimezone().hour, 2)
        self.assertEqual(sent["p_day"], day)
        # The summary is cached for a minute
        n = len(FakeBoekenwurm.calls)
        bw.check_cards({"cards": 1}, day)
        self.assertEqual(len(FakeBoekenwurm.calls), n)

    def test_disconnected_in_boekenwurm(self):
        bw = self.bw
        bw.pair("K7QM-3XPA", self.site)
        FakeBoekenwurm.revoked = True
        r = bw.check_diary({}, self.roster.today())
        self.assertFalse(r["ok"])
        self.assertIn("disconnected", r["message"])
        self.assertFalse(bw.status()["paired"])

    def test_site_without_pairing(self):
        with self.assertRaises(self.bw.BWError):
            self.bw.pair("K7QM-3XPA", "http://127.0.0.1:1")  # nothing there

    def test_diary_task_through_actions(self):
        self.bw.pair("K7QM-3XPA", self.site)
        r = self.roster
        aid = next(a["id"] for a in r.activities() if a["kind"] == "bw_diary")
        tid = r.add_task(r.today(), aid)
        self.assertTrue(self.actions.check(tid)["ok"])
        self.assertEqual(r.task(tid)["status"], "done")

    def test_publish_calendar(self):
        from poortwachter import ical
        with self.assertRaises(ical.ICalError):
            ical.publish()
        self.bw.pair("K7QM-3XPA", self.site)
        url = ical.publish()
        self.assertEqual(url, f"{self.site}/functions/v1/calendar?f={'s' * 60}")
        sent = [c for c in FakeBoekenwurm.calls if c[0].endswith("put_app_calendar")]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][1]["p_ics"].startswith("BEGIN:VCALENDAR"))
        ical.publish()  # nothing changed: nothing sent
        self.assertEqual(len([c for c in FakeBoekenwurm.calls if c[0].endswith("put_app_calendar")]), 1)
        url2 = ical.publish(force_new_secret=True)
        self.assertTrue(url2.endswith("n" * 60))


class AnkiDecksTests(Base):
    def test_several_decks(self):
        home = os.path.join(self.tmp, "home")
        prof = os.path.join(home, ".local/share/Anki2/User 1")
        os.makedirs(prof)
        col = os.path.join(prof, "collection.anki2")
        c = sqlite3.connect(col)
        c.executescript("""
            CREATE TABLE col (id integer primary key, crt integer);
            CREATE TABLE decks (id integer primary key, name text);
            CREATE TABLE cards (id integer primary key, did integer, queue integer, due integer);
            CREATE TABLE revlog (id integer primary key, cid integer, type integer);
        """)
        c.execute("INSERT INTO col VALUES (1, ?)", (int(time.time()) - 100 * 86400,))
        c.execute("INSERT INTO decks VALUES (1, 'Werkwoorden'), (2, 'Woordenschat'), (3, 'Woordenschat\x1fH5'), (4, 'Frans')")
        for i, did in enumerate([1] * 10 + [3] * 15 + [4] * 30):
            c.execute("INSERT INTO cards VALUES (?, ?, 2, 99999)", (i + 1, did))
        start_ms = int(self.roster.day_start_ts(self.roster.today()) * 1000)
        for i in range(55):
            c.execute("INSERT INTO revlog VALUES (?, ?, 1)", (start_ms + 1000 + i, i + 1))
        c.commit()
        c.close()
        self.db.set_setting("anki_collection", col)
        day = self.roster.today()
        r = self.anki.check({"decks": ["Werkwoorden", "Woordenschat"], "reviews": 25}, day)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["value"], 25)  # 10 + 15 (subdeck), not Frans
        self.assertIn("in 2 decks", r["message"])
        r = self.anki.check({"decks": ["Werkwoorden", "Duits"], "reviews": 1}, day)
        self.assertIn("“Duits”", r["message"])
        self.assertEqual(self.anki.check({"deck": "Frans", "reviews": 30}, day)["value"], 30)  # older activities
        self.assertEqual(self.anki.check({"decks": [], "reviews": 55}, day)["value"], 55)


class WritingTests(Base):
    def test_diary_prompts_and_new_prompt(self):
        r = self.roster
        aid = next(a["id"] for a in r.activities() if a["name"].startswith("Dagboek"))
        self.assertEqual(r.activity(aid)["params"]["prompt_set"], "diary")
        tid = r.add_task(r.today(), aid)
        p1 = self.actions.task_prompt(tid)
        from poortwachter import diary
        self.assertIn((p1["prompt"], p1["translation"]), diary.PROMPTS)
        self.assertEqual(self.actions.task_prompt(tid), p1, "the prompt stays for the day")
        p2 = self.actions.new_prompt(tid)
        self.assertNotEqual(p2["prompt"], p1["prompt"])
        text = ("Vandaag was een goede dag. Ik ben vroeg opgestaan en heb koffie gedronken met mijn vriendin. "
                "Daarna zijn we naar de markt gegaan en hebben we kaas en brood gekocht. In de middag heb ik "
                "Nederlands gestudeerd en een hoofdstuk gelezen. Het was soms moeilijk maar ik begreep het meeste. "
                "Morgen wil ik weer oefenen, want ik heb volgende week een toets en ik wil goed voorbereid zijn.")
        self.actions.submit_note(tid, text)
        self.assertEqual(r.task(tid)["evidence"]["prompt"], p2["prompt"])

    def test_grammar_hints(self):
        from poortwachter import grammar
        reply = {"matches": [{"offset": 3, "length": 4, "message": "Mogelijk een spelfout gevonden.",
                              "replacements": [{"value": "heb"}, {"value": "heeft"}],
                              "rule": {"category": {"name": "Spelling"}}, "sentence": "Ik hebt een boek."}]}
        sent = {}

        def fake(req, timeout=0):
            sent["url"], sent["data"] = req.full_url, req.data.decode()
            return FakeResp(json.dumps(reply).encode())

        with mock.patch.object(grammar.urllib.request, "urlopen", fake):
            r = grammar.check("Ik hebt een boek.")
        self.assertEqual(sent["url"], "https://api.languagetool.org/v2/check")
        self.assertIn("language=nl", sent["data"])
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["matches"][0]["text"], "hebt")
        self.assertEqual(r["matches"][0]["replacements"], ["heb", "heeft"])
        self.db.set_setting("languagetool", False)
        with self.assertRaises(grammar.GrammarError):
            grammar.check("Ik hebt een boek.")


ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:italki-weekly@google.com\r
DTSTART;TZID=Europe/Amsterdam:20261103T190000\r
DTEND;TZID=Europe/Amsterdam:20261103T200000\r
RRULE:FREQ=WEEKLY;BYDAY=TU,TH\r
EXDATE;TZID=Europe/Amsterdam:20261201T190000\r
SUMMARY:italki les met Sanne\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:italki-weekly@google.com\r
RECURRENCE-ID;TZID=Europe/Amsterdam:20261126T190000\r
DTSTART;TZID=Europe/Amsterdam:20261126T183000\r
DTEND;TZID=Europe/Amsterdam:20261126T193000\r
SUMMARY:italki les met Sanne (verzet)\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:lib-monthly@google.com\r
DTSTART;TZID=Europe/Amsterdam:20261107T140000\r
DURATION:PT2H\r
RRULE:FREQ=MONTHLY;BYDAY=1SA\r
SUMMARY:Taalgroep\\, Centrale Bibliotheek\r
LOCATION:Spui 68\\, Den Haag\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:dentist@google.com\r
DTSTART:20261127T090000Z\r
DTEND:20261127T093000Z\r
SUMMARY:Tandarts\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:allday@google.com\r
DTSTART;VALUE=DATE:20261128\r
DTEND;VALUE=DATE:20261129\r
SUMMARY:italki-dag (hele dag)\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:cancelled@google.com\r
DTSTART;TZID=Europe/Amsterdam:20261130T100000\r
DTEND;TZID=Europe/Amsterdam:20261130T110000\r
STATUS:CANCELLED\r
SUMMARY:italki proefles\r
END:VEVENT\r
END:VCALENDAR\r
"""


class ICalTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["TZ"] = "Europe/Amsterdam"
        time.tzset()
        from poortwachter import ical
        self.ical = ical
        ical._local_tz = lambda: __import__("zoneinfo").ZoneInfo("Europe/Amsterdam")

    def tearDown(self):
        os.environ.pop("TZ", None)
        time.tzset()
        super().tearDown()

    def test_expand(self):
        occ = self.ical.expand(ICS, dt.date(2026, 11, 26), dt.date(2026, 12, 8))
        got = [(o["start"].strftime("%Y-%m-%d %H:%M"), o["summary"], o["minutes"]) for o in occ]
        self.assertIn(("2026-11-26 18:30", "italki les met Sanne (verzet)", 60), got, "moved occurrence")
        self.assertNotIn(("2026-11-26 19:00", "italki les met Sanne", 60), got)
        self.assertIn(("2026-12-03 19:00", "italki les met Sanne", 60), got)
        self.assertNotIn(("2026-12-01 19:00", "italki les met Sanne", 60), got, "EXDATE")
        self.assertIn(("2026-12-08 19:00", "italki les met Sanne", 60), got)
        self.assertIn(("2026-12-05 14:00", "Taalgroep, Centrale Bibliotheek", 120), got, "first Saturday")
        self.assertIn(("2026-11-27 10:00", "Tandarts", 30), got, "UTC converted to Amsterdam")
        self.assertFalse([g for g in got if "hele dag" in g[1]], "all-day events skipped")
        self.assertFalse([g for g in got if "proefles" in g[1]], "cancelled events skipped")

    def test_dst(self):
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART;TZID=Europe/Amsterdam:20261020T190000\r\n"
               "DTEND;TZID=Europe/Amsterdam:20261020T200000\r\nRRULE:FREQ=WEEKLY\r\nSUMMARY:italki\r\nEND:VEVENT\r\n"
               "END:VCALENDAR\r\n")
        occ = self.ical.expand(ics, dt.date(2026, 10, 20), dt.date(2026, 11, 3))
        self.assertEqual([o["start"].strftime("%H:%M") for o in occ], ["19:00", "19:00", "19:00"],
                         "same wall-clock time before and after the clocks change (25 Oct)")

    def test_sync_to_sessions(self):
        from poortwachter import events
        r = self.roster
        self.db.set_setting("ical_feeds", [{"name": "Google", "url": "https://example/x.ics"}])
        rep = self.ical.sync(fetcher=lambda url: ICS)
        rows = [e for e in events.all_events(include_past=True) if e.get("ical_key")]
        names = sorted((e["date"], e["time"], e["kind"]) for e in rows)
        # tomorrow (26 Nov) to a week ahead (2 Dec): moved italki Thu 26 18:30; Tue 1 Dec is an EXDATE;
        # the first-Saturday taalgroep is 5 Dec (outside); the dentist doesn't match a keyword.
        self.assertEqual(names, [("2026-11-26", "18:30", "italki")])
        self.assertEqual(rep["added"], 1)
        r.ensure_roster("2026-11-26")
        t = [x for x in r.tasks_for("2026-11-26") if x["source"] == "event"]
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["est_min"], 60)
        # The event is moved again: the session follows; then cancelled: it goes.
        moved = ICS.replace("DTSTART;TZID=Europe/Amsterdam:20261126T183000", "DTSTART;TZID=Europe/Amsterdam:20261126T170000") \
                   .replace("DTEND;TZID=Europe/Amsterdam:20261126T193000", "DTEND;TZID=Europe/Amsterdam:20261126T180000")
        rep = self.ical.sync(fetcher=lambda url: moved)
        rows = [e for e in events.all_events(include_past=True) if e.get("ical_key")]
        self.assertEqual([(e["date"], e["time"]) for e in rows], [("2026-11-26", "17:00")])
        gone = ICS.replace("SUMMARY:italki les met Sanne (verzet)", "SUMMARY:Iets anders")
        rep = self.ical.sync(fetcher=lambda url: gone)
        self.assertEqual(rep["removed"], 1)
        self.assertFalse([e for e in events.all_events(include_past=True) if e.get("ical_key")])

    def test_unreachable_calendar_changes_nothing(self):
        from poortwachter import events
        self.db.set_setting("ical_feeds", [{"name": "Google", "url": "https://example/x.ics"}])
        self.ical.sync(fetcher=lambda url: ICS)

        def boom(url):
            raise self.ical.ICalError("offline")
        rep = self.ical.sync(fetcher=boom)
        self.assertEqual(rep["errors"], ["Google: offline"])
        self.assertEqual(len([e for e in events.all_events(include_past=True) if e.get("ical_key")]), 1)

    def test_plan_feed(self):
        from poortwachter import pto
        r = self.roster
        r.ensure_roster(r.today())
        tid = r.add_mystery("2026-11-27")
        pto._add("2026-11-20", 24, "test")
        pto.plan_free_day("2026-11-28")
        ics = self.ical.plan_ics()
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(ics.count("BEGIN:VEVENT"), 8)
        self.assertIn("DTSTART;VALUE=DATE:20261125", ics)
        self.assertIn("Free day", ics)
        self.assertIn("Mystery task", ics)
        hidden = r.task(tid)["name"]
        day27 = ics.split("UID:poortwachter-day-2026-11-27@poortwachter")[1].split("END:VEVENT")[0]
        unfolded = day27.replace("\r\n ", "")
        self.assertIn("Mystery task", unfolded)
        self.assertNotIn(hidden, unfolded)
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75)


class PageImageTests(Base):
    def test_render_page(self):
        tb = self.textbook
        src = os.path.join(self.tmp, "book.pdf")
        tiny_pdf(src, 5)
        bid = tb.add_book("B", src)
        png = tb.render_page(bid, 3)
        with open(png, "rb") as f:
            self.assertEqual(f.read(4), b"\x89PNG")
        with self.assertRaises(tb.TextbookError):
            tb.render_page(bid, 6)


class MigrationTests(Base):
    def test_v2_activities_upgrade(self):
        conn = self.db.connect()
        conn.execute("DELETE FROM activities WHERE kind = 'bw_diary'")
        conn.execute("UPDATE activities SET name = 'Boekenwurm — read 5 pages', params = ? WHERE kind = 'bw_reading'",
                     (json.dumps({"metric": "pages", "target": 7}),))
        self.db.init()
        a = next(x for x in self.roster.activities() if x["kind"] == "bw_reading")
        self.assertEqual(a["params"], {"pages": 7, "minutes": 20, "at_level": True})
        self.assertEqual(a["name"], "Boekenwurm — read 5 pages or 20 minutes")
        self.assertTrue([x for x in self.roster.activities() if x["kind"] == "bw_diary"])


if __name__ == "__main__":
    unittest.main()
