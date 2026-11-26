"""Speech to text for speaking exercises, on this computer only (Whisper, via faster-whisper).

Installed separately with `sudo poortwachter-whisper-setup` (about 1 GB: a Python environment and the "small"
multilingual model) into /opt/poortwachter-whisper. Recordings never leave the computer. When it isn't installed or
fails, speaking exercises say so and offer to skip ("didn't work").
"""

import os
import shutil
import subprocess
from pathlib import Path

from . import db

HELPER = Path(os.environ.get("POORTWACHTER_WHISPER", "/opt/poortwachter-whisper/bin/poortwachter-transcribe"))


class SpeechError(Exception):
    pass


def available():
    return bool(db.get_setting("whisper")) and HELPER.exists() and os.access(HELPER, os.X_OK)


def transcribe(path, timeout=600):
    if not available():
        raise SpeechError("Speech checking isn't installed. Run: sudo poortwachter-whisper-setup")
    try:
        r = subprocess.run([str(HELPER), str(path)], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SpeechError("Speech checking took too long.") from None
    except OSError as e:
        raise SpeechError(f"Speech checking couldn't start: {e}") from None
    if r.returncode != 0:
        raise SpeechError("Speech checking failed: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1][:200])
    text = r.stdout.strip()
    if not text:
        raise SpeechError("No speech was recognised in the recording.")
    return text


def status():
    return {"installed": HELPER.exists(), "enabled": bool(db.get_setting("whisper")),
            "ffmpeg": shutil.which("ffmpeg") is not None}
