#!/usr/bin/env bash
# Linux installer for quotaline. Asks three questions (or takes them as arguments):
#   bash install_linux.sh [both|claude|openrouter] [tray+widget|window|widget|tray|none] [yes|no]
#     1. what to show
#     2. launcher: tray+widget = indicator icon plus the floating panel (the default), window = small
#        terminal, widget = floating panel alone (needs python3-tk; no taskbar entry, the panel has
#        no title bar), tray = indicator icon alone. The tray installs pystray and pillow, and needs
#        the desktop's GTK bindings; if they cannot be installed, tray+widget falls back to widget.
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

# pystray + pillow for the tray. Debian/Ubuntu/Mint 23.04+ refuse `pip install --user` (PEP 668) and
# some installs have no pip at all: then use a virtualenv that still sees the system's packages
# (python3-gi for the indicator, tkinter for the widget). On success PY points at the Python to use.
tray_deps() { "$1" -c 'import pystray, PIL' 2>/dev/null; }
ensure_tray() {
  tray_deps "$PY" && return 0
  "$PY" -m pip install --quiet --user pystray pillow 2>/dev/null && tray_deps "$PY" && return 0
  local venv="$HOME/.local/share/quotaline/venv"
  if ! "$venv/bin/python3" -m pip --version >/dev/null 2>&1; then
    rm -rf "$venv"     # a half-made one (python3-venv missing) would fail the same way every time
    echo "Creating virtualenv $venv"
    "$PY" -m venv --system-site-packages "$venv" >/dev/null 2>&1 || rm -rf "$venv"
  fi
  if [ -x "$venv/bin/python3" ] \
     && { tray_deps "$venv/bin/python3" || "$venv/bin/python3" -m pip install --quiet pystray pillow; } \
     && tray_deps "$venv/bin/python3"; then
    PY="$venv/bin/python3"
    return 0
  fi
  echo "WARNING: could not install pystray and pillow. Try: sudo apt install python3-venv  (then run this again)"
  echo "         or: sudo apt install python3-pystray python3-pil"
  return 1
}

SHOW="${1:-$(ask 'What to show' 'both|claude|openrouter' both)}"
LAUNCHER="${2:-$(ask 'Launcher' 'tray+widget|window|widget|tray|none' tray+widget)}"
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
    tray|tray+widget)
      if ensure_tray; then
        "$PY" -c 'import gi' 2>/dev/null || echo "WARNING: the tray needs the GTK bindings: sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1"
      elif [ "$LAUNCHER" = tray+widget ]; then
        echo "The launcher opens the widget alone."
        LAUNCHER=widget
      else
        echo "The tray will not start until pystray and pillow are installed."
      fi ;;
  esac
  case "$LAUNCHER" in
    widget|tray+widget)
      "$PY" -c 'import tkinter' 2>/dev/null || echo "WARNING: tkinter is missing: sudo apt install python3-tk" ;;
  esac
  case "$LAUNCHER" in
    window) EXEC="$DIR/quotaline.sh" ;;
    widget) EXEC="$PY $DIR/quotaline.py --widget" ;;
    tray) EXEC="$PY $DIR/quotaline.py --tray" ;;
    tray+widget) EXEC="$PY $DIR/quotaline.py --tray --open-widget" ;;
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
