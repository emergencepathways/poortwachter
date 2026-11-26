"""Textbook study: the PDF lives in Poortwachter's private folder, the exercise
queue tracks what's next, and Claude grades submitted answers using your own
API key. Only the exercise's pages are sent to Claude, never the whole book."""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from . import config, db

API_URL = os.environ.get("POORTWACHTER_CLAUDE_URL", "https://api.anthropic.com/v1/messages")  # override: tests only


class TextbookError(Exception):
    pass


# ---------------------------------------------------------------- books


def books():
    return [dict(r) for r in db.connect().execute("SELECT * FROM textbooks ORDER BY id")]


def book(book_id):
    r = db.connect().execute("SELECT * FROM textbooks WHERE id = ?", (book_id,)).fetchone()
    return dict(r) if r else None


def page_count(path):
    try:
        out = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        return int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def add_book(name, tmp_path):
    cur = db.connect().execute("INSERT INTO textbooks (name, file, created_at) VALUES (?, '', ?)",
                               (name, time.time()))
    bid = cur.lastrowid
    dest = config.TEXTBOOK_DIR / f"{bid}.pdf"
    shutil.move(tmp_path, dest)
    os.chmod(dest, 0o600)
    db.connect().execute("UPDATE textbooks SET file = ?, pages = ? WHERE id = ?",
                         (str(dest), page_count(str(dest)), bid))
    if not db.get_setting("active_textbook"):
        db.set_setting("active_textbook", bid)
    return bid


def delete_book(book_id):
    b = book(book_id)
    if not b:
        return
    for f in (config.STATE_DIR / "cache").glob(f"tb{book_id}-p*.png"):
        f.unlink(missing_ok=True)
    try:
        os.remove(b["file"])
    except OSError:
        pass
    conn = db.connect()
    conn.execute("DELETE FROM textbook_sets WHERE textbook_id = ?", (book_id,))
    conn.execute("DELETE FROM textbooks WHERE id = ?", (book_id,))
    if db.get_setting("active_textbook") == book_id:
        db.set_setting("active_textbook", None)


