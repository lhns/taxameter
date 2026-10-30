#!/usr/bin/env bash
# Idle watchdog for a rented box: stop the instance (halts GPU billing; the disk is kept and still billed) after
# IDLE_MIN consecutive idle minutes. Runs ON the instance. Settings come from /root/.taxameter/env.
#   nohup setsid bash idle_autostop.sh > /root/autostop.log 2>&1 &
# Busy = a process matching TAXAMETER_BUSY or TAXAMETER_BATCH, a box.sh provision/models run (downloads and installs
# look idle), or GPU utilisation above BUSY_UTIL percent. Project patterns must be anchored: a bare project name
# would match this script's own path and the box would never go idle. Empty patterns are skipped (pgrep -f ''
# matches every process).
# Vast: the vastai CLI only takes the key as --api-key (no header file or stdin), so it stays on argv.
# Restarting a stopped instance needs the provider console or an account API key; this script never destroys anything.
# Kept alive across container restarts by box.sh ensure-watchdog (supervisor / onstart).
TAXAMETER_HOME="${TAXAMETER_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# shellcheck disable=SC1091
[ -f "$TAXAMETER_HOME/env" ] && . "$TAXAMETER_HOME/env"
IDLE_MIN="${TAXAMETER_IDLE_MIN:-20}"
BUSY_UTIL="${TAXAMETER_BUSY_UTIL:-10}"
VASTAI="${VASTAI:-/opt/instance-tools/bin/vastai}"
idle=0

count_procs() {  # number of processes matching any non-empty pattern; 0 when there is none
  local joined="" p
  for p in "$@"; do [ -n "$p" ] && joined="${joined:+$joined|}($p)"; done
  [ -n "$joined" ] || { echo 0; return; }
  pgrep -f "$joined" | wc -l
}

self_stop() {
  case "${TAXAMETER_PROVIDER:-}" in
    vast) "$VASTAI" stop instance "$CONTAINER_ID" --api-key "$CONTAINER_API_KEY" > /tmp/autostop_call.log 2>&1 ;;
    *) echo "no self-stop for provider '${TAXAMETER_PROVIDER:-}'" > /tmp/autostop_call.log; return 1 ;;
  esac
}

if [ "${TAXAMETER_PROVIDER:-}" = vast ] && [ -z "${CONTAINER_API_KEY:-}" ]; then
  echo "$(date +%T) ERROR: CONTAINER_API_KEY is not set: the stop call will fail and this box will never auto-stop"
fi
echo "$(date +%T) autostop armed: stop after ${IDLE_MIN} idle minutes (instance ${CONTAINER_ID:-?})"
while true; do
  busy=0
  [ "$(count_procs "${TAXAMETER_BUSY:-}" "${TAXAMETER_BATCH:-}" 'box[.]sh (provision|models)')" -gt 0 ] && busy=1
  util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | sort -nr | head -1)
  [ -n "$util" ] && [ "$util" -gt "$BUSY_UTIL" ] && busy=1
  if [ "$busy" = 1 ]; then idle=0; else idle=$((idle + 1)); fi
  echo "$(date +%T) busy=$busy util=${util:-?} idle_min=$idle"
  if [ "$idle" -ge "$IDLE_MIN" ]; then
    # Never exit after the stop call: when the host's network is down the call fails (seen on host 483833), so
    # keep retrying every minute while still idle. A successful stop ends this process with the container.
    echo "$(date +%T) idle for ${idle} min -> stopping instance ${CONTAINER_ID:-?}"
    if self_stop; then
      echo "$(date +%T) stop call returned ok; retrying in 60 s if still running"
    else
      echo "$(date +%T) stop call FAILED (network?): $(tail -1 /tmp/autostop_call.log); retrying in 60 s"
    fi
  fi
  sleep 60
done
