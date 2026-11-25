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
     {"deck": "", "reviews": 50, "clear_due": False}, 15, 1.0, 1,
     "Do your Anki reviews. Counts once you've logged the target number of reviews today."),
    ("Boekenwurm — read 5 pages", "reading", "bw_reading",
     {"metric": "pages", "target": 5}, 20, 1.2, 1,
     "Read in Boekenwurm. Finish or leave the session so it syncs, then press Check."),
    ("Boekenwurm — flashcards", "flashcards", "bw_cards",
     {"cards": 20}, 10, 1.0, 1,
     "Review your Boekenwurm study words."),
    ("Dutch novel — 20 minutes + summary", "reading", "timer",
     {"minutes": 20, "note_words": 30,
      "note_prompt": "Vat samen wat je hebt gelezen (minimaal 30 woorden)."}, 25, 1.0, 1,
     "Read a Dutch novel. The timer only runs while this page is open."),
    ("Dutch news — 15 minutes + summary", "reading", "timer",
     {"minutes": 15, "note_words": 20,
      "note_prompt": "Welk nieuwsbericht vond je interessant? Waarom? (minimaal 20 woorden)"}, 15, 0.8, 1,
     "Read NOS, NU.nl or the Jeugdjournaal in Dutch."),
    ("Textbook — next exercise", "textbook", "textbook",
     {"mode": "queue"}, 25, 1.2, 1,
     "Answer the next exercise from your textbook. Claude grades it."),
    ("Speaking — answer a prompt", "speaking", "recording",
     {"min_seconds": 120, "prompts": "speaking"}, 10, 1.0, 1,
     "Record yourself answering a random Dutch prompt for at least 2 minutes."),
    ("Speaking — read aloud", "speaking", "recording",
     {"min_seconds": 120, "prompt": "Lees een stuk uit je boek hardop voor."}, 8, 0.7, 1,
     "Read a passage of your book aloud, recorded."),
    ("Dagboek — diary entry", "writing", "note",
     {"min_words": 60, "prompt": "Schrijf over je dag (minimaal 60 woorden)."}, 15, 0.9, 1,
     "Write a short diary entry in Dutch."),
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
    schedule = {}
    for wd in range(7):  # 0 = Monday
        weekend = wd >= 5
        schedule[str(wd)] = {
            "budget": 45 if weekend else 90,
            "mystery": 0.0,
            "core": [1],  # Anki reviews every day
        }
    return schedule


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
    "pto_processed_through": None,
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


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn):
    """Bring a database from an earlier version up to date."""
    if "event_id" not in _columns(conn, "tasks"):
        conn.execute("ALTER TABLE tasks ADD COLUMN event_id INTEGER")
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
