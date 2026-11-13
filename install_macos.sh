#!/usr/bin/env bash
# macOS installer for quotaline (written without a Mac at hand: please report what breaks).
#   bash install_macos.sh [both|claude|openrouter] [tray+widget|widget|tray|none] [yes|no]
#     1. what to show
#     2. launcher: tray+widget = menu-bar icon plus the floating panel (the default), widget =
#        floating panel alone (tkinter), tray = menu-bar icon alone. The menu-bar icon installs
#        pystray and pillow; if that fails, tray+widget falls back to the widget alone.
#     3. start it at login (launchd agent)
# Also configures the Claude Code status line when `claude` is installed.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Tk 8.6.12 and older ignore mouse clicks on macOS 14+ (CPython issue 110218): prefer a python3 whose
# Tk is 8.6.13 or newer. python.org 3.11.7+/3.12+ qualify, and so does Homebrew's python-tk@3.x.
tk_ok() { "$1" - <<'PY' 2>/dev/null
import sys, tkinter
v = tuple(int(x) for x in tkinter.Tcl().eval('info patchlevel').split('.')[:3])
sys.exit(0 if v >= (8, 6, 13) else 1)
PY
}
pick_python() {
  local c
  for c in /opt/homebrew/bin/python3 /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
           /usr/local/bin/python3 python3 /usr/bin/python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    tk_ok "$(command -v "$c")" && { command -v "$c"; return; }
  done
  for c in /usr/local/bin/python3 python3 /usr/bin/python3; do   # any tkinter at all, with a warning
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import tkinter' >/dev/null 2>&1 && { echo "WARNING: $c has an old Tk; clicks may be ignored on macOS 14+. Fix: brew install python-tk@3.14" >&2; command -v "$c"; return; }
  done
  command -v python3 || true
}
PY="$(pick_python)"
[ -n "$PY" ] || { echo "python3 is required (brew install python-tk@3.14, or https://www.python.org/downloads/macos/)"; exit 1; }
echo "Using $PY ($("$PY" --version 2>&1), Tk $("$PY" -c 'import tkinter; print(tkinter.Tcl().eval("info patchlevel"))' 2>/dev/null || echo none))"
# Homebrew's Python refuses `pip install` outside a virtualenv (PEP 668): give quotaline its own venv there.
if ! "$PY" -m pip --version >/dev/null 2>&1 || "$PY" -c 'import sysconfig, os, sys; sys.exit(0 if os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 1)' 2>/dev/null; then
  VENV="$HOME/.local/share/quotaline/venv"
  [ -x "$VENV/bin/python3" ] || { echo "Creating virtualenv $VENV"; "$PY" -m venv "$VENV"; }
  PY="$VENV/bin/python3"
fi
# python.org builds ship without root certificates until "Install Certificates.command" is run:
# quotaline then uses certifi's bundle, so install it when the CA store is empty.
if ! "$PY" -c 'import ssl, sys; sys.exit(0 if ssl.create_default_context().cert_store_stats()["x509_ca"] else 1)' 2>/dev/null; then
  "$PY" -c 'import certifi' 2>/dev/null || { echo "No CA certificates in this Python: installing certifi"; "$PY" -m pip install --quiet certifi || echo "WARNING: pip install certifi failed; OpenRouter calls will fail with an SSL error"; }
fi
chmod +x "$DIR/quotaline.py"

ask() {
  local answer
  read -rp "$1 [$2] (default $3): " answer </dev/tty || answer=""
  answer="$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')"
  case "|$2|" in *"|$answer|"*) [ -n "$answer" ] && { echo "$answer"; return; } ;; esac
  echo "$3"
}
SHOW="${1:-$(ask 'What to show' 'both|claude|openrouter' both)}"
LAUNCHER="${2:-$(ask 'Launcher' 'tray+widget|widget|tray|none' tray+widget)}"
AUTOSTART="no"
[ "$LAUNCHER" != none ] && AUTOSTART="${3:-$(ask 'Start it at login' 'yes|no' no)}"

# 1. Saved preference
"$PY" "$DIR/quotaline.py" --show "$SHOW" --save

# 2. Claude Code status line
if command -v claude >/dev/null; then
  "$PY" "$DIR/quotaline.py" --install-statusline --show "$SHOW"
else
  echo "Claude Code is not installed on this machine: status line not configured."
fi

# 3. Launcher: a double-clickable .command on the Desktop, and optionally a launchd agent
if [ "$LAUNCHER" != none ]; then
  case "$LAUNCHER" in
    tray|tray+widget)
      if ! { "$PY" -m pip install --quiet pystray pillow && "$PY" -c 'import pystray, PIL' 2>/dev/null; }; then
        if [ "$LAUNCHER" = tray+widget ]; then
          echo "WARNING: pip install pystray pillow failed; the launcher opens the widget alone."
          LAUNCHER=widget
        else
          echo "WARNING: pip install pystray pillow failed; the menu-bar icon will not start."
        fi
      fi ;;
  esac
  case "$LAUNCHER" in
    widget|tray+widget)
      "$PY" -c 'import tkinter' 2>/dev/null || echo "WARNING: tkinter is missing in this Python; use python.org's installer or brew install python-tk" ;;
  esac
  case "$LAUNCHER" in
    tray+widget) ARGS=(--tray --open-widget) ;;
    *) ARGS=("--$LAUNCHER") ;;
  esac
  ARGS_XML=""
  for a in "${ARGS[@]}"; do ARGS_XML="$ARGS_XML<string>$a</string>"; done
  CMD="$HOME/Desktop/quotaline.command"
  printf '#!/usr/bin/env bash\nnohup "%s" "%s" %s >/dev/null 2>&1 &\n' "$PY" "$DIR/quotaline.py" "${ARGS[*]}" > "$CMD"
  chmod +x "$CMD"
  echo "Launcher: $CMD (double-click)"
  PLIST="$HOME/Library/LaunchAgents/quotaline.plist"
  if [ "$AUTOSTART" = yes ]; then
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>quotaline</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/quotaline.py</string>$ARGS_XML</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict></plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "Autostart: $PLIST (loaded)"
  else
    [ -f "$PLIST" ] && { launchctl unload "$PLIST" 2>/dev/null || true; rm -f "$PLIST"; }
  fi
fi

# 4. Check
"$PY" "$DIR/quotaline.py"
