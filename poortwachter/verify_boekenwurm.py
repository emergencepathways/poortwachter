"""Boekenwurm: pair once, then ask for a daily summary of numbers.

Pairing (Boekenwurm's Connected apps, migrations 0013 + 0014; its
docs/CONNECTED_APPS.md): in Boekenwurm → Account → Settings → Connected apps →
"Pair Poortwachter", the reader agrees to what is shared and gets a code;
Poortwachter finds the database in <Boekenwurm address>/connect.json and
trades the code for its own token; the reader then allows the request in
Boekenwurm. The token opens only today's numbers (pages, active reading
minutes, cards reviewed, diary yes/no, also counted at or above the reader's
level). Unused for 90 days, the connection expires.

Boekenwurm keeps this feature for the owner's own use only and removes it
before a public release, so every Boekenwurm task here must keep failing
gracefully (with a clear message) when the connection is gone.

The older way, signing in with the email code, still works as a fallback
(below): it reads your own reading sessions and flashcards directly. Row-level security means this
can only ever see your own rows, and Poortwachter only reads.

Sign-in uses the same email Boekenwurm sends: type its 6-digit code into
Poortwachter (simplest), or open its link on this computer. The link only
comes back to Poortwachter if http://127.0.0.1:7623/bw/callback is in the
Supabase project's Redirect URLs (Authentication → URL Configuration); the
code works without that.
"""

import base64
import datetime as dt
import socket
import hashlib
import json
import math
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, db, roster

CALLBACK = f"http://{config.HOST}:{config.PORT}/bw/callback"
DEFAULT_SITE = "https://boekenwurm.pages.dev"


class BWError(Exception):
    pass


def _conf():
    url = (db.get_setting("bw_url") or "").rstrip("/")
    key = db.get_setting("bw_key") or ""
    if not url or not key:
        raise BWError("Boekenwurm isn't connected yet. Add its Supabase URL and key in Settings.")
    return url, key


