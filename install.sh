#!/bin/bash
# Calcutta High Court Case Monitoring System: installer for macOS.
# Safe to run again (that is also how you update). Asks the setup questions the first time.
#   ./install.sh
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${CASEMONITOR_HOME:-$HOME/.casemonitor}"
APP="$HOME_DIR/app"

echo "== Calcutta High Court Case Monitoring System: installer =="
if ! /usr/bin/python3 -c "import sys" 2>/dev/null; then
  echo "Python is missing. A window will ask to install Apple's Command Line Tools. Click Install, then run this again."
  xcode-select --install || true
  exit 1
fi

mkdir -p "$APP" "$HOME_DIR/logs" "$HOME_DIR/reports" "$HOME_DIR/cache" "$HOME_DIR/monthly" "$HOME_DIR/bin"
cp "$REPO"/causelist.py "$REPO"/casemonitor.py "$REPO"/roster.py "$REPO"/court_mode.sh "$REPO"/watcher.user.js "$REPO"/requirements.txt "$APP"/
echo "$REPO" > "$HOME_DIR/repo_path"

if [ ! -x "$HOME_DIR/venv/bin/python" ]; then
  echo "Creating the Python environment (one time)..."
  /usr/bin/python3 -m venv "$HOME_DIR/venv"
fi
"$HOME_DIR/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$HOME_DIR/venv/bin/pip" install -q -r "$APP/requirements.txt"

if [ ! -f "$HOME_DIR/config.env" ]; then
  NAME="$(scutil --get ComputerName 2>/dev/null || hostname -s)"
  cat > "$HOME_DIR/config.env" <<EOF
# Case Monitoring System settings (this file stays on this Mac). Change them with: casemonitor setup
DEVICE_NAME=$NAME
# Pace of a Bench, minutes per ordinary motion item (raise it if Benches are slower)
MIN_PER_UNIT=5
EOF
  chmod 600 "$HOME_DIR/config.env"
fi

# Picture reader for notices sent as photos (macOS's built-in text recognition)
if [ -f "$REPO/ocr.swift" ] && { [ ! -x "$HOME_DIR/bin/ocr" ] || [ "$REPO/ocr.swift" -nt "$HOME_DIR/bin/ocr" ]; }; then
  echo "Preparing the picture reader (one time, about a minute)..."
  swiftc -O "$REPO/ocr.swift" -o "$HOME_DIR/bin/ocr" 2>>"$HOME_DIR/logs/install.log" || echo "(Picture reader not built: notices sent as photos won't be read; text and PDFs still work.)"
fi

# Launcher: update from GitHub once a day, then run the check.
cat > "$HOME_DIR/run.sh" <<'EOF'
#!/bin/bash
H="${CASEMONITOR_HOME:-$HOME/.casemonitor}"
REPO="$(cat "$H/repo_path" 2>/dev/null)"
STAMP="$H/.updated_$(date +%Y%m%d)"
if [ -n "$REPO" ] && [ -d "$REPO/.git" ] && [ ! -f "$STAMP" ]; then
  if git -C "$REPO" pull --ff-only -q 2>>"$H/logs/update.log"; then
    cp "$REPO"/causelist.py "$REPO"/casemonitor.py "$REPO"/roster.py "$REPO"/court_mode.sh "$REPO"/watcher.user.js "$H/app/" 2>>"$H/logs/update.log" && touch "$STAMP"
    if [ -f "$REPO/ocr.swift" ] && [ "$REPO/ocr.swift" -nt "$H/bin/ocr" ]; then swiftc -O "$REPO/ocr.swift" -o "$H/bin/ocr" 2>>"$H/logs/update.log"; fi
    rm -f $(ls "$H"/.updated_* 2>/dev/null | grep -v "$STAMP") 2>/dev/null
  fi
fi
exec "$H/venv/bin/python" "$H/app/casemonitor.py" "$@"
EOF
chmod +x "$HOME_DIR/run.sh"
ln -sf "$HOME_DIR/run.sh" "$HOME_DIR/casemonitor"

if ! grep -q '^ADVOCATE_NAMES=..*' "$HOME_DIR/config.env"; then
  "$HOME_DIR/casemonitor" setup
elif [ -z "$CASEMONITOR_NO_LAUNCHD" ]; then
  "$HOME_DIR/casemonitor" services
  echo "Updated. Change your answers any time with:  $HOME_DIR/casemonitor setup"
fi
