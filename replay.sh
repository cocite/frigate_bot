#!/usr/bin/env bash
# Replays the last Frigate event from the log into MQTT — for debugging.
#
# Run from anywhere:
#   ./replay.sh            # last payload from frigate_bot.log (or debug.log)
#   ./replay.sh <logfile>  # last payload from the given log
#
# Only type="end" events are logged as payloads, no filtering needed.
set -euo pipefail

cd "$(dirname "$0")"   # work from frigate_bot; docker compose finds the compose file itself (searches parent directories)

LOG_FILE="${1:-}"
if [[ -z "$LOG_FILE" ]]; then
    for f in frigate_bot.log debug.log; do
        [[ -f "$f" ]] && grep -q 'Payload: {' "$f" && LOG_FILE="$f" && break
    done
fi

if [[ -z "$LOG_FILE" || ! -f "$LOG_FILE" ]]; then
    echo "No log with payloads found (looked for frigate_bot.log and debug.log next to the script)" >&2
    exit 1
fi

PAYLOAD=$(grep -o 'Payload: {.*' "$LOG_FILE" | tail -1 | sed 's/^Payload: //' || true)
if [[ -z "$PAYLOAD" ]]; then
    echo "No 'Payload: {...}' lines in $LOG_FILE" >&2
    exit 1
fi

echo "Log:     $LOG_FILE"
echo "Payload: ${PAYLOAD:0:120}..."
docker compose exec -T mosquitto mosquitto_pub -t frigate/reviews -m "$PAYLOAD"
echo "Published to frigate/reviews. Watch: docker compose logs -f frigate_bot"
