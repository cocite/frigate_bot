# frigate_bot

> ⚠️ **Это README сгенерировано ИИ** — возможны излишние детали, а чего-то может не хватать. Нормальное README в процессе создания.

Telegram-уведомления о событиях [Frigate](https://frigate.video): снапшоты с разметкой + сжатое видео + подпись с метками, зонами и ИИ-сводкой происходящего.

Протестировано на **Frigate 0.18**.

## Как это работает

- слушает MQTT-топик `frigate/reviews`;
- когда событие завершается (`type: end`) — скачивает размеченные снапшоты детекций, заказывает у Frigate экспорт видео, сжимает его через VAAPI (или CPU, если GPU нет);
- параллельно ждёт ИИ-сводку события от Frigate (`review.genai`) и добавляет её в подпись — с маркером ⚠️/🚨 при ненулевом уровне угрозы;
- отправляет медиагруппу в Telegram через Bot API и/или MTProto — параллельно, отказ одного канала не мешает другому.

## Требования

- Docker + Compose; сервисы `frigate` и `mosquitto` в том же compose — бот обращается к ним по этим именам (`frigate:5000`, `mosquitto:1883`);
- во Frigate включены MQTT, снапшоты и записи;
- опционально: Intel iGPU (Broadwell / Core 5xxx и новее) для аппаратного кодирования. Без него бот кодирует на CPU и предупреждает об этом в логе;
- опционально: включённый `review.genai` во Frigate — для ИИ-сводок в подписи (см. ниже).

## Установка

**1. Код** — клонировать рядом с docker-compose.yaml от Frigate NVR:

```bash
git clone <url-репозитория>
```

Папка должна называться `frigate_bot` — на неё ссылаются пути в compose. Если репозиторий клонировался под другим именем, добавьте его вторым аргументом: `git clone <url> frigate_bot`.

**2. Конфиг:**

```bash
cp frigate_bot/config.example.py frigate_bot/config.py
```

Заполнить:
- `TELEGRAM_MODES` — каналы отправки. Режимов всего два, можно включить оба сразу:
  - `BOT` — обычный Telegram-бот, хватает в большинстве случаев (лимит видео ~50 МБ);
  - `MTPROTO` — отправка от юзер-аккаунта. Имеет смысл только если нужно слать большие видео (лимит 2 ГБ); требует api_id/api_hash и создания сессии (шаг 5);
- `BOT_CONFIG` — токен у [@BotFather](https://t.me/botfather), `chat_id` группы отрицательный;
- `MTPROTO_CONFIG` — `api_id`/`api_hash` с [my.telegram.org](https://my.telegram.org/apps), нужен только для режима MTPROTO.

**3. Сервис в compose** — добавить в ваш `docker-compose.yaml` (рядом с сервисами `frigate` и `mosquitto`) блок из `docker-compose.example.yaml`:

```yaml
  frigate_bot:
    container_name: frigate_bot
    build:
      context: ./frigate_bot
    restart: unless-stopped

    # Обе секции ниже (environment + devices) нужны только для аппаратного
    # кодирования видео (VAAPI) на Intel iGPU (Broadwell / Core 5xxx и новее).
    # /dev/dri — стандартный путь GPU-устройств ядра Linux, одинаков во всех дистрибутивах.
    # Если убрать — бот сам перейдёт на CPU-кодирование (libx264).
    environment:
      LIBVA_DRIVER_NAME: iHD
    devices:
      - /dev/dri:/dev/dri
    # Если VAAPI не заводится без привилегий — раскомментируй:
    # privileged: true

    volumes:
      # Код бота: живой с хоста, правки подхватываются рестартом без пересборки
      - ./frigate_bot:/app
      # Медиатека Frigate — только чтение, отсюда бот забирает готовые экспорты.
      # Путь слева должен совпадать с тем, что смонтирован в сервисе frigate.
      - ./media/frigate:/media/frigate:ro
      # Часовой пояс хоста — чтобы время в подписях совпадало с реальностью
      - /etc/localtime:/etc/localtime:ro

    depends_on:
      - mosquitto
```

Поправьте пути volumes под свою раскладку (`./media/frigate` — тот же каталог, что смонтирован в сервисе `frigate`).

**4. Запуск:**

```bash
docker compose up -d --build frigate_bot
docker compose logs -f frigate_bot
```

В логе должно появиться `Subscribed to topics: ['frigate/reviews']` и `MQTT диспетчер запущен`.

**5. Сессия MTProto** (только для режима MTPROTO):

```bash
docker compose exec -it frigate_bot python tg_client.py
```

Интерактивно спросит телефон, код и пароль 2FA. Пока сессии нет, бот работает без MTProto (остальные режимы не страдают) и подхватывает сессию автоматически, как только она появится — рестарт не нужен.

## ИИ-сводки в подписи (review.genai)

Сводку генерирует сам Frigate — бот только забирает готовую через API. Настраивается на стороне **Frigate** (не бота):

```yaml
genai:
  gemini_cloud:
    provider: gemini            # или openai / ollama / llamacpp
    api_key: "{FRIGATE_GENAI_API_KEY}"
    model: gemini-3.8-flash

review:
  genai:
    enabled: true
    preferred_language: Russian # сводки сразу на русском
    # detections: true          # суммаризировать и detections, не только alerts
```

Бот при каждом событии проверяет по `/api/config`, включена ли генерация и покрывает ли она severity события — если нет, сводку не ждёт. Ожидание идёт параллельно со всей обработкой (фото/видео) и ограничено таймаутом, алерт из-за сводки не задерживается дольше `REVIEW_GENAI_WAIT`.

Имена людей (Face Recognition) и номера машин (LPR), если они включены во Frigate, добавляются к меткам в подписи автоматически: `👤 ЧЕЛОВЕЧЕ [Юра]`, `🚗 МАШИНА [А123ВС77]`. Это не связано с review.genai и работает всегда.

## Логи

| Файл | Что это |
|---|---|
| `frigate_bot.log` | сервис (ротация 5 МБ × 3) |
| `debug.log` | ручные запуски: отладка и создание сессии |

## Отладка

Обработчик события можно запустить напрямую, без MQTT — payload берётся из лога (строки `Payload: {...}`):

```bash
docker compose exec -it frigate_bot python tg_alert.py '<json payload>'
```

Использует отдельную Telethon-сессию `*_debug` (создаётся так же: `python tg_client.py --debug`), поэтому не конфликтует с работающим сервисом.

Либо отправить payload обратно в MQTT — сервис обработает его как настоящее событие:

```bash
docker compose exec -T mosquitto mosquitto_pub -t frigate/reviews -m '<json payload>'
```

## Настройки (config.py)

| Параметр | По умолчанию | Что делает |
|---|---|---|
| `GENAI_REVIEW_SHOW` | `True` | добавлять в подпись ИИ-сводку review.genai из Frigate |
| `GENAI_REVIEW_WAIT` | `25` | сколько ждать генерацию сводки с начала обработки, сек |
| `GENAI_REVIEW_POLL` | `2.0` | интервал опроса готовности сводки, сек |
| `FRIGATE_PUBLIC_URL` | — | URL веб-интерфейса Frigate (`http://192.168.1.10:5000` или `https://frigate.example.com`) — внизу сообщения будет ссылка на review; `""` — без ссылки |
| `EXPORT_START_SHIFT` / `EXPORT_END_SHIFT` | `5` / `5` | запас видео до/после события, сек |
| `EXPORT_MAX_LEN` | `180` | максимум длины ролика, сек; подобрано опытным путём — при текущих параметрах кодирования итоговый файл укладывается в лимит Bot API (~50 МБ) |
| `OUTPUT_WIDTH` / `OUTPUT_FPS` / `OUTPUT_QP` | `1024` / `25` / `26` | параметры кодирования |