def _request(method, url, key, body=None, token=None, timeout=20):
    headers = {"apikey": key, "Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise BWError(f"Boekenwurm answered {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise BWError(f"Couldn't reach Boekenwurm: {e.reason}") from None


# ---------------------------------------------------------------- sign-in


def start_login(email):
    url, key = _conf()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    db.set_setting("bw_email", email)
    db.set_setting("bw_session", {"pkce_verifier": verifier})
    try:
        _send_otp(url, key, email, challenge)
    except BWError as e:
        msg = str(e).lower()
        if "signup" in msg or "not allowed" in msg or "not found" in msg:
            raise BWError("No Boekenwurm account uses that email. Use the address you sign in to Boekenwurm with.") from None
        if "429" in msg or "rate" in msg:
            raise BWError("Supabase is limiting sign-in emails right now (its built-in mail allows only a few an "
                          "hour). Wait a bit, or use a code from an email you already have, if it's under an hour old.") from None
        raise


def _send_otp(url, key, email, challenge):
    _request("POST", f"{url}/auth/v1/otp?redirect_to={urllib.parse.quote(CALLBACK, safe='')}", key, {
        "email": email,
        "create_user": False,
        "code_challenge": challenge,
        "code_challenge_method": "s256",
    })


def finish_login_otp(code):
    """The 6-digit code from the sign-in email."""
    url, key = _conf()
    email = db.get_setting("bw_email")
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if not email:
        raise BWError("Send the sign-in email first.")
    if len(code) < 6:
        raise BWError("Type the 6-digit code from the email.")
    try:
        tok = _request("POST", f"{url}/auth/v1/verify", key, {"type": "email", "email": email, "token": code})
    except BWError as e:
        if " 403" in str(e) or "expired" in str(e).lower() or "invalid" in str(e).lower():
            raise BWError("That code didn't work: it may be mistyped, used already, or expired "
                          "(codes last about an hour). Send a new email and use the newest code.") from None
        raise
    _store(tok)


def finish_login_code(code):
    url, key = _conf()
    verifier = (db.get_setting("bw_session") or {}).get("pkce_verifier")
    if not verifier:
        raise BWError("Start the sign-in from Poortwachter's Settings first.")
    tok = _request("POST", f"{url}/auth/v1/token?grant_type=pkce", key,
                   {"auth_code": code, "code_verifier": verifier})
    _store(tok)


def finish_login_tokens(access_token, refresh_token, expires_in=3600):
    _store({"access_token": access_token, "refresh_token": refresh_token, "expires_in": expires_in})


def _store(tok):
    if not tok.get("access_token") or not tok.get("refresh_token"):
        raise BWError("Sign-in didn't return a session.")
    db.set_setting("bw_session", {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60,
        "user_email": (tok.get("user") or {}).get("email") or db.get_setting("bw_email"),
    })
    db.log("boekenwurm", "signed in")


def sign_out():
    db.set_setting("bw_session", {})


def status():
    s = db.get_setting("bw_session") or {}
    pair = db.get_setting("bw_pairing") or {}
    return {
        "configured": bool(db.get_setting("bw_url") and db.get_setting("bw_key")),
        "paired": bool(pair.get("token")),
        "approved": bool(pair.get("approved")),
        "paired_name": pair.get("name", ""),
        "paired_at": pair.get("at"),
        "site": db.get_setting("bw_site") or DEFAULT_SITE,
        "signed_in": bool(s.get("refresh_token")),
        "email": s.get("user_email") or db.get_setting("bw_email") or "",
        "callback": CALLBACK,
        "connected": bool(pair.get("token") or s.get("refresh_token")),
    }


# ---------------------------------------------------------------- pairing (preferred)


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise BWError(f"{url} answered {e.code}.") from None
    except urllib.error.URLError as e:
        raise BWError(f"Couldn't reach {url}: {e.reason}") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise BWError("That Boekenwurm doesn't support pairing yet (no connect.json). "
                      "It needs the Connected apps update.") from None


def discover(site=None):
    """Where this Boekenwurm's database is: <site>/connect.json."""
    site = (site or db.get_setting("bw_site") or DEFAULT_SITE).strip().rstrip("/")
    if not site.startswith("https://") and not site.startswith("http://127.0.0.1"):
        raise BWError("The Boekenwurm address should start with https://")
    info = _get_json(f"{site}/connect.json")
    if not isinstance(info, dict) or info.get("api") != 1 or not info.get("supabase_url") or not info.get("publishable_key"):
        raise BWError("That Boekenwurm's connect.json isn't one Poortwachter understands.")
    return site, info["supabase_url"].rstrip("/"), info["publishable_key"]


def pair(code, site=None, name=None):
    code = (code or "").strip()
    if len(code.replace("-", "").replace(" ", "")) < 8:
        raise BWError("Type the 8-character code Boekenwurm shows (like K7QM-3XPA).")
    site, url, key = discover(site)
    name = name or f"Poortwachter on {socket.gethostname()}"[:80]
    try:
        r = _request("POST", f"{url}/rest/v1/rpc/claim_pairing_code", key, {"p_code": code, "p_name": name})
    except BWError as e:
        if "isn't valid" in str(e) or "expired" in str(e):
            raise BWError("That code didn't work: it may be mistyped, used already or older than 10 minutes. "
                          "Make a new one in Boekenwurm.") from None
        if "claim_pairing_code" in str(e) or "404" in str(e):
            raise BWError("That Boekenwurm's database isn't ready for pairing yet (it needs migration 0013).") from None
        raise
    if isinstance(r, dict) and r.get("error"):
        if r["error"] == "too_many":
            raise BWError("Too many wrong codes: wait 10 minutes, then make a new code in Boekenwurm.")
        raise BWError("That code didn't work: it may be mistyped, used already or older than 10 minutes. "
                      "Make a new one in Boekenwurm.")
    if not isinstance(r, dict) or not r.get("token"):
        raise BWError("Boekenwurm didn't return a connection. Try a new code.")
    db.set_setting("bw_site", site)
    db.set_setting("bw_url", url)
    db.set_setting("bw_key", key)
    db.set_setting("bw_pairing", {"token": r["token"], "id": r.get("connection_id"), "name": name, "at": time.time(),
                                  "approved": r.get("status") != "pending"})
    db.log("boekenwurm", f"paired as {name}")
    return status()


def unpair():
    db.set_setting("bw_pairing", {})
    db.log("boekenwurm", "unpaired")
    return status()


def _pair_token():
    return (db.get_setting("bw_pairing") or {}).get("token")


def call_rpc(fn, body):
    url, key = _conf()
    return _request("POST", f"{url}/rest/v1/rpc/{fn}", key, body)


_cache = {}


def _zone_name():
    from . import ical
    tz = ical._local_tz()
    return getattr(tz, "key", None) or "Europe/Amsterdam"


def summary(day):
    """Today's numbers for a study day (02:00 → 02:00 in this computer's time zone), cached for a minute.
    Boekenwurm only answers for today, or yesterday while it's before 02:00."""
    token = _pair_token()
    if not token:
        raise BWError("Pair Poortwachter with Boekenwurm in Settings first.")
    hit = _cache.get(day)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    r = call_rpc("connection_summary", {"p_token": token, "p_day": day, "p_tz": _zone_name(),
                                        "p_start_hour": config.RESET_HOUR})
    if isinstance(r, dict) and r.get("error") == "pending":
        raise BWError("Waiting for you to allow Poortwachter in Boekenwurm (Account → Settings → Connected apps).")
    if isinstance(r, dict) and r.get("error"):
        unpair()
        raise BWError("Boekenwurm says Poortwachter is no longer connected (disconnected, or unused for 90 days). "
                      "Pair it again in Settings.")
    pair = db.get_setting("bw_pairing") or {}
    if pair and not pair.get("approved"):
        pair["approved"] = True
        db.set_setting("bw_pairing", pair)
    _cache[day] = (time.time(), r)
    return r


def _token():
    url, key = _conf()
    s = db.get_setting("bw_session") or {}
    if not s.get("refresh_token"):
        raise BWError("Sign in to Boekenwurm in Settings first.")
    if s.get("expires_at", 0) > time.time():
        return url, key, s["access_token"]
    tok = _request("POST", f"{url}/auth/v1/token?grant_type=refresh_token", key,
                   {"refresh_token": s["refresh_token"]})
    _store(tok)
    return url, key, tok["access_token"]


# ---------------------------------------------------------------- checks


def _iso(day):
    return roster.day_start(day).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def reading_today(day):
    url, key, token = _token()
    since = urllib.parse.quote(_iso(day))
    sessions = _request("GET", f"{url}/rest/v1/reading_sessions?select=id,started_at,ended_at,paragraphs,results"
                               f"&started_at=gte.{since}", key, token=token)
    paragraphs = sum(int(s.get("paragraphs") or 0) for s in sessions)
    minutes = 0.0
    para_ids = []
    for s in sessions:
        if s.get("ended_at") and s.get("started_at"):
            a = dt.datetime.fromisoformat(s["started_at"].replace("Z", "+00:00"))
            b = dt.datetime.fromisoformat(s["ended_at"].replace("Z", "+00:00"))
            minutes += min(max((b - a).total_seconds() / 60, 0), 180)
        for r in s.get("results") or []:
            if isinstance(r, dict) and r.get("paragraphId"):
                para_ids.append(r["paragraphId"])
    paragraphs = max(paragraphs, len(para_ids))

    # Pages: distinct printed pages for books that know their page numbers;
    # for EPUBs (no printed pages) every N paragraphs count as a page.
    pages_known = set()
    unpaged = 0
    ids = list(dict.fromkeys(para_ids))
    for i in range(0, len(ids), 80):
        chunk = ids[i:i + 80]
        rows = _request("GET", f"{url}/rest/v1/paragraphs?select=id,book_id,page_number"
                               f"&id=in.({','.join(chunk)})", key, token=token)
        for r in rows:
            if r.get("page_number"):
                pages_known.add((r["book_id"], r["page_number"]))
            else:
                unpaged += 1
    per_page = max(int(db.get_setting("bw_paragraphs_per_page") or 4), 1)
    pages = len(pages_known) + math.floor(unpaged / per_page)
    return {"sessions": len(sessions), "paragraphs": paragraphs, "pages": pages, "minutes": round(minutes)}


def cards_today(day):
    url, key, token = _token()
    since = urllib.parse.quote(_iso(day))
    rows = _request("GET", f"{url}/rest/v1/cards?select=lemma&last_review=gte.{since}", key, token=token)
    return len(rows)


def _reading_targets(params):
    """Older activities said {metric, target}; now {pages, minutes, at_level}."""
    if "metric" in params and "pages" not in params and "minutes" not in params:
        m, t = params.get("metric"), int(params.get("target") or 1)
        return {"pages": t if m != "minutes" else 0, "minutes": t if m == "minutes" else 0, "at_level": False}
    return {"pages": int(params.get("pages") or 0), "minutes": int(params.get("minutes") or 0),
            "at_level": params.get("at_level", True) is not False}


def check_reading(params, day):
    t = _reading_targets(params)
    if _pair_token():
        try:
            s = summary(day)
        except BWError as e:
            return {"ok": False, "message": str(e)}
        sfx = "_at_level" if t["at_level"] else ""
        pages = int(s.get("pages" + sfx) or 0)
        minutes = float(s.get("active_minutes" + sfx) or 0)
        ok = (t["pages"] and pages >= t["pages"]) or (t["minutes"] and minutes >= t["minutes"])
        parts = []
        if t["pages"]:
            parts.append(f"{pages}/{t['pages']} pages")
        if t["minutes"]:
            parts.append(f"{minutes:g}/{t['minutes']} active min")
        msg = " or ".join(parts) + (" at or above your level" if t["at_level"] else "") + " today"
        if t["at_level"] and int(s.get("pages") or 0) > pages:
            msg += f" ({int(s.get('pages') or 0) - pages} more pages below your level don't count)"
        return {"ok": bool(ok), "value": pages, "target": t["pages"], "message": msg, "summary": s}
    # Fallback: the older email sign-in (no active time or levels).
    try:
        stats = reading_today(day)
    except BWError as e:
        return {"ok": False, "message": str(e)}
    ok = (t["pages"] and stats["pages"] >= t["pages"]) or (t["minutes"] and stats["minutes"] >= t["minutes"])
    return {"ok": bool(ok), "value": stats["pages"], "target": t["pages"],
            "message": f"{stats['pages']} pages, {stats['minutes']} min, {stats['paragraphs']} paragraphs today "
                       "(pair with Boekenwurm for active time and levels)"}


def check_cards(params, day):
    target = int(params.get("cards", 1))
    try:
        n = int(summary(day).get("cards_reviewed") or 0) if _pair_token() else cards_today(day)
    except BWError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": n >= target, "value": n, "target": target, "message": f"{n} / {target} cards reviewed today"}


def check_diary(params, day):
    if not _pair_token():
        return {"ok": False, "message": "Pair Poortwachter with Boekenwurm in Settings to count its diary."}
    try:
        done = bool(summary(day).get("diary_done"))
    except BWError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": done, "message": "Diary written in Boekenwurm today" if done else
            "No Boekenwurm diary entry today yet. Write one there, or use Poortwachter's own diary task."}
