#!/bin/bash
# Try the system for any name without installing anything or sending any message:
#   ./try.sh "ARUN KUMAR SEN, A K SEN" [DDMMYYYY]
# Uses a separate test folder (~/.casemonitor-try); your installed copy is not touched.
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
NAMES="${1:?Give a name, e.g. ./try.sh \"ARUN KUMAR SEN\"}"
DATE="${2:-$(date +%d%m%Y)}"
export CASEMONITOR_HOME="$HOME/.casemonitor-try"
mkdir -p "$CASEMONITOR_HOME"
if [ ! -x "$CASEMONITOR_HOME/venv/bin/python" ]; then
  echo "Preparing a test environment (one time)..."
  /usr/bin/python3 -m venv "$CASEMONITOR_HOME/venv"
  "$CASEMONITOR_HOME/venv/bin/pip" install -q --disable-pip-version-check -r "$REPO/requirements.txt"
fi
printf 'ADVOCATE_NAMES=%s\nDEVICE_NAME=try\n' "$NAMES" > "$CASEMONITOR_HOME/config.env"
"$CASEMONITOR_HOME/venv/bin/python" "$REPO/casemonitor.py" report --date "$DATE" --dry-run
