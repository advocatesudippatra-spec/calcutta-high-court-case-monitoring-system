#!/bin/bash
# Removes the background jobs from this Mac. Your settings and roster history in ~/.casemonitor are kept
# (delete that folder to remove everything).
for L in com.casemonitor.app.tick com.casemonitor.app.listener; do
  P="$HOME/Library/LaunchAgents/$L.plist"
  launchctl bootout "gui/$(id -u)" "$P" 2>/dev/null || true
  rm -f "$P"
done
echo "Case Monitoring System removed from this Mac's schedule. (Settings kept in ~/.casemonitor.)"
