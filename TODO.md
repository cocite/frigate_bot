# TODO: конвейер доставки с независимыми каналами

> **Статус: реализовано** (этапы A и B, 2026-09-06). Файл оставлен как описание архитектуры. Дальнейшая чистка — в CLEANUP.md.

## Проблема

Сейчас событие обрабатывается монолитно: воркер ждёт, пока отправят ВСЕ мессенджеры,
и только потом берёт следующее событие. Быстрый канал (TG_BOT) простаивает, пока медленный
(TG_MTPROTO с большим видео) доползает. С добавлением каналов эффект усиливается.

## Архитектура

```
СТАДИЯ 1 — подготовка (один воркер, как сейчас):
    событие → снапшоты + экспорт/ffmpeg + сводка genai → notification

СТАДИЯ 2 — форматирование (при постановке в очередь):
    для каждого включённого канала:
        messages = форматтер канала(notification, format_params канала)
        messages → Queue[канал]

СТАДИЯ 3 — доставка (воркер на канал, независимый темп):
    msg = Queue[канал].get() → транспорт канала(msg)   # только API и ретраи
```

Медленный канал копит хвост только в своей очереди; порядок внутри канала — FIFO
(подготовка последовательная, перемешивания событий нет).

## Notification (нейтральное уведомление, собирается один раз)

```python
notification = {
    "review_id": "...",
    "photos": [пути, хронологический порядок],
    "video": {"path", "thumb_path", "meta"} | None,   # без решений о лимитах
    "caption": "готовая строка",                       # message_blocks больше нет
}
```

## Реестр каналов

```python
CHANNELS = {
    "TG_BOT": {
        "formatter": messenger_style,
        "transport": _deliver_bot,        # существующие отправщики Bot API
        "format_params": dict(album_size=8, caption_limit=1024, message_limit=4096,
                              video_mb=49,       # лимит Bot API (~50 МБ)
                              video_in_album=True),
    },
    "TG_MTPROTO": {
        "formatter": messenger_style,
        "transport": _deliver_mtproto,    # существующие отправщики Telethon
        "format_params": dict(album_size=8, caption_limit=1024, message_limit=4096,
                              video_mb=None,     # 2 ГБ, фактически без лимита
                              video_in_album=True),
    },
}
```

- `messenger_style` — ОБЩИЙ форматтер для мессенджеров, различия только в `format_params`.
- `video_in_album` — для Telegram пока не нужно (везде True), оставлено на будущее:
  канал, где видео нельзя в альбом, получит False (бывший SEND_VIDEO_SEPARATELY).
- Подпись: на последний альбом, если влезает в caption_limit; иначе отдельным text;
  длиннее message_limit (практически нереально) — резать по предложениям.

## Сообщения в очереди

Словарь сообщений — договорённость пары «форматтер ↔ транспорт» ОДНОГО канала,
не всеобщий стандарт. Очередь — просто труба.

Мессенджерская пара:
```python
{"kind": "media_group", "items": [{"type": "photo", "path": ...},
                                  {"type": "video", "path", "thumb_path", "meta"}], "caption": ""}
{"kind": "text", "text": "..."}
```
- thumb: как поле называется в API (thumbnail у Bot API, thumb= у Telethon) — забота
  транспорта; форматтер оперирует нейтральным thumb_path.

Будущая почта — свой канал целиком:
```python
"EMAIL": {"formatter": email_style,      # тема + тело + вложения, без альбомов
          "transport": _deliver_smtp,
          "format_params": dict(attachment_mb=25)}
# сообщение: {"kind": "email", "subject", "body", "attachments": [...]}
```

## Обвязка

- Воркеры доставки стартуют в main() mqtt_dispatcher внутри tg_client.session().
- Debug-режим (python notifier.py): после send_frigate_alert — queue.join() по всем
  очередям, чтобы не выйти раньше отправки.
- Queue(maxsize=N) + warning при заполнении («канал X не успевает, в очереди N»).
- Падение одного сообщения логируется, воркер канала живёт дальше.

## Что умирает из текущего кода

- send_queue / caption_for_last_group / send_caption_separately с блочным циклом;
- SEND_VIDEO_SEPARATELY (станет format_params.video_in_album);
- BOT_MAX_VIDEO_MB как отдельная константа (уедет в format_params.video_mb);
- дублирование сборки групп в двух ветках send_telegram_media_group;
- сами send_telegram_message/send_telegram_media_group в текущем виде
  (распадаются на форматтер + транспорты).

## Порядок реализации

1. Форматтер messenger_style(notification, format_params) → messages + прогон руками.
2. Транспорты: перекроить существующие отправщики под приём готового message.
3. Очереди + delivery_worker'ы + старт в main() и debug-обвязка.
4. send_frigate_alert: собирает notification, кладёт в очереди, завершается.
5. Чистка умершего кода, обновление README (константы поменяются).
