#!/bin/bash
# Removes Poortwachter. Your data in /var/lib/poortwachter (recordings, history,
# textbook copy) is kept unless you add --purge.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo:  sudo ./uninstall.sh"; exit 1; }
PHRASE="Ik haal de poortwachter weg."
echo "To remove Poortwachter, type:  $PHRASE"
read -r -p "> " TYPED
[ "$TYPED" = "$PHRASE" ] || { echo "Didn't match. Nothing removed."; exit 1; }

USER_NAME="$(sed -n 's/^user *= *//p' /etc/poortwachter.conf 2>/dev/null || true)"
systemctl disable --now poortwachter 2>/dev/null || true
rm -f /etc/systemd/system/poortwachter.service
systemctl daemon-reload
rm -rf /opt/poortwachter /usr/local/bin/poortwachter /usr/local/bin/poortwachter-steam /usr/local/bin/poortwachter-update /etc/poortwachter.conf
if [ -n "$USER_NAME" ]; then
  H="$(getent passwd "$USER_NAME" | cut -d: -f6)"
  rm -f "$H/.local/share/applications/poortwachter.desktop" "$H/.config/autostart/poortwachter.desktop"
  for f in steam.desktop com.valvesoftware.Steam.desktop; do
    grep -q poortwachter-steam "$H/.local/share/applications/$f" 2>/dev/null && rm -f "$H/.local/share/applications/$f"
  done
fi
if [ "${1:-}" = "--purge" ]; then rm -rf /var/lib/poortwachter; echo "Data removed."; fi
echo "Poortwachter removed. Tot ziens!"
