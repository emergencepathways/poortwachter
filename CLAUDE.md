# Poortwachter: notes for Claude sessions

A personal Linux Mint app that keeps Steam closed until the owner's daily Dutch study roster is done
(02:00 reset). Owner: Emergence Pathways (`emergencepathways/poortwachter`, public).

## Layout

- `poortwachter/`: Python 3, standard library only. `roster.py` (day rules, generation, carryover, lock),
  `events.py` (sessions), `pto.py` (earned free days, ledger in whole hours), `textbook.py` (sets, grading
  via the Claude API), `actions.py` (how each task kind is completed), `verify_*.py` (Anki, Boekenwurm),
  `watchdog.py` (closes Steam; `Jobs` runs calendar import), `server.py` (local web app + JSON API
  on 127.0.0.1:7623), `__main__.py` (CLI), `ical.py` (calendar import → sessions, the plan as a local feed),
  `grammar.py` (LanguageTool hints), `diary.py` (Boekenwurm's 20 diary prompts).
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
  Change them only when she asks, and update tests + CHANGELOG together.
- The repo is public: never commit the textbook, sets files built from it, recordings, databases or keys (see .gitignore).
- The lock must stay hard to bypass from the browser: no UI or API path that weakens today.

## Releasing

1. Bump `__version__` in `poortwachter/__init__.py` (semver).
2. Add a `## [x.y.z] - date` section at the top of CHANGELOG.md (a test checks they match).
3. Commit and push `main`. Then create the release tag `vX.Y.Z`: Claude sessions can push branches but
   not tags (403), so the owner publishes it under Releases → Draft a new release → new tag on `main`
   (or `git tag vX.Y.Z && git push origin vX.Y.Z` from her machine).
4. On her machine: `poortwachter-update`.
