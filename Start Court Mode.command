#!/bin/bash
# Double-click to keep this Mac awake for court (until 4:45 PM by default).
H="$HOME/.casemonitor/app/court_mode.sh"; [ -x "$H" ] || H="$(dirname "$0")/court_mode.sh"
read -p "Keep awake until what time? (press Enter for 16:45): " T
"$H" start "${T:-16:45}"
echo; echo "You can close this window now."
