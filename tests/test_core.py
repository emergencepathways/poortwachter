"""Tests for Poortwachter. Run:  python3 -m unittest discover -s tests -v

Each test gets a throwaway state folder, and a fake clock so we can move
across the 02:00 reset."""

import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pw-test-")
        os.environ["POORTWACHTER_HOME"] = self.tmp
        os.environ["POORTWACHTER_CONF"] = os.path.join(self.tmp, "pw.conf")
        import importlib
        from poortwachter import config, db
        importlib.reload(config)
        db._local.conn = None
        importlib.reload(db)
        from poortwachter import roster, actions, textbook, verify_anki, verify_boekenwurm, dutch
        for m in (roster, actions, textbook, verify_anki, verify_boekenwurm, dutch):
            importlib.reload(m)
        self.config, self.db, self.roster, self.actions = config, db, roster, actions
        self.textbook, self.anki, self.bw, self.dutch = textbook, verify_anki, verify_boekenwurm, dutch
        db.init()
        # Pretend Poortwachter was installed weeks ago, so today is a normal locked day.
        db.connect().execute("DELETE FROM free_days")
        db.set_setting("installed_day", "2026-11-01")
        db.set_setting("pto_processed_through", "2026-11-24")
        self.set_now(dt.datetime(2026, 11, 25, 10, 0))  # a Wednesday

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_now(self, when):
        self.now = when
        r = self.roster
        r.study_day = lambda now=None, _w=when: ((now or _w) - dt.timedelta(hours=2)).date()


# ---------------------------------------------------------------- roster


