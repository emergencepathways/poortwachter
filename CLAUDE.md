# Poortwachter: notes for Claude sessions

A personal Linux Mint app that keeps Steam closed until the owner's daily Dutch study roster is done
(02:00 reset). Owner: Emergence Pathways (`emergencepathways/poortwachter`, public).

## Layout

- `poortwachter/`: Python 3, standard library only. `roster.py` (day rules, generation, carryover, lock),
  `events.py` (sessions), `pto.py` (earned free days, ledger in whole hours), `textbook.py` (sets, grading
  via the Claude API), `actions.py` (how each task kind is completed), `verify_*.py` (Anki, Boekenwurm),
  `watchdog.py` (closes Steam), `server.py` (local web app + JSON API on 127.0.0.1:7623), `__main__.py` (CLI).
- `web/`: plain HTML/CSS/JS, no build step, works offline.
- `bin/`, `systemd/`, `desktop/`, `install.sh`, `uninstall.sh`: install and update.
- `tests/`: `python3 -m unittest discover -s tests` (needs poppler-utils, qpdf, ffmpeg). CI runs them on every push.

## Rules

- The owner's choices are the spec: 02:00 reset, today fixed / plan 7 days ahead, carryover cap 2,
  textbook & reading caps 45 min, session days = sessions + one minimum task, PTO (+1 h per completed day,
  24 h per free day, missed day wipes partial hours, cap 15 → +1/yr → 20, emergency key spends a banked day).
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
