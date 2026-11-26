"""Command line.

  poortwachter serve          run the service (systemd does this)
  poortwachter status         is Steam locked? what's left?
  poortwachter open           open the study app in your browser
  sudo poortwachter noodsleutel
                              the emergency key: unlock Steam for the rest of
                              today without finishing. It's logged.
"""

import json
import os
import signal
import sys
import time
import urllib.request

from . import config

PHRASE = "Ik kies ervoor om vandaag niet te studeren, en dat is mijn eigen keuze."
COOLDOWN = 90


def _api(path):
    with urllib.request.urlopen(f"http://{config.HOST}:{config.PORT}{path}", timeout=5) as r:
        return json.loads(r.read())


def serve():
    from . import db, server, watchdog
    httpd = server.make_server()
    db.log("service", "started")
    wd = watchdog.Watchdog()
    wd.start()
    watchdog.Jobs().start()

    def stop(*_):
        wd.stop.set()
        httpd.shutdown()
    signal.signal(signal.SIGTERM, lambda *_: (wd.stop.set(), os._exit(0)))
    print(f"Poortwachter listening on http://{config.HOST}:{config.PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop()


def status():
    try:
        s = _api("/api/lock")
    except OSError:
        print("The Poortwachter service isn't running (sudo systemctl status poortwachter).")
        return 1
    if s["unlocked"]:
        why = {"emergency": "emergency key used", "emergency_free_day": "emergency key (paid with a free day)",
               "free_day": "free day. Geniet ervan!", "install_day": "install day, so the lock starts at 02:00",
               }.get(s["reason"], "roster complete. Veel plezier!")
        print(f"Steam is UNLOCKED ({why}). Resets {s['next_reset'].replace('T', ' ')}.")
    else:
        print(f"Steam is LOCKED: {s['done']}/{s['total']} tasks done, ~{s['minutes_left']} min left.")
        print(f"Open the study app: http://{config.HOST}:{config.PORT}/")
    return 0


def noodsleutel():
    if os.geteuid() != 0:
        print("The emergency key needs sudo:  sudo poortwachter noodsleutel")
        return 1
    from . import db, roster
    db.init()
    day = roster.today()
    st = roster.lock_state(day)
    if st["unlocked"]:
        print("Steam is already unlocked today.")
        return 0
    used = db.connect().execute(
        "SELECT COUNT(*) FROM unlocks WHERE day >= date('now', '-30 days')").fetchone()[0]
    print(f"\nYou still have {st['total'] - st['done']} task(s) left (~{st['minutes_left']} min).")
    print(f"Emergency keys used in the last 30 days: {used}")
    from . import pto
    b = pto.balance()
    if b >= 24:
        print(f"You have {b // 24} banked free day(s): this will use one, and your chain stays intact.\n")
    else:
        print(f"No banked free day ({b} h saved): today will count as missed and your hours reset.\n")
    print("To unlock Steam for the rest of today, type this sentence exactly:\n")
    print(f"  {PHRASE}\n")
    try:
        typed = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1
    if typed != PHRASE:
        print("That didn't match. Steam stays locked.")
        return 1
    print(f"\nOK. Waiting {COOLDOWN} seconds. Press Ctrl+C to change your mind "
          "(or use the time to do a few flashcards).")
    try:
        for left in range(COOLDOWN, 0, -1):
            print(f"\r  {left:3d} s ", end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nCancelled. Steam stays locked. Succes!")
        return 1
    reason = input("\n\nOne line for your log: why today? ").strip() or "(no reason given)"
    from . import pto
    if pto.spend_for_emergency(day):
        db.log("emergency", f"{day}: {reason} (paid with a banked free day)")
        b = pto.balance()
        print(f"Steam is unlocked until 02:00. This used 1 banked free day, so your streak and "
              f"hour chain are safe. Left: {b // 24} days {b % 24} h.")
        return 0
    db.connect().execute("INSERT OR REPLACE INTO unlocks (day, reason, ts) VALUES (?, ?, ?)",
                         (day, reason, time.time()))
    db.log("emergency", f"{day}: {reason}")
    print("Steam is unlocked until 02:00. You had no banked free day, so today counts as missed: "
          "the hours toward your next free day reset, and up to 2 unfinished tasks carry over.")
    return 0


def open_app():
    import webbrowser
    webbrowser.open(f"http://{config.HOST}:{config.PORT}/")
    return 0


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "status"
    if cmd == "serve":
        serve()
        return 0
    if cmd == "status":
        return status()
    if cmd in ("noodsleutel", "emergency-unlock"):
        return noodsleutel()
    if cmd == "open":
        return open_app()
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())
