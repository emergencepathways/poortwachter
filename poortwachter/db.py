"""SQLite storage. One file, root-owned, so the roster can't be edited by hand."""

import json
import sqlite3
import threading
import time

from . import config

_local = threading.local()
_init_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    est_min     INTEGER NOT NULL DEFAULT 15,
    weight      REAL NOT NULL DEFAULT 1.0,
    in_pool     INTEGER NOT NULL DEFAULT 1,
    enabled     INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rosters (
    day        TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    frozen     INTEGER NOT NULL DEFAULT 0,
    edited     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY,
    day          TEXT NOT NULL,
    activity_id  INTEGER,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    params       TEXT NOT NULL DEFAULT '{}',
    est_min      INTEGER NOT NULL,
    source       TEXT NOT NULL,           -- core | random | carry | manual | bonus
    mystery      INTEGER NOT NULL DEFAULT 0,
    revealed     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'todo',   -- todo | done
    counts       INTEGER NOT NULL DEFAULT 1,     -- 0 = bonus, doesn't gate Steam
    progress     TEXT NOT NULL DEFAULT '{}',
    evidence     TEXT NOT NULL DEFAULT '{}',
    carried_from INTEGER,
    position     INTEGER NOT NULL DEFAULT 0,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_day ON tasks (day);
CREATE TABLE IF NOT EXISTS unlocks (
    day    TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    ts     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,
    type   TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS textbooks (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    file  TEXT NOT NULL,
    pages INTEGER,
    page_offset INTEGER NOT NULL DEFAULT 0,   -- PDF page = printed page + offset
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS exercises (
    id          INTEGER PRIMARY KEY,
    textbook_id INTEGER NOT NULL,
    label       TEXT NOT NULL,
    page_from   INTEGER NOT NULL,
    page_to     INTEGER NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER,
    textbook_id INTEGER,
    exercise_id INTEGER,
    label       TEXT NOT NULL DEFAULT '',
    page_from   INTEGER,
    page_to     INTEGER,
    answers     TEXT NOT NULL,
    result      TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS textbook_sets (
    id          INTEGER PRIMARY KEY,
    textbook_id INTEGER NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    title       TEXT NOT NULL,
    items       TEXT NOT NULL DEFAULT '[]',    -- [{label, page_from, page_to, minutes, review}]
    est_min     INTEGER NOT NULL DEFAULT 25,
    done        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'other',  -- italki | library | other
    category    TEXT NOT NULL DEFAULT 'speaking',
    minutes     INTEGER NOT NULL DEFAULT 60,
    weekday     INTEGER,                         -- weekly pattern: 0 = Monday
    date        TEXT,                            -- one-off date
    time        TEXT NOT NULL DEFAULT '',
    start_date  TEXT,
    end_date    TEXT,
    note_words  INTEGER NOT NULL DEFAULT 10,
    link        INTEGER NOT NULL DEFAULT 0,
    place       TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_skips (
    event_id INTEGER NOT NULL,
    day      TEXT NOT NULL,
    PRIMARY KEY (event_id, day)
);
CREATE TABLE IF NOT EXISTS free_days (
    day    TEXT PRIMARY KEY,
    source TEXT NOT NULL,        -- install | planned | emergency
    ts     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pto_ledger (
    id     INTEGER PRIMARY KEY,
    day    TEXT NOT NULL,
    hours  INTEGER NOT NULL,     -- +1 per completed day, -24 per free day
    reason TEXT NOT NULL,
    ts     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_units (       -- the private exercise bank, built from the owner's textbook
    textbook_id INTEGER NOT NULL,
    id          TEXT NOT NULL,
    position    INTEGER NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (textbook_id, id)
);
CREATE TABLE IF NOT EXISTS unit_state (        -- where each exercise stands: done | retry | italki | unsupported
    textbook_id INTEGER NOT NULL,
    unit_id     TEXT NOT NULL,
    status      TEXT NOT NULL,
    correct     INTEGER,
    total       INTEGER,
    seconds     REAL NOT NULL DEFAULT 0,
    ts          REAL NOT NULL,
    PRIMARY KEY (textbook_id, unit_id)
);
CREATE TABLE IF NOT EXISTS unit_attempts (
    id          INTEGER PRIMARY KEY,
    textbook_id INTEGER NOT NULL,
    unit_id     TEXT NOT NULL,
    task_id     INTEGER,
    day         TEXT NOT NULL,
    correct     INTEGER NOT NULL,
    total       INTEGER NOT NULL,
    seconds     REAL NOT NULL,
    results     TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings (
    id      INTEGER PRIMARY KEY,
    task_id INTEGER,
    file    TEXT NOT NULL,
    seconds REAL NOT NULL,
    prompt  TEXT NOT NULL DEFAULT '',
    ts      REAL NOT NULL
);
"""

SPEAKING_PROMPTS = [
    "Vertel over je ochtendroutine. Wat doe je eerst, en wat daarna?",
    "Wat heb je gisteren gedaan? Gebruik de voltooide tijd.",
    "Beschrijf je favoriete plek in de stad. Waarom vind je het daar fijn?",
    "Wat ga je dit weekend doen?",
    "Vertel over je familie of over een goede vriend.",
    "Beschrijf je huis. Welke kamer vind je het mooist?",
    "Wat eet je graag? Wat kook je vaak?",
    "Hoe ga je naar je werk of naar de winkel? Hoe lang duurt dat?",
    "Wat is anders in Nederland dan in het land waar je vandaan komt?",
    "Vertel over een boek, serie of film die je nu leest of kijkt.",
    "Wat doe je als het de hele dag regent?",
    "Beschrijf het weer van vandaag en van gisteren.",
    "Vertel over je laatste vakantie. Waar was je en wat heb je gedaan?",
    "Welke hobby's heb je? Hoe ben je daarmee begonnen?",
    "Vertel over een dag waarop alles misging.",
    "Wat zou je doen als je een miljoen euro had?",
    "Hoe ziet jouw ideale zondag eruit?",
    "Leg uit hoe je je favoriete gerecht maakt, stap voor stap.",
    "Wat vind je moeilijk aan Nederlands leren? En wat gaat goed?",
    "Beschrijf iemand die je bewondert. Waarom?",
    "Je belt de huisarts voor een afspraak. Speel het gesprek na.",
    "Je bent in een winkel en iets is kapot. Wat zeg je?",
]

DEFAULT_ACTIVITIES = [
    # name, category, kind, params, est_min, weight, in_pool, description
    ("Anki — daily reviews", "flashcards", "anki",
     {"decks": [], "reviews": 50, "clear_due": False}, 15, 1.0, 1,
     "Do your Anki reviews. Counts once you've logged the target number of reviews today."),
    ("Boekenwurm — read 5 pages or 20 minutes", "reading", "bw_reading",
     {"pages": 5, "minutes": 20, "at_level": True}, 20, 1.2, 1,
     "Read in Boekenwurm at or above your level: 5 pages or 20 minutes of active reading, whichever comes first. "
     "Finish or leave the session so it syncs."),
    ("Boekenwurm — diary entry", "writing", "bw_diary",
     {}, 15, 0.8, 1,
     "Write a diary entry in Boekenwurm (fine on your phone). Only the fact that you wrote is shared, never the text."),
    ("Boekenwurm — flashcards (a set of 30)", "flashcards", "bw_cards",
     {"cards": 30}, 12, 0.6, 0,
     "Review a full set of 30 Boekenwurm flashcards. Required 1–2 days a week; Anki is the daily review otherwise."),
    ("Dutch novel — 20 minutes + summary", "reading", "timer",
     {"minutes": 20, "note_words": 30,
      "note_prompt": "Vat samen wat je hebt gelezen (minimaal 30 woorden)."}, 25, 1.0, 1,
     "Read a Dutch novel. The timer only runs while this page is open."),
    ("Dutch news — 15 minutes + summary", "reading", "timer",
     {"minutes": 15, "note_words": 20,
      "note_prompt": "Welk nieuwsbericht vond je interessant? Waarom? (minimaal 20 woorden)"}, 15, 0.8, 1,
     "Read NOS, NU.nl or the Jeugdjournaal in Dutch."),
    ("Textbook — oefenen", "textbook", "textbook",
     {"mode": "queue"}, 20, 1.2, 0,
     "Work through your textbook in order, one question at a time, for at least 20 minutes. Answers are checked "
     "against the book's answer key."),
    ("Speaking — answer a prompt", "speaking", "recording",
     {"min_seconds": 120, "prompts": "speaking"}, 10, 1.0, 1,
     "Record yourself answering a random Dutch prompt for at least 2 minutes."),
    ("Speaking — read aloud", "speaking", "recording",
     {"min_seconds": 120, "prompt": "Lees een stuk uit je boek hardop voor."}, 8, 0.7, 1,
     "Read a passage of your book aloud, recorded."),
    ("Dagboek — diary entry", "writing", "note",
     {"min_words": 60, "prompt_set": "diary"}, 15, 0.9, 1,
     "Write a diary entry in Dutch to one of the 20 prompts, with free grammar hints."),
    ("Grammar sentences", "writing", "note",
     {"min_words": 40, "prompt": "Kies een grammaticaregel en schrijf vijf eigen zinnen ermee."}, 15, 0.7, 1,
     "Write five sentences practising one grammar rule."),
    ("Dutch podcast — 15 minutes", "listening", "timer",
     {"minutes": 15, "note_words": 20,
      "note_prompt": "Waar ging het over? (minimaal 20 woorden)"}, 20, 0.8, 1,
     "Listen to a Dutch podcast (e.g. Zeg het in het Nederlands, NOS Journaal podcast)."),
    ("Dutch TV / YouTube — 20 minutes", "listening", "timer",
     {"minutes": 20, "note_words": 20,
      "note_prompt": "Wat heb je gekeken? Noem drie nieuwe woorden."}, 22, 0.6, 1,
     "Watch something Dutch with Dutch subtitles."),
]


def _default_schedule():
    """Before activities exist: Anki every day. init() replaces it with recommended_plan()."""
    schedule = {}
    for wd in range(7):  # 0 = Monday
        weekend = wd >= 5
        schedule[str(wd)] = {
            "budget": 45 if weekend else 90,
            "mystery": 0.0,
            "core": [1],  # Anki reviews every day
        }
    return schedule


def recommended_plan(conn=None):
    """The owner's plan (v4): every weekday the textbook (20+ min) and Boekenwurm reading; the daily review is Anki,
    except Boekenwurm flashcards (a set of 30) on Wednesday and Saturday; writing (the Boekenwurm diary) every other
    day; weekends 45 minutes. Returns (schedule, every-other-day ids)."""
    conn = conn or connect()

    def first(kind):
        r = conn.execute("SELECT id FROM activities WHERE kind = ? AND enabled = 1 ORDER BY id LIMIT 1",
                         (kind,)).fetchone()
        return r["id"] if r else None
    anki, cards, book, reading, diary = (first(k) for k in ("anki", "bw_cards", "textbook", "bw_reading", "bw_diary"))
    schedule = {}
    for wd in range(7):
        review = cards if wd in (2, 5) and cards else anki
        core = [review] + ([book] if wd < 5 else []) + [reading]
        schedule[str(wd)] = {"budget": 45 if wd >= 5 else 90, "mystery": 0.0, "core": [i for i in core if i]}
    return schedule, [diary] if diary else []


DEFAULT_SETTINGS = {
    "schedule": _default_schedule(),
    "claude_api_key": "",
    "claude_model": "claude-sonnet-5-5",
    "anki_collection": "",            # auto-detected when empty
    "bw_url": "",
    "bw_key": "",
    "bw_email": "",
    "bw_session": {},                 # tokens, never sent to the browser
    "bw_paragraphs_per_page": 4,
    "speaking_prompts": SPEAKING_PROMPTS,
    "min_fill_minutes": 8,
    "active_textbook": None,
    # Most minutes a day of required work per category (generator + planner).
    "category_caps": {"textbook": 45, "reading": 45},
    # On a day where a planned session fills the budget, this activity is still required.
    "event_minimum_activity": 1,
    "plan_days_ahead": 7,
    "installed_day": None,           # set on first start: that day is free
    "bw_site": "https://boekenwurm.pages.dev",
    "bw_pairing": {},                # Boekenwurm connection token (never sent to the browser)
    "languagetool": True,            # grammar hints on writing tasks (the text goes to LanguageTool)
    "ical_feeds": [],                # [{"url", "name"}]: calendars whose matching events become sessions
    "ical_rules": [{"keyword": "italki", "kind": "italki"}, {"keyword": "taalgroep", "kind": "library"},
                   {"keyword": "taalcafé", "kind": "other"}, {"keyword": "bibliotheek", "kind": "library"}],
    "pto_processed_through": None,
    # Textbook (oefenen): minutes per weekday session (never under 20), hardcore = only 70%+ counts.
    "textbook_minutes": 20,
    "textbook_hardcore": False,
    "tb_focus": {},                  # {"textbook_id", "unit_id"}: work from this exercise on
    "tb_resume": {},                 # after italki work: ask where to continue
    # Raise weekday study time: +5 min per completed weekday after 7 in a row, up to 120 (weekends stay).
    "ramp": {"enabled": False, "step": 5, "after": 7, "max": 120},
    "ramp_state": {"streak": 0, "bonus": 0, "through": None},
    "writing_every_other_day": [],   # activity ids required on alternate days
    "whisper": True,                 # check speaking exercises with the local Whisper (if installed)
}


def connect():
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(config.DB_PATH), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init():
    with _init_lock:
        conn = connect()
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                         (key, json.dumps(value)))
        if conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0:
            for (name, cat, kind, params, est, weight, pool, desc) in DEFAULT_ACTIVITIES:
                conn.execute(
                    "INSERT INTO activities (name, category, kind, params, est_min, weight, in_pool,"
                    " description, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (name, cat, kind, json.dumps(params), est, weight, pool, desc, time.time()))
        _migrate(conn)
        _migrate_plan(conn)
        if get_setting("installed_day") is None:
            _mark_install_day(conn)
        for d in (config.TEXTBOOK_DIR, config.RECORDING_DIR):
            d.mkdir(parents=True, exist_ok=True)
        try:
            for p in (config.STATE_DIR, config.TEXTBOOK_DIR, config.RECORDING_DIR):
                p.chmod(0o700)
            config.DB_PATH.chmod(0o600)
        except OSError:
            pass


def _migrate_plan(conn):
    """v4 defaults: a new install, or one still on the old default plan (Anki only, 90/45), gets the recommended
    plan. Plans the owner changed are left alone (Settings has a button to switch)."""
    if get_setting("plan_version"):
        return
    sched = get_setting("schedule") or {}
    anki = conn.execute("SELECT id FROM activities WHERE kind = 'anki' ORDER BY id LIMIT 1").fetchone()
    untouched = anki and all(
        (sched.get(str(wd)) or {}).get("core") == [anki["id"]]
        and (sched.get(str(wd)) or {}).get("budget") == (45 if wd >= 5 else 90) for wd in range(7))
    for r in conn.execute("SELECT id, params, est_min FROM activities WHERE kind = 'bw_cards'").fetchall():
        p = json.loads(r["params"])
        if int(p.get("cards") or 0) < 30:
            p["cards"] = 30
            conn.execute("UPDATE activities SET params = ?, est_min = MAX(est_min, 12), name = "
                         "'Boekenwurm — flashcards (a set of 30)' WHERE id = ?", (json.dumps(p), r["id"]))
    conn.execute("UPDATE activities SET name = 'Textbook — oefenen', est_min = 20 "
                 "WHERE kind = 'textbook' AND name = 'Textbook — next exercise'")
    if untouched:
        conn.execute("UPDATE activities SET in_pool = 0 WHERE kind IN ('textbook', 'bw_cards')")
        apply_recommended_plan(conn)
    else:
        set_setting("plan_version", 3)


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn):
    """Bring a database from an earlier version up to date."""
    if "event_id" not in _columns(conn, "tasks"):
        conn.execute("ALTER TABLE tasks ADD COLUMN event_id INTEGER")
    if "ical_key" not in _columns(conn, "sessions"):
        conn.execute("ALTER TABLE sessions ADD COLUMN ical_key TEXT")
    if "time" not in _columns(conn, "tasks"):
        conn.execute("ALTER TABLE tasks ADD COLUMN time TEXT NOT NULL DEFAULT ''")
    # v1 kept single exercises; v2 groups them into sets.
    for ex in conn.execute("SELECT * FROM exercises").fetchall():
        conn.execute(
            "INSERT INTO textbook_sets (textbook_id, position, title, items, est_min, done) VALUES (?,?,?,?,?,?)",
            (ex["textbook_id"], ex["position"], ex["label"],
             json.dumps([{"label": ex["label"], "page_from": ex["page_from"], "page_to": ex["page_to"],
                          "minutes": 25}]), 25, ex["done"]))
    conn.execute("DELETE FROM exercises")
    # v3: Boekenwurm reading counts pages OR active minutes at your level; a Boekenwurm diary task;
    # Poortwachter's own diary uses the 20 prompts.
    for r in conn.execute("SELECT id, params FROM activities WHERE kind = 'bw_reading'").fetchall():
        p = json.loads(r["params"])
        if "metric" in p:
            t = int(p.get("target") or 5)
            new = {"pages": t if p["metric"] == "pages" else 0, "minutes": t if p["metric"] == "minutes" else 20,
                   "at_level": True}
            conn.execute("UPDATE activities SET params = ? WHERE id = ?", (json.dumps(new), r["id"]))
    conn.execute("UPDATE activities SET name = 'Boekenwurm — read 5 pages or 20 minutes' "
                 "WHERE name = 'Boekenwurm — read 5 pages' AND kind = 'bw_reading'")
    if conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] and \
            not conn.execute("SELECT 1 FROM activities WHERE kind = 'bw_diary'").fetchone():
        conn.execute(
            "INSERT INTO activities (name, category, kind, params, est_min, weight, in_pool, description, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("Boekenwurm — diary entry", "writing", "bw_diary", "{}", 15, 0.8, 1,
             "Write a diary entry in Boekenwurm (fine on your phone). Only the fact that you wrote is shared, "
             "never the text.", time.time()))
    for r in conn.execute("SELECT id, params FROM activities WHERE name = 'Dagboek — diary entry' AND kind = 'note'"):
        p = json.loads(r["params"])
        if p.get("prompt") == "Schrijf over je dag (minimaal 60 woorden)." and "prompt_set" not in p:
            p.pop("prompt")
            p["prompt_set"] = "diary"
            conn.execute("UPDATE activities SET params = ? WHERE id = ?", (json.dumps(p), r["id"]))


def apply_recommended_plan(conn=None):
    conn = conn or connect()
    schedule, alternate = recommended_plan(conn)
    set_setting("schedule", schedule)
    set_setting("writing_every_other_day", alternate)
    set_setting("plan_version", 4)


def _mark_install_day(conn):
    """The day Poortwachter is installed is free; blocking starts at the next 02:00."""
    import datetime as dt
    day = (dt.datetime.now() - dt.timedelta(hours=config.RESET_HOUR)).date().isoformat()
    set_setting("installed_day", day)
    set_setting("pto_processed_through", day)
    conn.execute("INSERT OR IGNORE INTO free_days (day, source, ts) VALUES (?, 'install', ?)", (day, time.time()))
    log("install", f"installed on {day}: today is free, the lock starts at 02:00")


def get_setting(key):
    row = connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    return json.loads(row["value"])


def set_setting(key, value):
    connect().execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)))


def log(event_type, detail=""):
    connect().execute("INSERT INTO events (ts, type, detail) VALUES (?, ?, ?)",
                      (time.time(), event_type, detail))


def row_to_dict(row, json_fields=("params", "progress", "evidence", "result")):
    if row is None:
        return None
    d = dict(row)
    for f in json_fields:
        if f in d and isinstance(d[f], str):
            try:
                d[f] = json.loads(d[f])
            except ValueError:
                pass
    return d
