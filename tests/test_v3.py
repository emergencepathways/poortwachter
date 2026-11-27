"""Tests for v3: Boekenwurm pairing and its daily summary, several Anki decks, the diary prompts, LanguageTool hints,
calendars in (iCal → sessions) and out (the plan as a local feed), and textbook pages as images.
The fake Boekenwurm behaves like migrations 0013-0015: a browser handoff with PKCE, today's numbers only."""

import base64
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_core import Base, FakeResp, tiny_pdf  # noqa: E402

TOKEN = "t" * 90


class FakeBoekenwurm(BaseHTTPRequestHandler):
    """connect.json (the site) and the two RPCs (the database), in one fake server."""
    log_message = lambda *a: None  # noqa: E731
    summary = {}
    calls = []
    revoked = False
    approved = True
    links = {}  # code -> challenge, as link_app() would keep them

    def _send(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if "Python-urllib" in (self.headers.get("User-Agent") or ""):
            return self._send({}, 403)  # as Cloudflare does
        if self.path == "/connect.json":
            port = self.server.server_port
            return self._send({"api": 1, "supabase_url": f"http://127.0.0.1:{port}", "publishable_key": "pub"})
        self._send({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeBoekenwurm.calls.append((self.path, body, self.headers.get("apikey")))
        if self.path == "/rest/v1/rpc/claim_app_link":
            challenge = FakeBoekenwurm.links.pop(body["p_code"], None)
            made = base64.urlsafe_b64encode(hashlib.sha256(body["p_verifier"].encode()).digest()).rstrip(b"=").decode()
            if challenge is None or challenge != made:
                return self._send({"error": "invalid", "message": "That connection has expired or was already used."})
            return self._send({"token": TOKEN, "connection_id": "c1", "app": "poortwachter", "status": "ok"})
        if self.path == "/rest/v1/rpc/connection_summary":
            if body.get("p_token") != TOKEN or FakeBoekenwurm.revoked:
                return self._send({"error": "gone", "message": "This app is no longer connected."})
            if not FakeBoekenwurm.approved:
                return self._send({"error": "pending", "message": "Allow it in Boekenwurm."})
            return self._send(dict(FakeBoekenwurm.summary, day=body["p_day"]))
        self._send({}, 404)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class BoekenwurmPairingTests(Base):
    def setUp(self):
        super().setUp()
        FakeBoekenwurm.calls = []
        FakeBoekenwurm.revoked = False
        FakeBoekenwurm.approved = True
        FakeBoekenwurm.links = {}
        FakeBoekenwurm.summary = {"version": 2, "pages": 9, "pages_at_level": 3, "active_minutes": 31.5,
                                  "active_minutes_at_level": 12.0, "cards_reviewed": 25, "diary_done": True}
        self.srv = HTTPServer(("127.0.0.1", 0), FakeBoekenwurm)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.site = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def connect(self, allow=True):
        """The whole handoff: Poortwachter → Boekenwurm (the reader allows) → back to Poortwachter."""
        from poortwachter import db
        db.set_setting("bw_site", self.site)
        url = urllib.parse.urlparse(self.bw.link_start())
        q = dict(urllib.parse.parse_qsl(url.query))
        code = secrets.token_hex(30)
        if allow:
            FakeBoekenwurm.links[code] = q["challenge"]  # link_app()
        return q, code, self.bw.link_finish(code, q["state"])

    def test_link_and_checks(self):
        bw = self.bw
        from poortwachter import db
        db.set_setting("bw_site", self.site)
        url = urllib.parse.urlparse(bw.link_start())
        q = dict(urllib.parse.parse_qsl(url.query))
        # Only the challenge (SHA-256 of the secret) leaves this computer, with a random state and a name.
        self.assertEqual(f"{url.scheme}://{url.netloc}", self.site)
        self.assertEqual(q["connect"], "poortwachter")
        self.assertRegex(q["challenge"], r"^[A-Za-z0-9_-]{43}$")
        self.assertRegex(q["state"], r"^[A-Za-z0-9_-]{16,64}$")
        self.assertTrue(q["name"].startswith("Poortwachter on "))
        secret = bw._links[q["state"]]["verifier"]
        self.assertNotIn(secret, url.query)
        code = secrets.token_hex(30)
        FakeBoekenwurm.links[code] = q["challenge"]
        st = bw.link_finish(code, q["state"])
        self.assertTrue(st["paired"] and st["approved"])
        self.assertTrue(st["paired_name"].startswith("Poortwachter on "))
        path, body, key = FakeBoekenwurm.calls[-1]
        self.assertEqual((path, key), ("/rest/v1/rpc/claim_app_link", "pub"))
        self.assertEqual(body["p_verifier"], secret)
        # The state is used up: the same answer can't be replayed.
        with self.assertRaises(bw.BWError):
            bw.link_finish(code, q["state"])
        day = self.roster.today()
        # 3 pages at level < 5, 12 min at level < 20: not yet
        r = bw.check_reading({"pages": 5, "minutes": 20, "at_level": True}, day)
        self.assertFalse(r["ok"], r)
        self.assertIn("don't count: below your level", r["message"])
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
        # Only the study day is asked for (Boekenwurm works out 02:00 → 02:00 in this time zone): no history window.
        sent = [c for c in FakeBoekenwurm.calls if c[0].endswith("connection_summary")][-1][1]
        self.assertEqual(set(sent), {"p_token", "p_day", "p_tz", "p_start_hour"})
        self.assertEqual(sent["p_day"], day)
        self.assertEqual(sent["p_start_hour"], 2)
        self.assertTrue(sent["p_tz"])

        # The summary is cached for a minute
        n = len(FakeBoekenwurm.calls)
        bw.check_cards({"cards": 1}, day)
        self.assertEqual(len(FakeBoekenwurm.calls), n)

    def test_only_requests_started_here(self):
        bw = self.bw
        with self.assertRaises(bw.BWError) as cm:
            bw.link_finish("ab" * 30, "made-up-state-made-up-state")
        self.assertIn("wasn't started on this computer", str(cm.exception))
        # Another site can't use Poortwachter to start a connection to itself.
        with self.assertRaises(bw.BWError) as cm:
            bw.link_start("https://evil.example")
        self.assertIn("set to connect to", str(cm.exception))

    def test_code_without_allow(self):
        with self.assertRaises(self.bw.BWError) as cm:
            self.connect(allow=False)
        self.assertIn("expired or was already used", str(cm.exception))
        self.assertFalse(self.bw.status()["paired"])

    def test_old_pending_connection(self):
        bw = self.bw
        self.connect()
        FakeBoekenwurm.approved = False  # a connection from the typed-code days, never allowed
        r = bw.check_diary({}, self.roster.today())
        self.assertFalse(r["ok"])
        self.assertIn("allow Poortwachter", r["message"])

    def test_disconnected_in_boekenwurm(self):
        bw = self.bw
        self.connect()
        FakeBoekenwurm.revoked = True
        r = bw.check_diary({}, self.roster.today())
        self.assertFalse(r["ok"])
        self.assertIn("disconnected", r["message"])
        self.assertFalse(bw.status()["paired"])

    def test_site_without_connect_json(self):
        from poortwachter import db
        db.set_setting("bw_site", "http://127.0.0.1:1")
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.bw.link_start()).query))["state"]
        with self.assertRaises(self.bw.BWError):
            self.bw.link_finish("ab" * 30, state)  # nothing there

    def test_pages_in_the_browser(self):
        """The two addresses Boekenwurm and the browser use, through the real web server."""
        from poortwachter import db, server
        db.set_setting("bw_site", self.site)
        srv = HTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_port}"
            opener = urllib.request.build_opener(NoRedirect)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                opener.open(f"{base}/boekenwurm/connect?site={urllib.parse.quote(self.site)}")
            self.assertEqual(cm.exception.code, 302)
            to = cm.exception.headers["Location"]
            self.assertTrue(to.startswith(self.site + "/?connect=poortwachter&"))
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(to).query))
            code = secrets.token_hex(30)
            FakeBoekenwurm.links[code] = q["challenge"]
            page = urllib.request.urlopen(f"{base}/boekenwurm/linked?code={code}&state={q['state']}").read().decode()
            self.assertIn("Connected to Boekenwurm", page)
            self.assertTrue(self.bw.status()["paired"])
            page = urllib.request.urlopen(f"{base}/boekenwurm/linked?code={code}&state=<script>").read().decode()
            self.assertIn("Not connected", page)
            self.assertNotIn("<script>", page)
        finally:
            srv.shutdown()

    def test_diary_task_through_actions(self):
        self.connect()
        r = self.roster
        aid = next(a["id"] for a in r.activities() if a["kind"] == "bw_diary")
        tid = r.add_task(r.today(), aid)
        self.assertTrue(self.actions.check(tid)["ok"])
        self.assertEqual(r.task(tid)["status"], "done")

    def test_no_calendar_is_published(self):
        from poortwachter import ical
        self.assertFalse(hasattr(ical, "publish"))
        self.connect()
        self.assertFalse([c for c in FakeBoekenwurm.calls if "calendar" in c[0]])


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
        self.assertIn("Mystery task", ics.replace("\r\n ", ""))  # lines are folded at 75 octets
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
