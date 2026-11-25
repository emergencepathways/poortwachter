#!/bin/bash
# Poortwachter installer for Linux Mint (and other Ubuntu/Debian systems).
#   sudo ./install.sh                 watch the user who ran sudo
#   sudo ./install.sh --user NAME     watch someone else
#   sudo ./install.sh --no-autostart  don't open the study app at login
# Re-running it upgrades in place and keeps your data (/var/lib/poortwachter).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-}"
AUTOSTART=1
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="$2"; shift 2 ;;
    --no-autostart) AUTOSTART=0; shift ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo:  sudo ./install.sh"; exit 1; fi
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = "root" ]; then
  echo "Couldn't tell which desktop user to watch. Use: sudo ./install.sh --user yourname"; exit 1
fi
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
[ -d "$USER_HOME" ] || { echo "No home folder for $USER_NAME"; exit 1; }

echo "==> Installing for user: $USER_NAME"

echo "==> Packages (python3, poppler-utils, qpdf, ffmpeg, libnotify-bin)"
apt-get update -qq || true
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 poppler-utils qpdf ffmpeg libnotify-bin >/dev/null

PYV=$(python3 -c 'import sys; print(sys.version_info >= (3, 8))')
[ "$PYV" = "True" ] || { echo "Python 3.8 or newer is needed."; exit 1; }

echo "==> Files → /opt/poortwachter"
systemctl stop poortwachter 2>/dev/null || true
rm -rf /opt/poortwachter
mkdir -p /opt/poortwachter
cp -r "$HERE/poortwachter" "$HERE/web" /opt/poortwachter/
find /opt/poortwachter -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root /opt/poortwachter
chmod -R u=rwX,go=rX /opt/poortwachter
install -m 755 "$HERE/bin/poortwachter" /usr/local/bin/poortwachter
install -m 755 "$HERE/bin/poortwachter-steam" /usr/local/bin/poortwachter-steam

echo "==> Config /etc/poortwachter.conf"
if [ ! -f /etc/poortwachter.conf ]; then
  cat > /etc/poortwachter.conf <<CONF
# Poortwachter: only root can change this file.
# The desktop user whose Steam is locked:
user = $USER_NAME
# More programs to block, comma-separated (process names, max 15 characters):
extra_process_names =
# ...or anything whose command line contains one of these:
extra_cmdline_patterns =
CONF
else
  sed -i "s/^user *=.*/user = $USER_NAME/" /etc/poortwachter.conf
fi
chmod 644 /etc/poortwachter.conf

mkdir -p /var/lib/poortwachter
chmod 700 /var/lib/poortwachter

echo "==> Service"
install -m 644 "$HERE/systemd/poortwachter.service" /etc/systemd/system/poortwachter.service
systemctl daemon-reload
systemctl enable --now poortwachter >/dev/null
sleep 1.5
if ! systemctl is-active --quiet poortwachter; then
  echo "The service didn't start. See: journalctl -u poortwachter -n 50"; exit 1
fi

echo "==> Menu entries"
as_user() { runuser -u "$USER_NAME" -- "$@"; }
APPS="$USER_HOME/.local/share/applications"
as_user mkdir -p "$APPS"
install -o "$USER_NAME" -m 644 "$HERE/desktop/poortwachter.desktop" "$APPS/poortwachter.desktop"

# Put the gate in front of Steam's own menu/panel launchers (native and Flatpak).
GATED=0
for SRC in /usr/share/applications/steam.desktop \
           /var/lib/flatpak/exports/share/applications/com.valvesoftware.Steam.desktop \
           "$USER_HOME/.local/share/flatpak/exports/share/applications/com.valvesoftware.Steam.desktop"; do
  [ -f "$SRC" ] || continue
  DEST="$APPS/$(basename "$SRC")"
  sed -E 's#^Exec=(.*)$#Exec=/usr/local/bin/poortwachter-steam -- \1#' "$SRC" > "$DEST.tmp"
  # Don't double-wrap on re-install.
  sed -i 's#poortwachter-steam -- /usr/local/bin/poortwachter-steam -- #poortwachter-steam -- #' "$DEST.tmp"
  mv "$DEST.tmp" "$DEST"; chown "$USER_NAME": "$DEST"; chmod 644 "$DEST"
  GATED=$((GATED + 1))
done
as_user update-desktop-database "$APPS" 2>/dev/null || true

if [ "$AUTOSTART" = 1 ]; then
  as_user mkdir -p "$USER_HOME/.config/autostart"
  install -o "$USER_NAME" -m 644 "$HERE/desktop/poortwachter-autostart.desktop" "$USER_HOME/.config/autostart/poortwachter.desktop"
fi

cat <<DONE

  ✓ Poortwachter is running.

    Study app:      http://127.0.0.1:7623/   (also "Poortwachter" in the menu)
    Status:         poortwachter status
    Emergency key:  sudo poortwachter noodsleutel
    Logs:           journalctl -u poortwachter -f

  Steam launchers gated: $GATED   (Steam started any other way is closed by the service)

  Next: open the app → Settings to add your Claude key, Anki and Boekenwurm,
  then Textbook to upload your PDF and its exercise list.
  Panel or desktop shortcut to Steam you made yourself? Re-pin it from the menu
  so it uses the gated launcher.

DONE
