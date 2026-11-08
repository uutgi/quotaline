#!/usr/bin/env bash
# Opens the counter in a small terminal window (Linux). Used by the quotaline.desktop launcher.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CMD=(python3 "$DIR/quotaline.py" --watch 60)
if command -v gnome-terminal >/dev/null; then
  exec gnome-terminal --title=quotaline --geometry=72x14 -- "${CMD[@]}"
elif command -v mate-terminal >/dev/null; then
  exec mate-terminal --title=quotaline --geometry=72x14 -e "${CMD[*]}"
elif command -v xfce4-terminal >/dev/null; then
  exec xfce4-terminal --title=quotaline --geometry=72x14 -e "${CMD[*]}"
else
  exec x-terminal-emulator -e "${CMD[*]}"
fi
