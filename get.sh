#!/bin/bash
# One-line installer:
#   curl -fsSL https://raw.githubusercontent.com/advocatesudippatra-spec/calcutta-high-court-case-monitoring-system/main/get.sh | bash
# For the private copy (before it is public), use the GitHub tool instead:
#   gh repo clone advocatesudippatra-spec/calcutta-high-court-case-monitoring-system ~/CaseMonitoringSystem && ~/CaseMonitoringSystem/install.sh
set -e
REPO_SLUG="advocatesudippatra-spec/calcutta-high-court-case-monitoring-system"
DEST="$HOME/CaseMonitoringSystem"
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" pull --ff-only -q
elif command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  gh repo clone "$REPO_SLUG" "$DEST"
else
  git clone -q "https://github.com/$REPO_SLUG.git" "$DEST"
fi
# the setup questions need the keyboard even when this script arrives through a pipe
bash "$DEST/install.sh" < /dev/tty
