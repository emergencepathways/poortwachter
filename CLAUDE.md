# Poortwachter: notes for Claude sessions

A personal Linux Mint app that keeps Steam closed until the owner's daily Dutch study roster is done
(02:00 reset). Owner: Emergence Pathways (`emergencepathways/poortwachter`, public).

## Layout

- `poortwachter/`: Python 3, standard library only. `roster.py` (day rules, generation, carryover, lock),
  `events.py` (sessions), `pto.py` (earned free days, ledger in whole hours), `textbook.py` (sets, grading
  via the Claude API), `actions.py` (how each task kind is completed), `verify_*.py` (Anki, Boekenwurm),
  `watchdog.py` (closes Steam; `Jobs` runs calendar import), `server.py` (local web app + JSON API
  on 127.0.0.1:7623), `__main__.py` (CLI), `ical.py` (calendar import → sessions, the plan as a local feed),
  `grammar.py` (LanguageTool hints), `diary.py` (Boekenwurm's 20 diary prompts), `oefenen.py` (the exercise bank,
  one question at a time, the timer rules), `speech.py` (local Whisper for speaking exercises).
- Boekenwurm: connected through Boekenwurm's Connected apps (its `docs/CONNECTED_APPS.md`, migrations 0013-0015) by
  a browser handoff with PKCE: `/boekenwurm/connect` → `link_start()` (only the configured Boekenwurm address; the
  verifier stays in memory) → Boekenwurm, where the owner clicks Allow → `/boekenwurm/linked` → `link_finish()`
  (state must be ours; `connect.json`, then `claim_app_link(code, verifier)` → token). Then it calls
  `connection_summary(p_token, p_day, p_tz, p_start_hour=2)` for today only (`{error: pending|gone}` when not
  allowed / gone). Boekenwurm stores nothing for Poortwachter: no calendar. Boekenwurm will remove Connected apps
  before its public release (its `docs/PRELAUNCH.md`), so Boekenwurm tasks must fail with a clear message, never
  break the roster. The older email-code sign-in is a fallback only.
- `web/`: plain HTML/CSS/JS, no build step, works offline.
- `bin/`, `systemd/`, `desktop/`, `install.sh`, `uninstall.sh`: install and update. `bin/poortwachter-window` is the
  app window (GTK + WebKit2, system python3).
- `tests/`: `python3 -m unittest discover -s tests` (needs poppler-utils, qpdf, ffmpeg). CI runs them on every push.

## Rules

- The owner's choices are the spec: 02:00 reset, today fixed / plan 7 days ahead, carryover cap 2,
  textbook & reading caps 45 min, session days = sessions + one minimum task, PTO (+1 h per completed day,
  24 h per free day, missed day wipes partial hours, cap 15 → +1/yr → 20, emergency key spends a banked day),
  Boekenwurm reading = pages OR active minutes, at or above her level (any set level counts).
  Textbook (oefenen, docs/OEFENEN.md): weekdays ≥ 20 min (setting, never under 20), one question at a time in book
  order, counts at target time + ≥1 answer + open exercise finished, no answers by the target = session cancelled,
  2× target = "took long", more work after done to 2× = +1 h PTO once, hardcore = 70%, italki replaces the task and
  asks where to continue, focus never loses the book's place. Default plan: textbook + BW reading weekdays, Anki
  daily except 30 BW cards Wed + Sat, BW diary every other day. Ramp: +5 min/weekday after 7 in a row, max 120,
  weekends 45 and not counted. A past day the computer was never on (no roster, or one never frozen on the day)
  is paused like a free day; setting `offline_days` = "missed" counts it as missed. When the Claude key fails for
  good (400 credit, 401, 403; textbook.claude_call records it), Claude-only exercises wait and a textbook task that
  can't be done may be swapped (actions.replace_blocked) for another activity of about the same length, never fewer.
  Change them only when she asks, and update tests + CHANGELOG together.
- The repo is public: never commit the textbook, sets files or exercise banks built from it, its audio, recordings,
  databases or keys (see .gitignore). Tests use invented exercises (tests/test_v4.py).
- The lock must stay hard to bypass from the browser: no UI or API path that weakens today.

## Releasing

1. Bump `__version__` in `poortwachter/__init__.py` (semver).
2. Add a `## [x.y.z] - date` section at the top of CHANGELOG.md (a test checks they match).
3. Commit and push `main`. Then create the release tag `vX.Y.Z`: Claude sessions can push branches but
   not tags (403), so the owner publishes it under Releases → Draft a new release → new tag on `main`
   (or `git tag vX.Y.Z && git push origin vX.Y.Z` from her machine).
4. On her machine: `poortwachter-update`.
