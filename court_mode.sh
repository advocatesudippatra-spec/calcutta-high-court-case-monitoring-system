#!/bin/bash
# Court Mode: keep this Mac (and its screen) awake while you monitor the display board.
#
#   court_mode.sh start [HH:MM]   stay awake until HH:MM today (default 16:45)
#   court_mode.sh stop            let the Mac sleep normally again
#   court_mode.sh status
#
# Uses macOS's built-in "caffeinate". No system settings are changed; the moment
# Court Mode stops (by command, from Telegram with /courtoff, or at the end time),
# the Mac goes back to sleeping as usual.
# Keep the lid open and the charger connected: closing a MacBook's lid still sleeps it.

PIDFILE="$HOME/.casemonitor/courtmode.pid"
mkdir -p "$HOME/.casemonitor"

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-status}" in
  start)
    UNTIL="${2:-16:45}"
    NOW=$(date +%s)
    END=$(date -j -f "%Y-%m-%d %H:%M" "$(date +%Y-%m-%d) $UNTIL" +%s 2>/dev/null)
    if [ -z "$END" ]; then echo "Time must look like 16:45"; exit 1; fi
    if [ "$END" -le "$NOW" ]; then echo "$UNTIL has already passed today."; exit 1; fi
    running && kill "$(cat "$PIDFILE")" 2>/dev/null
    # -d screen awake, -i no idle sleep, -s no sleep on charger, -m disks awake
    nohup caffeinate -d -i -s -m -t $((END - NOW)) >/dev/null 2>&1 &
    echo $! > "$PIDFILE"
    echo "Court Mode ON: this Mac and its screen will stay awake until $UNTIL."
    echo "Stop it any time: double-click 'Stop Court Mode', or send /courtoff to your bot."
    ;;
  stop)
    if running; then kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; echo "Court Mode OFF: the Mac can sleep normally again."
    else rm -f "$PIDFILE"; echo "Court Mode was not running."; fi
    ;;
  status)
    if running; then
      echo "Court Mode is ON (keeping this Mac awake)."
    else
      echo "Court Mode is OFF."
    fi
    ;;
  *) echo "Use: court_mode.sh start [HH:MM] | stop | status"; exit 1 ;;
esac
