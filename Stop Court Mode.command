#!/bin/bash
# Double-click when your matter is over.
H="$HOME/.casemonitor/app/court_mode.sh"; [ -x "$H" ] || H="$(dirname "$0")/court_mode.sh"
"$H" stop
echo; echo "You can close this window now."