def render_page(book_id, pdf_page, dpi=110):
    """One PDF page as a PNG (cached), so the study view works in any browser or app window, PDF viewer or not."""
    b = book(book_id)
    if not b:
        raise TextbookError("No such textbook.")
    pdf_page = int(pdf_page)
    if pdf_page < 1 or (b["pages"] and pdf_page > b["pages"]):
        raise TextbookError("That page isn't in the PDF.")
    cache = config.STATE_DIR / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"tb{book_id}-p{pdf_page}-{dpi}.png"
    if not out.exists():
        stem = str(out)[:-4]
        try:
            subprocess.run(["pdftoppm", "-f", str(pdf_page), "-l", str(pdf_page), "-r", str(dpi), "-png",
                            "-singlefile", b["file"], stem], check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise TextbookError(f"Couldn't draw that page (is poppler-utils installed?): {e}") from None
    return out


def set_offset(book_id, offset):
    db.connect().execute("UPDATE textbooks SET page_offset = ? WHERE id = ?", (int(offset), book_id))


# ---------------------------------------------------------------- set queue
#
# A "set" is one day's textbook work: one or more exercises (each with printed
# pages and estimated minutes), at most the daily textbook limit in total.
# Sets come from an imported sets file (see docs) or are typed in by hand.


def sets(book_id):
    return [db.row_to_dict(r, ("items",)) for r in db.connect().execute(
        "SELECT * FROM textbook_sets WHERE textbook_id = ? ORDER BY position, id", (book_id,))]


def get_set(set_id):
    return db.row_to_dict(db.connect().execute("SELECT * FROM textbook_sets WHERE id = ?", (set_id,)).fetchone(),
                          ("items",))


def next_set(book_id):
    r = db.connect().execute(
        "SELECT * FROM textbook_sets WHERE textbook_id = ? AND done = 0 ORDER BY position, id LIMIT 1",
        (book_id,)).fetchone()
    return db.row_to_dict(r, ("items",)) if r else None


def _clean_item(it):
    a = int(it["page_from"])
    b = int(it.get("page_to") or a)
    return {"label": str(it.get("label") or f"p. {a}")[:160], "page_from": min(a, b), "page_to": max(a, b),
            "minutes": max(1, int(it.get("minutes") or 10)), "review": bool(it.get("review")),
            "note": str(it.get("note") or "")[:400]}


def _insert_set(book_id, pos, title, items, est=None):
    items = [_clean_item(i) for i in items]
    if not items:
        raise TextbookError("A set needs at least one exercise.")
    est = int(est or sum(i["minutes"] for i in items))
    db.connect().execute("INSERT INTO textbook_sets (textbook_id, position, title, items, est_min) "
                         "VALUES (?,?,?,?,?)", (book_id, pos, str(title)[:200], json.dumps(items), est))


def _max_pos(book_id):
    return db.connect().execute("SELECT COALESCE(MAX(position), 0) FROM textbook_sets WHERE textbook_id = ?",
                                (book_id,)).fetchone()[0]


LINE = re.compile(r"^\s*(.+?)\s*[;|\t]\s*(\d+)\s*(?:[-–]\s*(\d+))?\s*(?:[;|\t]\s*(\d+)\s*(?:min)?)?\s*$")


def add_lines(book_id, text):
    """One set per line: "H5 Oefening 3; 84-85" or "H5 Oefening 3; 84-85; 20" (minutes)."""
    pos = _max_pos(book_id)
    added, bad = 0, []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = LINE.match(line)
        if not m:
            bad.append(line.strip())
            continue
        pos += 1
        minutes = int(m.group(4) or 20)
        _insert_set(book_id, pos, m.group(1), [{"label": m.group(1), "page_from": m.group(2),
                                                "page_to": m.group(3) or m.group(2), "minutes": minutes}])
        added += 1
    return added, bad


def import_sets(book_id, data, replace=False):
    """Import a sets file: {"sets": [{"title", "est_min"?, "items": [{label, page_from, page_to, minutes}]}]}."""
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError:
            raise TextbookError("That file isn't valid JSON.") from None
    raw = data.get("sets") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise TextbookError("No sets found in that file.")
    cap = (db.get_setting("category_caps") or {}).get("textbook")
    conn = db.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if replace:
            conn.execute("DELETE FROM textbook_sets WHERE textbook_id = ? AND done = 0", (book_id,))
        pos = _max_pos(book_id)
        over = 0
        for s in raw:
            pos += 1
            items = s.get("items") or []
            est = int(s.get("est_min") or sum(int(i.get("minutes") or 10) for i in items))
            if cap and est > cap:
                over += 1
            _insert_set(book_id, pos, s.get("title") or f"Set {pos}", items, est)
        conn.execute("COMMIT")
    except (KeyError, TypeError, ValueError) as e:
        conn.execute("ROLLBACK")
        raise TextbookError(f"A set in that file is incomplete ({e}).") from None
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(raw), over


def delete_set(set_id):
    db.connect().execute("DELETE FROM textbook_sets WHERE id = ?", (set_id,))


def reset_set(set_id):
    db.connect().execute("UPDATE textbook_sets SET done = 0 WHERE id = ?", (set_id,))


def clear_sets(book_id, only_open=True):
    q = "DELETE FROM textbook_sets WHERE textbook_id = ?" + (" AND done = 0" if only_open else "")
    db.connect().execute(q, (book_id,))


def mark_set_done(set_id):
    db.connect().execute("UPDATE textbook_sets SET done = 1 WHERE id = ?", (set_id,))


# ---------------------------------------------------------------- page extraction


def _ranges(pages):
    """[3, 4, 5, 9] -> "3-5,9" """
    out, start, prev = [], None, None
    for p in sorted(set(pages)):
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = p
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(out)


def extract_pages(path, pdf_pages):
    """A small PDF of just these PDF pages, in order (qpdf, or poppler as a fallback)."""
    pdf_pages = sorted(set(pdf_pages))
    tmp = tempfile.mkdtemp(prefix="pw-tb-")
    out = os.path.join(tmp, "pages.pdf")
    try:
        if shutil.which("qpdf"):
            subprocess.run(["qpdf", "--empty", "--pages", path, _ranges(pdf_pages), "--", out],
                           check=True, capture_output=True, timeout=60)
        elif shutil.which("pdfseparate") and shutil.which("pdfunite"):
            parts = []
            for p in pdf_pages:
                part = os.path.join(tmp, f"p-{p}.pdf")
                subprocess.run(["pdfseparate", "-f", str(p), "-l", str(p), path, part],
                               check=True, capture_output=True, timeout=60)
                parts.append(part)
            if len(parts) == 1:
                shutil.copy(parts[0], out)
            else:
                subprocess.run(["pdfunite", *parts, out], check=True, capture_output=True, timeout=60)
        else:
            raise TextbookError("Install qpdf or poppler-utils to send pages to Claude.")
        with open(out, "rb") as f:
            return f.read()
    except subprocess.CalledProcessError as e:
        raise TextbookError(f"Couldn't cut those pages out of the PDF: {e.stderr.decode(errors='replace')[:200]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- grading

MAX_PAGES = 12

GRADER = """You are a warm, precise Dutch teacher grading an A2-level learner's textbook work.

The attached PDF contains printed pages {pages} of the learner's textbook, in that order.
Today's set is "{title}" and contains these exercises:
{items}

The learner's answers are below, as they typed them, usually under a heading per exercise.
Find each exercise on the pages, work out what each question asks, and grade every answer.

For each answer give:
- whether it is correct (minor capitalisation/punctuation slips count as correct, but mention them)
- the corrected Dutch (the full corrected sentence or answer)
- the grammar rule behind the correction, explained in clear English with the Dutch term
- one extra example sentence using the same rule
- a short note on a common trap or related exception, when helpful

Treat open questions (free answers) generously: grade grammar, spelling and whether it answers
the question, and suggest a more natural phrasing. If an exercise has no answers, list it in
"missing".

Reply with JSON only, no other text, in exactly this shape:
{{
  "exercise_found": true,
  "items": [
    {{"exercise": "exercise label", "number": "1", "question": "short restatement",
      "answer": "what the learner wrote", "correct": true, "corrected": "...", "rule": "...",
      "example": "...", "note": "..."}}
  ],
  "missing": [],
  "score": {{"correct": 0, "total": 0}},
  "summary": "Two or three encouraging sentences in English on what went well and what to practise next.",
  "summary_nl": "One short encouraging sentence in simple Dutch."
}}
If you can't find an exercise on the pages, set "exercise_found" to false, explain in
"summary", and still grade the answers as well as you can.

The learner's answers:
---
{answers}
---"""


def grade(book_id, items, answers, title=""):
    """items: [{label, page_from, page_to}] with printed page numbers."""
    key = db.get_setting("claude_api_key")
    if not key:
        raise TextbookError("Add your Claude API key in Settings to have answers graded.")
    b = book(book_id)
    if not b:
        raise TextbookError("That textbook isn't in Poortwachter anymore.")
    if len(answers.strip()) < 3:
        raise TextbookError("Write your answers first.")
    off = b["page_offset"] or 0
    printed = sorted({p for it in items for p in range(int(it["page_from"]), int(it["page_to"]) + 1)})
    if len(printed) > MAX_PAGES:
        raise TextbookError(f"That's {len(printed)} pages; send at most {MAX_PAGES} at a time.")
    pdf_pages = [p + off for p in printed]
    if b["pages"] and (min(pdf_pages) < 1 or max(pdf_pages) > b["pages"]):
        raise TextbookError(f"Those pages fall outside the PDF (1–{b['pages']}). Check the page offset.")
    pdf = extract_pages(b["file"], pdf_pages)
    item_lines = "\n".join(
        f"- {it['label']} (page {it['page_from']}{'–' + str(it['page_to']) if it['page_to'] != it['page_from'] else ''})"
        + (" [review]" if it.get("review") else "") for it in items)
    body = {
        "model": db.get_setting("claude_model") or "claude-sonnet-5-5",
        "max_tokens": 12000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(pdf).decode()}},
                {"type": "text", "text": GRADER.format(
                    title=title or items[0]["label"], items=item_lines, answers=answers.strip(),
                    pages=_ranges(printed).replace("-", "–").replace(",", ", "))},
            ],
        }],
    }
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = detail[:300]
        raise TextbookError(f"Claude API error {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise TextbookError(f"Couldn't reach the Claude API: {e.reason}") from None
    text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
    return parse_result(text)


def parse_result(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise TextbookError("Claude's reply couldn't be read. Try again.")
    try:
        result = json.loads(text[start:end + 1])
    except ValueError:
        raise TextbookError("Claude's reply couldn't be read. Try again.") from None
    items = result.get("items") or []
    if not isinstance(items, list):
        items = []
    result["items"] = items
    score = result.get("score") or {}
    score.setdefault("total", len(items))
    score.setdefault("correct", sum(1 for i in items if i.get("correct")))
    result["score"] = score
    return result


def save_submission(task_id, book_id, set_id, label, page_from, page_to, answers, result):
    conn = db.connect()
    conn.execute("INSERT INTO submissions (task_id, textbook_id, exercise_id, label, page_from, page_to,"
                 " answers, result, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                 (task_id, book_id, set_id, label, page_from, page_to, answers,
                  json.dumps(result), time.time()))
    if set_id:
        mark_set_done(set_id)


def submissions(limit=50):
    return [db.row_to_dict(r) for r in db.connect().execute(
        "SELECT * FROM submissions ORDER BY ts DESC LIMIT ?", (limit,))]