class RosterTests(Base):
    def test_study_day_resets_at_two(self):
        r = self.roster
        self.set_now(dt.datetime(2026, 11, 26, 1, 59))
        self.assertEqual(r.today(), "2026-11-25")
        self.set_now(dt.datetime(2026, 11, 26, 2, 0))
        self.assertEqual(r.today(), "2026-11-26")

    def test_generation_fills_budget_with_variety(self):
        r = self.roster
        for _ in range(40):
            tasks = r.generate_tasks("2026-12-02")  # Wednesday, 90 min
            total = sum(t["est_min"] for t in tasks)
            self.assertGreaterEqual(total, 90 - 8, tasks)
            self.assertLessEqual(total, 90 + 10)
            self.assertEqual(tasks[0]["source"], "core")
            ids = [t["activity_id"] for t in tasks]
            self.assertEqual(len(ids), len(set(ids)), "no activity twice in a day")
            cats = [t["category"] for t in tasks]
            self.assertGreaterEqual(len(set(cats)), 3)
        weekend = r.generate_tasks("2026-12-05")  # Saturday, 45 min
        self.assertLessEqual(sum(t["est_min"] for t in weekend), 55)

    def test_honor_tasks_not_drawn_randomly(self):
        for _ in range(60):
            names = [t["name"] for t in self.roster.generate_tasks("2026-12-02")]
            self.assertNotIn("italki lesson", names)

    def test_mystery_share(self):
        sched = self.db.get_setting("schedule")
        sched["2"]["mystery"] = 1.0
        self.db.set_setting("schedule", sched)
        tasks = self.roster.generate_tasks("2026-12-02")
        randoms = [t for t in tasks if t["source"] == "random"]
        self.assertTrue(randoms and all(t["mystery"] for t in randoms))
        self.assertFalse(any(t["mystery"] for t in tasks if t["source"] == "core"))

    def test_today_frozen_tomorrow_editable(self):
        r = self.roster
        r.ensure_roster(r.today())
        r.ensure_roster(r.tomorrow())
        today_task = r.tasks_for(r.today())[0]
        with self.assertRaises(r.EditError):
            r.remove_task(today_task["id"])
        with self.assertRaises(r.EditError):
            r.regenerate(r.today())
        tm = r.tasks_for(r.tomorrow())
        r.remove_task(tm[-1]["id"])
        self.assertEqual(len(r.tasks_for(r.tomorrow())), len(tm) - 1)
        # Adding to today is allowed and it counts.
        tid = r.add_task(r.today(), 10)  # Grammar sentences
        self.assertEqual(r.task(tid)["counts"], 1)
        # Bonus tasks can be removed again.
        bid = r.add_task(r.today(), 10, bonus=True)
        r.remove_task(bid)

    def test_carryover_cap_two_and_tomorrow_becomes_today(self):
        r = self.roster
        r.ensure_roster(r.today())
        n_today = len(r.tasks_for(r.today()))
        self.assertGreaterEqual(n_today, 3)
        r.ensure_roster(r.tomorrow())
        planned = [t["activity_id"] for t in r.tasks_for(r.tomorrow())]
        # Finish one task today, leave the rest.
        first = r.tasks_for(r.today())[0]
        r.complete(first["id"], {})
        self.set_now(dt.datetime(2026, 11, 26, 9, 0))
        r.ensure_roster(r.today())
        tasks = r.tasks_for(r.today())
        carried = [t for t in tasks if t["source"] == "carry"]
        self.assertLessEqual(len(carried), 2)
        planned_now = [t["activity_id"] for t in tasks if t["source"] != "carry"]
        self.assertEqual(planned, planned_now, "yesterday's plan for today is kept as-is")
        for c in carried:
            self.assertNotIn(c["activity_id"], planned_now)
        # Freezing happens once: no second carryover.
        r.ensure_roster(r.today())
        self.assertEqual(len(r.tasks_for(r.today())), len(tasks))

    def test_lock_state(self):
        r = self.roster
        st = r.lock_state()
        self.assertFalse(st["unlocked"])
        for t in r.tasks_for(r.today()):
            r.complete(t["id"], {})
        st = r.lock_state()
        self.assertTrue(st["unlocked"])
        self.assertEqual(st["reason"], "complete")
        self.assertEqual(r.streak(), 1)

    def test_emergency_unlock(self):
        r = self.roster
        r.ensure_roster(r.today())
        self.db.connect().execute("INSERT INTO unlocks (day, reason, ts) VALUES (?, 'ziek', 0)", (r.today(),))
        st = r.lock_state()
        self.assertTrue(st["unlocked"])
        self.assertEqual(st["reason"], "emergency")

    def test_mystery_hidden_until_revealed(self):
        r = self.roster
        r.ensure_roster(r.today())
        tid = r.add_mystery(r.today())
        pub = r.public_task(r.task(tid))
        self.assertEqual(pub["kind"], "mystery")
        self.assertNotIn("params", pub)
        with self.assertRaises(self.actions.ActionError):
            self.actions.submit_note(tid, "x")
        r.reveal(tid)
        self.assertNotEqual(r.public_task(r.task(tid))["kind"], "mystery")

    def test_mystery_on_tomorrow_cannot_be_unhidden_or_revealed(self):
        r = self.roster
        r.ensure_roster(r.tomorrow())
        tid = r.add_mystery(r.tomorrow())
        with self.assertRaises(r.EditError):
            r.reveal(tid)
        with self.assertRaises(r.EditError):
            r.mystify(tid, on=False)

    def test_plan_changes_redraw_unedited_tomorrow_only(self):
        from poortwachter import server
        r = self.roster
        r.ensure_roster(r.today())
        r.ensure_roster(r.tomorrow())
        today_before = [t["id"] for t in r.tasks_for(r.today())]
        sched = self.db.get_setting("schedule")
        sched["3"]["budget"] = 30  # Thursday = tomorrow
        self.db.set_setting("schedule", sched)
        server.refresh_future()
        self.assertEqual(today_before, [t["id"] for t in r.tasks_for(r.today())])
        self.assertLessEqual(sum(t["est_min"] for t in r.tasks_for(r.tomorrow())), 40)


# ---------------------------------------------------------------- checks


