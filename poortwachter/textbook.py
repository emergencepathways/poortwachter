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
    try:
        os.remove(b["file"])
    except OSError:
        pass
    conn = db.connect()
    conn.execute("DELETE FROM exercises WHERE textbook_id = ?", (book_id,))
    conn.execute("DELETE FROM textbooks WHERE id = ?", (book_id,))
    if db.get_setting("active_textbook") == book_id:
        db.set_setting("active_textbook", None)


def set_offset(book_id, offset):
    db.connect().execute("UPDATE textbooks SET page_offset = ? WHERE id = ?", (int(offset), book_id))


# ---------------------------------------------------------------- exercise queue


def exercises(book_id):
    return [dict(r) for r in db.connect().execute(
        "SELECT * FROM exercises WHERE textbook_id = ? ORDER BY done, position, id", (book_id,))]


LINE = re.compile(r"^\s*(.+?)\s*[;|,\t]\s*(\d+)\s*(?:[-–]\s*(\d+))?\s*$")


def add_exercises(book_id, text):
    """One exercise per line: "H5 Oefening 3; 84-85" or "H5 Oefening 3 | 84"."""
    conn = db.connect()
    pos = conn.execute("SELECT COALESCE(MAX(position), 0) FROM exercises WHERE textbook_id = ?",
                       (book_id,)).fetchone()[0]
    added, bad = 0, []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = LINE.match(line)
        if not m:
            bad.append(line.strip())
            continue
        a = int(m.group(2))
        b = int(m.group(3) or a)
        pos += 1
        conn.execute("INSERT INTO exercises (textbook_id, label, page_from, page_to, position) "
                     "VALUES (?,?,?,?,?)", (book_id, m.group(1), min(a, b), max(a, b), pos))
        added += 1
    return added, bad


def delete_exercise(ex_id):
    db.connect().execute("DELETE FROM exercises WHERE id = ?", (ex_id,))


def reset_exercise(ex_id):
    db.connect().execute("UPDATE exercises SET done = 0 WHERE id = ?", (ex_id,))


def next_exercise(book_id):
    r = db.connect().execute(
        "SELECT * FROM exercises WHERE textbook_id = ? AND done = 0 ORDER BY position, id LIMIT 1",
        (book_id,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- page extraction


def extract_pages(path, first_pdf_page, last_pdf_page):
    """A small PDF of just the exercise pages (pdfseparate/pdfunite or qpdf)."""
    tmp = tempfile.mkdtemp(prefix="pw-tb-")
    out = os.path.join(tmp, "pages.pdf")
    try:
        if shutil.which("qpdf"):
            subprocess.run(["qpdf", "--empty", "--pages", path, f"{first_pdf_page}-{last_pdf_page}", "--", out],
                           check=True, capture_output=True, timeout=60)
        elif shutil.which("pdfseparate") and shutil.which("pdfunite"):
            pattern = os.path.join(tmp, "p-%d.pdf")
            subprocess.run(["pdfseparate", "-f", str(first_pdf_page), "-l", str(last_pdf_page), path, pattern],
                           check=True, capture_output=True, timeout=60)
            parts = [pattern % i for i in range(first_pdf_page, last_pdf_page + 1)]
            if len(parts) == 1:
                shutil.copy(parts[0], out)
            else:
                subprocess.run(["pdfunite", *parts, out], check=True, capture_output=True, timeout=60)
        else:
            raise TextbookError("Install poppler-utils or qpdf to send pages to Claude.")
        with open(out, "rb") as f:
            return f.read()
    except subprocess.CalledProcessError as e:
        raise TextbookError(f"Couldn't cut those pages out of the PDF: {e.stderr.decode(errors='replace')[:200]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- grading

GRADER = """You are a warm, precise Dutch teacher grading an A2-level learner's textbook exercise.

The attached PDF pages contain the exercise "{label}" (printed page {pages}). The learner's
answers are below, as they typed them. Find the exercise on the pages, work out what each
question asks, and grade every answer.

For each answer give:
- whether it is correct (minor capitalisation/punctuation slips count as correct, but mention them)
- the corrected Dutch (the full corrected sentence or answer)
- the grammar rule behind the correction, explained in clear English with the Dutch term
- one extra example sentence using the same rule
- a short note on a common trap or related exception, when helpful

Treat open questions (free answers) generously: grade grammar, spelling and whether it answers
the question, and suggest a more natural phrasing.

Reply with JSON only, no other text, in exactly this shape:
{{
  "exercise_found": true,
  "items": [
    {{"number": "1", "question": "short restatement", "answer": "what the learner wrote",
      "correct": true, "corrected": "...", "rule": "...", "example": "...", "note": "..."}}
  ],
  "score": {{"correct": 0, "total": 0}},
  "summary": "Two or three encouraging sentences in English on what went well and what to practise next.",
  "summary_nl": "One short encouraging sentence in simple Dutch."
}}
If you can't find the exercise on the pages, set "exercise_found" to false, explain in
"summary", and still grade the answers as well as you can.

The learner's answers:
---
{answers}
---"""


def grade(book_id, page_from, page_to, label, answers):
    key = db.get_setting("claude_api_key")
    if not key:
        raise TextbookError("Add your Claude API key in Settings to have answers graded.")
    b = book(book_id)
    if not b:
        raise TextbookError("That textbook isn't in Poortwachter anymore.")
    if len(answers.strip()) < 3:
        raise TextbookError("Write your answers first.")
    off = b["page_offset"] or 0
    first, last = page_from + off, page_to + off
    if b["pages"] and (first < 1 or last > b["pages"]):
        raise TextbookError(f"Pages {first}–{last} are outside the PDF (1–{b['pages']}). Check the page offset.")
    if last - first > 5:
        raise TextbookError("Send at most 6 pages at a time.")
    pdf = extract_pages(b["file"], first, last)
    body = {
        "model": db.get_setting("claude_model") or "claude-sonnet-5-5",
        "max_tokens": 6000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(pdf).decode()}},
                {"type": "text", "text": GRADER.format(
                    label=label or "the exercise", answers=answers.strip(),
                    pages=f"{page_from}" if page_from == page_to else f"{page_from}–{page_to}")},
            ],
        }],
    }
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
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


def save_submission(task_id, book_id, exercise_id, label, page_from, page_to, answers, result):
    conn = db.connect()
    conn.execute("INSERT INTO submissions (task_id, textbook_id, exercise_id, label, page_from, page_to,"
                 " answers, result, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                 (task_id, book_id, exercise_id, label, page_from, page_to, answers,
                  json.dumps(result), time.time()))
    if exercise_id:
        conn.execute("UPDATE exercises SET done = 1 WHERE id = ?", (exercise_id,))


def submissions(limit=50):
    return [db.row_to_dict(r) for r in db.connect().execute(
        "SELECT * FROM submissions ORDER BY ts DESC LIMIT ?", (limit,))]
