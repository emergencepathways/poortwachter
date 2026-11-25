"""Paths and fixed constants.

Everything lives under one state directory that only root can write
(/var/lib/poortwachter in a normal install). Set POORTWACHTER_HOME to run a
throwaway copy for development or tests.
"""

import os
from pathlib import Path

STATE_DIR = Path(os.environ.get("POORTWACHTER_HOME", "/var/lib/poortwachter"))
DB_PATH = STATE_DIR / "poortwachter.db"
TEXTBOOK_DIR = STATE_DIR / "textbooks"
RECORDING_DIR = STATE_DIR / "recordings"
CONF_PATH = Path(os.environ.get("POORTWACHTER_CONF", "/etc/poortwachter.conf"))

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

HOST = "127.0.0.1"
PORT = int(os.environ.get("POORTWACHTER_PORT", "7623"))

# The study day starts at this hour (local time). 2 → everything done before
# 02:00 still counts for the previous day.
RESET_HOUR = 2

# Unfinished tasks carried into the next day, at most.
CARRYOVER_CAP = 2

CATEGORIES = [
    ("flashcards", "Flashcards"),
    ("reading", "Reading"),
    ("speaking", "Speaking"),
    ("textbook", "Textbook"),
    ("writing", "Writing"),
    ("listening", "Listening"),
    ("other", "Other"),
]

# How a task proves it is done.
KINDS = {
    "anki": "Anki reviews (checked in your Anki collection)",
    "bw_reading": "Boekenwurm reading (checked in your account)",
    "bw_cards": "Boekenwurm flashcards (checked in your account)",
    "timer": "Timer (runs only while the app is open)",
    "note": "Write in Dutch (word minimum, Dutch-language check)",
    "recording": "Voice recording (minimum length)",
    "textbook": "Textbook exercise (graded by Claude)",
    "honor": "Honour check-off (with a note)",
}


def read_conf():
    """/etc/poortwachter.conf holds the few things only root may change:
    which desktop user is watched and which processes count as Steam."""
    conf = {"user": None, "extra_process_names": [], "extra_cmdline_patterns": []}
    if CONF_PATH.exists():
        for line in CONF_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (p.strip() for p in line.split("=", 1))
            if key == "user":
                conf["user"] = value
            elif key == "extra_process_names":
                conf["extra_process_names"] = [v.strip() for v in value.split(",") if v.strip()]
            elif key == "extra_cmdline_patterns":
                conf["extra_cmdline_patterns"] = [v.strip() for v in value.split(",") if v.strip()]
    return conf
