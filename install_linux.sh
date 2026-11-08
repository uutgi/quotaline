#!/usr/bin/env bash
# Linux installer for quotaline. Asks three questions (or takes them as arguments):
#   bash install_linux.sh [both|claude|openrouter] [window|widget|tray|none] [yes|no]
#     1. what to show      2. launcher: window = small terminal, widget = floating panel (needs
#     python3-tk), tray = indicator icon (installs pystray; needs the desktop's GTK bindings)
#     3. start it with the session (autostart)
# Also sets the folder icon and, when `claude` is installed, the Claude Code status line.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 is required (sudo apt install python3)"; exit 1; }
chmod +x "$DIR/quotaline.py" "$DIR/quotaline.sh" 2>/dev/null || true

ask() {  # ask "question" "a|b|c" default
  local answer
  read -rp "$1 [$2] (default $3): " answer </dev/tty || answer=""
  answer="${answer,,}"
  case "|$2|" in *"|$answer|"*) [ -n "$answer" ] && { echo "$answer"; return; } ;; esac
  echo "$3"
}
SHOW="${1:-$(ask 'What to show' 'both|claude|openrouter' both)}"
LAUNCHER="${2:-$(ask 'Launcher' 'window|widget|tray|none' widget)}"
AUTOSTART="no"
[ "$LAUNCHER" != none ] && AUTOSTART="${3:-$(ask 'Start it with the session' 'yes|no' no)}"

# 1. Saved preference
"$PY" "$DIR/quotaline.py" --show "$SHOW" --save

# 2. Folder icon (Nemo, Caja and Nautilus honour metadata::custom-icon)
if command -v gio >/dev/null; then
  gio set "$DIR" metadata::custom-icon "file://$DIR/_icon.png" 2>/dev/null && echo "Folder icon set." || true
fi

# 3. Claude Code status line, only if Claude Code is installed
if command -v claude >/dev/null; then
  "$PY" "$DIR/quotaline.py" --install-statusline --show "$SHOW"
else
  echo "Claude Code is not installed on this machine: status line not configured."
fi

# 4. Launcher
if [ "$LAUNCHER" != none ]; then
  case "$LAUNCHER" in
    window) EXEC="$DIR/quotaline.sh" ;;
    widget)
      "$PY" -c 'import tkinter' 2>/dev/null || echo "WARNING: tkinter is missing: sudo apt install python3-tk"
      EXEC="$PY $DIR/quotaline.py --widget" ;;
    tray)
      "$PY" -m pip install --quiet --user pystray pillow || echo "WARNING: pip install pystray pillow failed"
      "$PY" -c 'import gi' 2>/dev/null || echo "WARNING: the tray needs the GTK bindings: sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1"
      EXEC="$PY $DIR/quotaline.py --tray" ;;
  esac
  DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
  mkdir -p "$DESKTOP" "$HOME/.local/share/applications"
  ENTRY="[Desktop Entry]
Type=Application
Name=quotaline
Comment=OpenRouter balance and Claude Code limits ($LAUNCHER)
Exec=$EXEC
Icon=$DIR/_icon.png
Terminal=false
Categories=Utility;"
  for target in "$DESKTOP/quotaline.desktop" "$HOME/.local/share/applications/quotaline.desktop"; do
    printf '%s\n' "$ENTRY" > "$target"; chmod +x "$target"
    # mark the launcher as trusted: GNOME/Cinnamon/MATE look at metadata::trusted, Xfce 4.18+ at a checksum
    gio set "$target" metadata::trusted true 2>/dev/null || true
    gio set "$target" metadata::xfce-exe-checksum "$(sha256sum "$target" | cut -d' ' -f1)" 2>/dev/null || true
    echo "Launcher: $target"
  done
  if [ "$AUTOSTART" = yes ]; then
    mkdir -p "$HOME/.config/autostart"
    printf '%s\nX-GNOME-Autostart-enabled=true\n' "$ENTRY" > "$HOME/.config/autostart/quotaline.desktop"
    echo "Autostart: $HOME/.config/autostart/quotaline.desktop"
  else
    rm -f "$HOME/.config/autostart/quotaline.desktop"
  fi
fi

# 5. Check
"$PY" "$DIR/quotaline.py"
