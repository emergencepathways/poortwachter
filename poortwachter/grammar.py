"""Free grammar and spelling hints from LanguageTool (LanguageTool GmbH,
Germany, EU), the same service Boekenwurm's diary uses. Only when you press
"Check grammar", and only if it's turned on in Settings: the text you wrote is
sent to api.languagetool.org. No key or account needed (the free API allows
about 20 checks a minute and 20 kB of text)."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import db

API = os.environ.get("POORTWACHTER_LT_URL", "https://api.languagetool.org/v2/check")  # override: tests only
MAX_CHARS = 18000


class GrammarError(Exception):
    pass


def check(text):
    if not db.get_setting("languagetool"):
        raise GrammarError("Grammar hints are off (Settings → Writing).")
    text = (text or "").strip()
    if len(text) < 3:
        raise GrammarError("Write something first.")
    if len(text) > MAX_CHARS:
        raise GrammarError("That's too long to check in one go.")
    data = urllib.parse.urlencode({"text": text, "language": "nl", "enabledOnly": "false"}).encode()
    req = urllib.request.Request(API, data=data, method="POST",
                                 headers={"Accept": "application/json",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise GrammarError("LanguageTool is busy: wait a minute and try again.") from None
        raise GrammarError(f"LanguageTool answered {e.code}.") from None
    except (urllib.error.URLError, ValueError) as e:
        raise GrammarError(f"Couldn't reach LanguageTool: {getattr(e, 'reason', e)}") from None
    return simplify(text, body)


def simplify(text, body):
    out = []
    for m in body.get("matches", []):
        off, length = int(m.get("offset", 0)), int(m.get("length", 0))
        rule = m.get("rule") or {}
        out.append({
            "offset": off, "length": length, "text": text[off:off + length],
            "message": m.get("message") or m.get("shortMessage") or "",
            "replacements": [r.get("value") for r in (m.get("replacements") or [])[:4] if r.get("value")],
            "category": (rule.get("category") or {}).get("name", ""),
            "context": (m.get("sentence") or "")[:300],
        })
    return {"matches": out, "count": len(out)}
