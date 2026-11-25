"""Read Anki Desktop's collection to count today's reviews.

Anki keeps the collection open (and locked) while it runs, so we copy the
database (plus its WAL file) to a private temp dir and read the copy. Nothing
in the Anki folder is ever written.
"""

import glob
import os
import pwd
import shutil
import sqlite3
import tempfile
import time

from . import config, db, roster


def _home():
    user = config.read_conf().get("user")
    if user:
        try:
            return pwd.getpwnam(user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def find_collections():
    home = _home()
    patterns = [
        os.path.join(home, ".local/share/Anki2/*/collection.anki2"),
        os.path.join(home, ".var/app/net.ankiweb.Anki/data/Anki2/*/collection.anki2"),
        os.path.join(home, "snap/anki-woodrow/common/*/collection.anki2"),
    ]
    found = []
    for p in patterns:
        found.extend(sorted(glob.glob(p)))
    return found


def collection_path():
    p = db.get_setting("anki_collection")
    if p and os.path.exists(p):
        return p
    found = find_collections()
    # Pick the most recently modified profile.
    found.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    return found[0] if found else None


class _Snapshot:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="pw-anki-")
        dst = os.path.join(self.dir, "c.anki2")
        shutil.copy2(self.path, dst)
        for suffix in ("-wal",):
            if os.path.exists(self.path + suffix):
                shutil.copy2(self.path + suffix, dst + suffix)
        self.conn = sqlite3.connect(dst)
        self.conn.row_factory = sqlite3.Row
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def _deck_ids(conn, deck_name):
    """Deck ids for a deck and its subdecks. Supports new (decks table) and
    old (JSON in col.decks) schemas."""
    if not deck_name:
        return None
    want = deck_name.strip().lower()
    names = {}
    try:
        for r in conn.execute("SELECT id, name FROM decks"):
            names[r["id"]] = r["name"].replace("\x1f", "::")
    except sqlite3.OperationalError:
        import json
        row = conn.execute("SELECT decks FROM col").fetchone()
        for did, d in json.loads(row["decks"]).items():
            names[int(did)] = d["name"]
    ids = [i for i, n in names.items() if n.lower() == want or n.lower().startswith(want + "::")]
    return ids


def list_decks():
    path = collection_path()
    if not path:
        return []
    with _Snapshot(path) as conn:
        try:
            return sorted(r["name"].replace("\x1f", "::") for r in conn.execute("SELECT name FROM decks"))
        except sqlite3.OperationalError:
            import json
            row = conn.execute("SELECT decks FROM col").fetchone()
            return sorted(d["name"] for d in json.loads(row["decks"]).values())


def check(params, day):
    path = collection_path()
    if not path:
        return {"ok": False, "message": "No Anki collection found. Set its path in Settings."}
    target = int(params.get("reviews", 0) or 0)
    since_ms = int(roster.day_start_ts(day) * 1000)
    with _Snapshot(path) as conn:
        dids = _deck_ids(conn, params.get("deck", ""))
        if dids is not None and not dids:
            return {"ok": False, "message": f"Deck “{params.get('deck')}” not found in Anki."}
        q = ("SELECT COUNT(*) FROM revlog r JOIN cards c ON c.id = r.cid "
             "WHERE r.id >= ? AND r.type IN (0, 1, 2, 3)")
        args = [since_ms]
        if dids:
            q += f" AND c.did IN ({','.join('?' * len(dids))})"
            args += dids
        reviews = conn.execute(q, args).fetchone()[0]

        due_left = None
        if params.get("clear_due"):
            crt = conn.execute("SELECT crt FROM col").fetchone()[0]
            today_n = int((time.time() - crt) // 86400)
            dq = ("SELECT COUNT(*) FROM cards WHERE "
                  "((queue IN (2, 3) AND due <= ?) OR (queue = 1 AND due <= ?))")
            dargs = [today_n, int(time.time())]
            if dids:
                dq += f" AND did IN ({','.join('?' * len(dids))})"
                dargs += dids
            due_left = conn.execute(dq, dargs).fetchone()[0]

    ok = reviews >= target and (due_left is None or due_left == 0)
    msg = f"{reviews} / {target} reviews today"
    if due_left is not None:
        msg += f" · {due_left} due cards left"
    return {"ok": ok, "message": msg, "value": reviews, "target": target, "due_left": due_left}
