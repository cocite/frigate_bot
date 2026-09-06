#!/usr/bin/env bash
# Реплей последнего события Frigate из лога в MQTT — для отладки.
#
# Запуск из любого места:
#   ./replay.sh          # последний payload из свежего лога
#   ./replay.sh <файл>   # payload из указанного лога
#
# В лог попадают только события type="end", фильтровать не нужно.
set -euo pipefail

cd "$(dirname "$0")"   # работаем из frigate_bot; docker compose найдёт compose-файл сам (ищет вверх по дереву)

LOG_FILE="${1:-}"
if [[ -z "$LOG_FILE" ]]; then
    for f in frigate_bot.log debug.log; do
        [[ -f "$f" ]] && grep -q 'Payload: {' "$f" && LOG_FILE="$f" && break
    done
fi

if [[ -z "$LOG_FILE" || ! -f "$LOG_FILE" ]]; then
    echo "Не найден лог с payload'ами (искал frigate_bot.log и debug.log рядом со скриптом)" >&2
    exit 1
fi

PAYLOAD=$(grep -o 'Payload: {.*' "$LOG_FILE" | tail -1 | sed 's/^Payload: //' || true)
if [[ -z "$PAYLOAD" ]]; then
    echo "В $LOG_FILE нет строк 'Payload: {...}'" >&2
    exit 1
fi

echo "Лог:     $LOG_FILE"
echo "Payload: ${PAYLOAD:0:120}..."
docker compose exec -T mosquitto mosquitto_pub -t frigate/reviews -m "$PAYLOAD"
echo "Отправлено в frigate/reviews. Смотри: docker compose logs -f frigate_bot"
