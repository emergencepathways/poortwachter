"""Boekenwurm (Supabase) — sign in once, then read today's reading sessions
and flashcard reviews with your own account. Row-level security means this
can only ever see your own rows, and Poortwachter only reads.

Sign-in uses the same email Boekenwurm sends: type its 6-digit code into
Poortwachter (simplest), or open its link on this computer. The link only
comes back to Poortwachter if http://127.0.0.1:7623/bw/callback is in the
Supabase project's Redirect URLs (Authentication → URL Configuration); the
code works without that.
"""

import base64
import datetime as dt
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
    return {
        "configured": bool(db.get_setting("bw_url") and db.get_setting("bw_key")),
        "signed_in": bool(s.get("refresh_token")),
        "email": s.get("user_email") or db.get_setting("bw_email") or "",
        "callback": CALLBACK,
    }


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


def check_reading(params, day):
    try:
        stats = reading_today(day)
    except BWError as e:
        return {"ok": False, "message": str(e)}
    metric = params.get("metric", "pages")
    target = int(params.get("target", 1))
    value = stats.get(metric, 0)
    return {"ok": value >= target, "value": value, "target": target,
            "message": f"{value} / {target} {metric} today · {stats['sessions']} session(s), "
                       f"{stats['paragraphs']} paragraphs"}


def check_cards(params, day):
    try:
        n = cards_today(day)
    except BWError as e:
        return {"ok": False, "message": str(e)}
    target = int(params.get("cards", 1))
    return {"ok": n >= target, "value": n, "target": target, "message": f"{n} / {target} cards reviewed today"}