class CheckTests(Base):
    def test_dutch_check(self):
        ok, _ = self.dutch.check("Vandaag ben ik naar de markt gegaan en heb ik kaas gekocht. Het was erg druk maar ook gezellig.", 15)
        self.assertTrue(ok)
        ok, msg = self.dutch.check("Today I went to the market and I bought some cheese. It was very busy but nice.", 15)
        self.assertFalse(ok)
        ok, _ = self.dutch.check("de de de de de de de de de de de de de de de", 10)
        self.assertFalse(ok)
        ok, msg = self.dutch.check("Ik ben moe.", 20)
        self.assertFalse(ok)
        self.assertIn("3 / 20", msg)

    def _task_of_kind(self, kind):
        r = self.roster
        aid = next(a["id"] for a in r.activities() if a["kind"] == kind)
        return r.add_task(r.today(), aid)

    def test_timer_only_counts_covered_time(self):
        a = self.actions
        tid = self._task_of_kind("timer")
        clock = [1000.0]
        with mock.patch.object(a.time, "time", lambda: clock[0]):
            a.beat(tid, True)
            for _ in range(10):
                clock[0] += 10
                a.beat(tid, True)
            clock[0] += 3600  # panel closed for an hour
            r = a.beat(tid, True)
            self.assertEqual(r["seconds"], 100)
            with self.assertRaises(a.ActionError):
                a.finish_timer(tid, "")
            need = self.roster.task(tid)["params"]["minutes"] * 60
            while clock[0] and a.beat(tid, True)["seconds"] < need:
                clock[0] += 10
        with self.assertRaises(a.ActionError):
            a.finish_timer(tid, "too short")
        a.finish_timer(tid, "Ik heb een hoofdstuk gelezen over een vrouw die in Amsterdam woont en elke dag "
                            "naar haar werk fietst. Het was een mooi verhaal en ik heb veel nieuwe woorden geleerd.")
        self.assertEqual(self.roster.task(tid)["status"], "done")

    def test_honor_needs_note(self):
        r = self.roster
        from poortwachter import events
        events.create({"kind": "italki", "date": r.today(), "time": "18:00"})
        r._sync_events(r.today())
        tid = next(t["id"] for t in r.tasks_for(r.today()) if t["source"] == "event")
        with self.assertRaises(self.actions.ActionError):
            self.actions.submit_honor(tid, "good")
        self.actions.submit_honor(tid, "We oefenden de perfectum en praatten over mijn weekend, heel leuk", "https://www.italki.com/lesson/1")
        self.assertEqual(r.task(tid)["status"], "done")

    def test_cannot_work_on_tomorrow(self):
        r = self.roster
        r.ensure_roster(r.tomorrow())
        t = r.tasks_for(r.tomorrow())[0]
        with self.assertRaises(self.actions.ActionError):
            self.actions.check(t["id"])

    def test_recording_duration(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg missing")
        a = self.actions
        tid = self._task_of_kind("recording")
        out = []
        for secs in (5, 125):
            path = os.path.join(self.tmp, f"r{secs}.webm")
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=300:duration={secs}",
                            "-c:a", "libopus", path], check=True)
            with open(path, "rb") as f:
                out.append(a.save_recording(tid, f.read(), 999))
        self.assertFalse(out[0]["ok"])
        self.assertTrue(out[1]["ok"], out[1])
        self.assertEqual(self.roster.task(tid)["status"], "done")

    def test_anki(self):
        home = os.path.join(self.tmp, "home")
        prof = os.path.join(home, ".local/share/Anki2/User 1")
        os.makedirs(prof)
        col = os.path.join(prof, "collection.anki2")
        c = sqlite3.connect(col)
        crt = int(time.time()) - 400 * 86400
        c.executescript("""
            CREATE TABLE col (id integer primary key, crt integer);
            CREATE TABLE decks (id integer primary key, name text);
            CREATE TABLE cards (id integer primary key, did integer, queue integer, due integer);
            CREATE TABLE revlog (id integer primary key, cid integer, type integer);
        """)
        c.execute("INSERT INTO col VALUES (1, ?)", (crt,))
        c.execute("INSERT INTO decks VALUES (1, 'Default'), (2, 'Nederlands'), (3, 'Nederlands\x1fWerkwoorden')")
        today_n = (int(time.time()) - crt) // 86400
        for i in range(30):
            c.execute("INSERT INTO cards VALUES (?, ?, 2, ?)", (i + 1, 3 if i < 20 else 1, today_n + 3))
        c.execute("INSERT INTO cards VALUES (99, 3, 2, ?)", (today_n - 1,))  # overdue
        start_ms = int(self.roster.day_start_ts(self.roster.today()) * 1000)
        for i in range(30):
            c.execute("INSERT INTO revlog VALUES (?, ?, 1)", (start_ms + 1000 + i, i + 1))
        c.execute("INSERT INTO revlog VALUES (?, 1, 1)", (start_ms - 5000,))  # yesterday
        c.commit()
        c.close()
        self.db.set_setting("anki_collection", col)
        r = self.anki.check({"deck": "", "reviews": 30}, self.roster.today())
        self.assertTrue(r["ok"], r)
        r = self.anki.check({"deck": "Nederlands", "reviews": 21}, self.roster.today())
        self.assertFalse(r["ok"])
        self.assertEqual(r["value"], 20)
        r = self.anki.check({"deck": "Nederlands", "reviews": 20, "clear_due": True}, self.roster.today())
        self.assertFalse(r["ok"])
        self.assertEqual(r["due_left"], 1)
        self.assertIn("Nederlands::Werkwoorden", self.anki.list_decks())
        r = self.anki.check({"deck": "Frans", "reviews": 1}, self.roster.today())
        self.assertIn("not found", r["message"])


