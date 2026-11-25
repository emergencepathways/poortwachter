"""Keeps Steam closed while today's roster is unfinished.

Runs inside the root service. Every few seconds it scans /proc for Steam,
its web helper, and anything started by Steam (native or Proton games carry
SteamAppId / SteamGameId in their environment), but only for the watched
desktop user. When locked it asks them to close, then forces them."""

import os
import pwd
import signal
import subprocess
import threading
import time

from . import config, db, roster

# Process names (as in /proc/PID/comm, max 15 chars). Steam's helpers with
# generic names (reaper, pv-bwrap) are caught by their command line instead,
# so an unrelated program called "reaper" is left alone.
STEAM_NAMES = {"steam", "steam.sh", "steamwebhelper", "steamerrorrepor", "steam-runtime-l"}
CMDLINE_PATTERNS = (
    "/.steam/", "/.local/share/Steam/", "/steamapps/", "com.valvesoftware.Steam",
    "/usr/lib/steam/", "/usr/games/steam", "steam-runtime",
)
ENV_MARKERS = (b"SteamAppId=", b"SteamGameId=", b"STEAM_COMPAT_APP_ID=")

SCAN_EVERY = 3
NOTIFY_EVERY = 60


def _read(path, mode="rb"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return None


def steam_pids(uid, extra_names=(), extra_patterns=()):
    me = {os.getpid(), os.getppid()}
    names = STEAM_NAMES | set(extra_names)
    patterns = CMDLINE_PATTERNS + tuple(extra_patterns)
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in me:
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        comm = (_read(f"/proc/{pid}/comm", "r") or "").strip()
        cmd = (_read(f"/proc/{pid}/cmdline") or b"").replace(b"\0", b" ").decode(errors="replace")
        if "poortwachter" in cmd:
            continue
        hit = comm in names or any(p in cmd for p in patterns)
        if not hit:
            env = _read(f"/proc/{pid}/environ") or b""
            hit = any(m in env for m in ENV_MARKERS)
        if hit:
            found.append((pid, comm or cmd[:40]))
    return found


def kill(pids, grace=4):
    for pid, _ in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + grace
    alive = list(pids)
    while alive and time.time() < deadline:
        time.sleep(0.3)
        alive = [(p, n) for p, n in alive if os.path.exists(f"/proc/{p}")]
    for pid, _ in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _user_env(user):
    pw = pwd.getpwnam(user)
    env = {
        "HOME": pw.pw_dir, "USER": user, "LOGNAME": user,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "DISPLAY": os.environ.get("POORTWACHTER_DISPLAY", ":0"),
        "XDG_RUNTIME_DIR": f"/run/user/{pw.pw_uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{pw.pw_uid}/bus",
    }
    xauth = os.path.join(pw.pw_dir, ".Xauthority")
    if os.path.exists(xauth):
        env["XAUTHORITY"] = xauth
    return env


def run_as_user(user, args):
    """Run a desktop command (notify-send, xdg-open) in the user's session."""
    try:
        subprocess.Popen(["runuser", "-u", user, "--", *args], env=_user_env(user),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def notify(user, state, open_app=True):
    left = state["total"] - state["done"]
    body = (f"{left} study task{'s' if left != 1 else ''} left today (~{state['minutes_left']} min). "
            "Eerst Nederlands, dan spelen!")
    run_as_user(user, ["notify-send", "-a", "Poortwachter", "-i", "dialog-information",
                       "Steam is locked", body])
    if open_app:
        run_as_user(user, ["xdg-open", f"http://{config.HOST}:{config.PORT}/"])


class Watchdog(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="watchdog")
        self.stop = threading.Event()
        self.last_notify = 0
        self.last_auto = 0

    def run(self):
        conf = config.read_conf()
        user = conf.get("user")
        if not user:
            db.log("watchdog", "no user in /etc/poortwachter.conf, so the watchdog is off")
            return
        uid = pwd.getpwnam(user).pw_uid
        while not self.stop.is_set():
            try:
                self.tick(user, uid, conf)
            except Exception as e:  # noqa: BLE001 — never die
                db.log("watchdog-error", repr(e))
            self.stop.wait(SCAN_EVERY)

    def tick(self, user, uid, conf):
        # Automatic checks (Anki/Boekenwurm) every 3 minutes, so finishing your
        # reviews opens Steam even if the app isn't open.
        if time.time() - self.last_auto > 180:
            self.last_auto = time.time()
            from . import actions
            actions.auto_check_all()
        state = roster.lock_state()
        if state["unlocked"]:
            return
        pids = steam_pids(uid, conf["extra_process_names"], conf["extra_cmdline_patterns"])
        if not pids:
            return
        now = time.time()
        if now - self.last_notify > NOTIFY_EVERY:
            self.last_notify = now
            notify(user, state)
        kill(pids)
        db.log("blocked", ", ".join(sorted({n for _, n in pids}))[:200])