# ---------------------------------------------------------------- textbook


def tiny_pdf(path, pages):
    """A valid multi-page PDF with a line of text per page, no dependencies."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for i in range(pages):
        text = f"BT /F1 18 Tf 72 720 Td (Pagina {i + 1}: Oefening {i + 1}) Tj ET".encode()
        objs.append(f"<< /Length {len(text)} >>\nstream\n{text.decode()}\nendstream")
        content = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
                    f"/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {pages} >>"
    out = b"%PDF-1.4\n"
    offsets = []
    for n, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    with open(path, "wb") as f:
        f.write(out)


class FakeResp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TextbookTests(Base):
    def test_sets_extract_and_grade(self):
        tb = self.textbook
        src = os.path.join(self.tmp, "book.pdf")
        tiny_pdf(src, 40)
        bid = tb.add_book("Testboek", src)
        self.assertEqual(tb.book(bid)["pages"], 40)
        tb.set_offset(bid, 2)
        added, bad = tb.add_lines(bid, "H1 Oefening 1; 3\nH1 Oefening 2; 4-5; 15\nnonsense line")
        self.assertEqual((added, bad), (2, ["nonsense line"]))
        n, over = tb.import_sets(bid, json.dumps({"sets": [
            {"title": "Set 3: H2 + review H1", "items": [
                {"label": "H2 Oefening 1", "page_from": 20, "page_to": 21, "minutes": 20},
                {"label": "H1 Oefening 4 (review)", "page_from": 8, "minutes": 10, "review": True}]},
            {"title": "Too long", "est_min": 60, "items": [{"label": "x", "page_from": 30, "minutes": 60}]},
        ]}))
        self.assertEqual((n, over), (2, 1))
        sets = tb.sets(bid)
        self.assertEqual([s["est_min"] for s in sets], [20, 15, 30, 60])

        pdf = tb.extract_pages(tb.book(bid)["file"], [10, 6, 7])
        info = subprocess.run(["pdfinfo", "-"], input=pdf, capture_output=True).stdout.decode()
        self.assertRegex(info, r"Pages:\s+3\n")
        self.assertEqual(tb._ranges([3, 4, 5, 9, 11, 12]), "3-5,9,11-12")

        r = self.roster
        aid = next(a["id"] for a in r.activities() if a["kind"] == "textbook")
        # Two sets done already, so the next is the multi-exercise one.
        tb.mark_set_done(sets[0]["id"])
        tb.mark_set_done(sets[1]["id"])
        tid = r.add_task(r.today(), aid)
        self.assertEqual(r.task(tid)["est_min"], 30, "textbook task takes the next set's minutes")
        target = self.actions.textbook_target(tid)
        self.assertEqual(target["title"], "Set 3: H2 + review H1")

        self.db.set_setting("claude_api_key", "sk-test")
        reply = {"content": [{"type": "text", "text": "Here you go:\n" + json.dumps({
            "exercise_found": True,
            "items": [{"exercise": "H2 Oefening 1", "number": "1", "answer": "Ik heb gisteren gewerkt",
                       "correct": True, "corrected": "Ik heb gisteren gewerkt."},
                      {"exercise": "H1 Oefening 4", "number": "1", "answer": "Hij is naar huis gelopt",
                       "correct": False, "corrected": "Hij is naar huis gelopen.",
                       "rule": "Irregular past participle", "example": "Wij zijn gelopen."}],
            "summary": "Good."})}]}
        sent = {}

        def fake_urlopen(req, timeout=0):
            sent["body"] = json.loads(req.data)
            sent["headers"] = dict(req.header_items())
            return FakeResp(json.dumps(reply).encode())

        with mock.patch.object(tb.urllib.request, "urlopen", fake_urlopen):
            res = self.actions.submit_textbook(tid, "## H2 Oefening 1\n1. Ik heb gisteren gewerkt\n"
                                                    "## H1 Oefening 4\n1. Hij is naar huis gelopt")
        self.assertEqual(res["score"], {"correct": 1, "total": 2})
        self.assertEqual(sent["headers"]["X-api-key"], "sk-test")
        content = sent["body"]["messages"][0]["content"]
        import base64
        sent_pdf = base64.b64decode(content[0]["source"]["data"])
        info = subprocess.run(["pdfinfo", "-"], input=sent_pdf, capture_output=True).stdout.decode()
        self.assertRegex(info, r"Pages:\s+3\n")  # printed 8, 20, 21
        self.assertIn("printed pages 8, 20–21", content[1]["text"])
        self.assertIn("H1 Oefening 4 (review) (page 8) [review]", content[1]["text"])
        text = subprocess.run(["pdftotext", "-", "-"], input=sent_pdf, capture_output=True).stdout.decode()
        self.assertIn("Pagina 10", text)  # printed 8 + offset 2
        self.assertEqual(r.task(tid)["status"], "done")
        self.assertEqual(tb.next_set(bid)["title"], "Too long")

    def test_grade_needs_key(self):
        tb = self.textbook
        src = os.path.join(self.tmp, "book.pdf")
        tiny_pdf(src, 3)
        bid = tb.add_book("B", src)
        with self.assertRaises(tb.TextbookError):
            tb.grade(bid, [{"label": "x", "page_from": 1, "page_to": 1}], "1. a")


# ---------------------------------------------------------------- Boekenwurm (fake Supabase)


class FakeSupabase(BaseHTTPRequestHandler):
    log_message = lambda *a: None  # noqa: E731
    calls = []

    def _send(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeSupabase.calls.append((self.path, body))
        if self.path.startswith("/auth/v1/otp"):
            return self._send({})
        if self.path.startswith("/auth/v1/token?grant_type=pkce"):
            return self._send({"access_token": "A1", "refresh_token": "R1", "expires_in": 3600,
                               "user": {"email": "me@example.com"}})
        if self.path == "/auth/v1/verify":
            if body.get("token") != "123456" or body.get("type") != "email":
                return self._send({"msg": "Token has expired or is invalid"}, 403)
            return self._send({"access_token": "A3", "refresh_token": "R3", "expires_in": 3600,
                               "user": {"email": body["email"]}})
        if self.path.startswith("/auth/v1/token?grant_type=refresh_token"):
            return self._send({"access_token": "A2", "refresh_token": "R2", "expires_in": 3600})
        self._send({}, 404)

    def do_GET(self):
        FakeSupabase.calls.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("/rest/v1/reading_sessions"):
            return self._send([
                {"id": "s1", "started_at": "2026-11-25T09:00:00Z", "ended_at": "2026-11-25T09:20:00Z", "paragraphs": 6,
                 "results": [{"paragraphId": f"p{i}"} for i in range(6)]},
                {"id": "s2", "started_at": "2026-11-25T10:00:00Z", "ended_at": None, "paragraphs": 8,
                 "results": [{"paragraphId": f"q{i}"} for i in range(8)]},
            ])
        if self.path.startswith("/rest/v1/paragraphs"):
            ids = self.path.split("in.(")[1].split(")")[0].split(",")
            rows = []
            for pid in ids:
                if pid.startswith("p"):  # photographed book: 2 paragraphs per printed page
                    rows.append({"id": pid, "book_id": "b1", "page_number": 10 + int(pid[1:]) // 2})
                else:  # EPUB: no pages
                    rows.append({"id": pid, "book_id": "b2", "page_number": None})
            return self._send(rows)
        if self.path.startswith("/rest/v1/cards"):
            return self._send([{"lemma": w} for w in ("huis", "fiets", "kaas")])
        self._send([], 404)


class BoekenwurmTests(Base):
    def test_login_and_counts(self):
        srv = HTTPServer(("127.0.0.1", 0), FakeSupabase)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.db.set_setting("bw_url", f"http://127.0.0.1:{srv.server_port}")
            self.db.set_setting("bw_key", "anon")
            bw = self.bw
            bw.start_login("me@example.com")
            path, body = FakeSupabase.calls[-1]
            self.assertIn("redirect_to=http%3A%2F%2F127.0.0.1%3A7623%2Fbw%2Fcallback", path)
            self.assertEqual(body["code_challenge_method"], "s256")
            self.assertFalse(body["create_user"])
            bw.finish_login_code("CODE")
            self.assertTrue(bw.status()["signed_in"])
            self.assertEqual(bw.status()["email"], "me@example.com")
            stats = bw.reading_today(self.roster.today())
            # 6 photographed paragraphs over 3 printed pages + 8 EPUB paragraphs / 4 = 2 pages
            self.assertEqual(stats["pages"], 5)
            self.assertEqual(stats["paragraphs"], 14)
            self.assertEqual(stats["minutes"], 20)
            r = bw.check_reading({"metric": "pages", "target": 5}, self.roster.today())
            self.assertTrue(r["ok"], r)
            self.assertTrue(bw.check_cards({"cards": 3}, self.roster.today())["ok"])
            # Expired token gets refreshed.
            s = self.db.get_setting("bw_session")
            s["expires_at"] = 0
            self.db.set_setting("bw_session", s)
            bw.cards_today(self.roster.today())
            self.assertEqual(self.db.get_setting("bw_session")["refresh_token"], "R2")
            self.assertEqual(FakeSupabase.calls[-1][1], "Bearer A2")
        finally:
            srv.shutdown()

    def test_sign_in_with_code(self):
        srv = HTTPServer(("127.0.0.1", 0), FakeSupabase)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.db.set_setting("bw_url", f"http://127.0.0.1:{srv.server_port}")
            self.db.set_setting("bw_key", "anon")
            bw = self.bw
            with self.assertRaises(bw.BWError):
                bw.finish_login_otp("123456")  # no email sent yet
            bw.start_login("me@example.com")
            with self.assertRaises(bw.BWError) as cm:
                bw.finish_login_otp("999999")
            self.assertIn("didn't work", str(cm.exception))
            bw.finish_login_otp(" 123 456 ")
            self.assertTrue(bw.status()["signed_in"])
            self.assertEqual(self.db.get_setting("bw_session")["refresh_token"], "R3")
        finally:
            srv.shutdown()

    def test_not_connected_message(self):
        r = self.bw.check_cards({"cards": 1}, self.roster.today())
        self.assertFalse(r["ok"])
        self.assertIn("Settings", r["message"])


# ---------------------------------------------------------------- watchdog


class WatchdogTests(Base):
    def test_finds_and_kills_steam_like_processes(self):
        from poortwachter import watchdog
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        fake_steam = os.path.join(bindir, "steam")
        shutil.copy(shutil.which("sleep"), fake_steam)
        p1 = subprocess.Popen([fake_steam, "300"])
        env = dict(os.environ, SteamAppId="2243010")
        p2 = subprocess.Popen(["sleep", "301"], env=env)
        p3 = subprocess.Popen(["sleep", "302"])  # innocent
        reaper = os.path.join(bindir, "reaper")
        shutil.copy(shutil.which("sleep"), reaper)
        p4 = subprocess.Popen([reaper, "303"])  # a DAW called reaper: innocent
        time.sleep(0.3)
        try:
            found = {pid for pid, _ in watchdog.steam_pids(os.getuid())}
            self.assertIn(p1.pid, found)
            self.assertIn(p2.pid, found)
            self.assertNotIn(p3.pid, found)
            self.assertNotIn(p4.pid, found)
            watchdog.kill([(p1.pid, "steam"), (p2.pid, "game")], grace=2)
            p1.wait(5)
            p2.wait(5)
            self.assertIsNone(p3.poll())
        finally:
            for p in (p1, p2, p3, p4):
                if p.poll() is None:
                    p.kill()


if __name__ == "__main__":
    unittest.main()
