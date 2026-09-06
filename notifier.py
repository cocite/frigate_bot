import requests
import json
import sys
import os
import subprocess
import logging
import time
import asyncio
from telethon.tl.types import InputMediaUploadedPhoto, InputMediaUploadedDocument, DocumentAttributeVideo, DocumentAttributeFilename
import log_config
import tg_client
from config import (ENABLED_CHANNELS, TG_BOT_CONFIG, TG_MTPROTO_CONFIG, FRIGATE_PUBLIC_URL,
                    GENAI_REVIEW_SHOW, GENAI_REVIEW_WAIT, GENAI_REVIEW_POLL,
                    EXPORT_START_SHIFT, EXPORT_END_SHIFT, EXPORT_MAX_LEN,
                    OUTPUT_WIDTH, OUTPUT_FPS, OUTPUT_QP)

# --- 1. КОНСТАНТЫ (пользовательские настройки — в config.py) ---
# Директории
CLIP_DIR    = "/app/data/media"
CLEANUP_INTERVAL = 3600           # период уборки старых медиа из CLIP_DIR, сек

# Отправка в Telegram (параметры форматирования каналов — в CHANNELS, секция 6)
BOT_API_TIMEOUT = 300             # сокет-таймаут Bot API (connect/write/read), сек
DELIVERY_QUEUE_SIZE = 30          # сообщений в очереди доставки каждого канала

# Frigate API
FRIGATE_URL = "http://frigate:5000"
FRIGATE_TIMEOUT = (5, 15)          # (подключение, чтение)
FRIGATE_CONFIG_TTL = 300           # кэш конфига Frigate (проверка, что genai включён), сек
EXPORT_WAIT_TIMEOUT = 60
EXPORT_POLL_FIRST = 0.5           # первый опрос статуса
EXPORT_POLL_MAX = 3.0             # потолок интервала
EXPORT_POLL_FACTOR = 1.6
# 0.18: размеченные снапшоты только через API, файлов .jpg на диске больше нет
SNAPSHOT_PARAMS = {"bounding_box": 1, "timestamp": 1, "crop": 1, "quality": 80}

# Подписи детекций
LABEL_DICT = {
    "person": {"emoji": "👤", "name": "ЧЕЛОВЕЧЕ"},
    "cat": {"emoji": "🐈", "name": "КОШЕН"},
    "dog": {"emoji": "🐕", "name": "СОБАКЕН"},
    "bird": {"emoji": "🐦", "name": "ПТИЦА"},
    "car": {"emoji": "🚗", "name": "МАШИНА"},
    "face": {"emoji": "🧔‍♀️", "name": "ФЭЙС"},
    "fox": {"emoji": "🦊", "name": "ЛИС"},
    "bicycle": {"emoji": "🚲", "name": "велосипед"},
    "motorcycle": {"emoji": "🏍️", "name": "мотоцикл"},
    "bus": {"emoji": "🚌", "name": "автобус"},
    "truck": {"emoji": "🚚", "name": "грузовик"}
}

# --- 2. ЛОГГЕР ---
# Хендлеры настраивает entry point через log_config.setup()
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# --- 3. КЛИЕНТ TELETHON ---
# Клиентом владеет tg_client.py: entry point открывает 'async with tg_client.session()',
# здесь клиент берётся через 'await tg_client.ensure()' — при недоступной сессии
# попытка подключения повторяется на каждой отправке.

# --- 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_time_from_id(detection_id): return float(detection_id.split('-')[0])

def format_time(ts): return time.strftime('%H:%M:%S', time.localtime(ts))

async def cleanup_worker():
    """Периодическая уборка CLIP_DIR: удаляет медиа старше суток.
    Запускается entry point'ом сервиса; раньше жила в горячем пути каждого события."""
    os.makedirs(CLIP_DIR, exist_ok=True)   # каталог нужен до первого find
    while True:
        await asyncio.to_thread(subprocess.run, ["find", CLIP_DIR, "-type", "f", "-mmin", "+1440", "-delete"])
        await asyncio.to_thread(subprocess.run, ["find", CLIP_DIR, "-type", "d", "-empty", "-delete"])
        await asyncio.sleep(CLEANUP_INTERVAL)


def _fetch_snapshot(detection_id, dest_path):
    """Скачивает размеченный снапшот события через API Frigate."""
    try:
        r = requests.get(f"{FRIGATE_URL}/api/events/{detection_id}/snapshot.jpg",
                         params=SNAPSHOT_PARAMS, timeout=FRIGATE_TIMEOUT)
        if r.status_code != 200 or not r.content:
            logger.info("Снапшот %s недоступен: HTTP %s", detection_id, r.status_code)
            return False
        with open(dest_path, "wb") as f:
            f.write(r.content)
        return True
    except requests.RequestException as e:
        logger.error("Ошибка загрузки снапшота %s: %s", detection_id, e)
        return False


def _fetch_detection(detection_id):
    """Детали события из Frigate. Возвращает dict или None."""
    try:
        r = requests.get(f"{FRIGATE_URL}/api/events/{detection_id}", timeout=FRIGATE_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.error("Не удалось получить событие %s: %s", detection_id, e)
        return None


def _fetch_review_metadata(review_id):
    """Сводка review.genai из Frigate. Возвращает dict (data.metadata) или None.
    Параметр '_' обходит кеш nginx — как в _export_record."""
    try:
        r = requests.get(f"{FRIGATE_URL}/api/review/{review_id}",
                         params={"_": int(time.time() * 1000)},
                         timeout=FRIGATE_TIMEOUT)
        if r.status_code != 200:
            logger.info("Сводка review %s недоступна: HTTP %s", review_id, r.status_code)
            return None
        return (r.json() or {}).get("data", {}).get("metadata")
    except (requests.RequestException, ValueError) as e:
        logger.error("Ошибка запроса сводки review %s: %s", review_id, e)
        return None


_frigate_config_cache = {"data": None, "ts": 0.0}


async def _get_review_summary(camera, severity, review_id):
    """Фоновая задача: проверяет по конфигу Frigate, что сводка вообще будет
    (review.genai включён и суммаризирует данный severity), и ждёт её до
    GENAI_REVIEW_WAIT сек. Стартует в самом начале обработки события,
    поэтому лог времени — полное время генерации со стороны Frigate."""
    if time.time() - _frigate_config_cache["ts"] > FRIGATE_CONFIG_TTL:
        try:
            r = await asyncio.to_thread(
                requests.get, f"{FRIGATE_URL}/api/config", timeout=FRIGATE_TIMEOUT)
            r.raise_for_status()
            _frigate_config_cache["data"] = r.json()
            _frigate_config_cache["ts"] = time.time()
        except (requests.RequestException, ValueError) as e:
            logger.error("Не удалось получить конфиг Frigate: %s", e)

    cfg = _frigate_config_cache["data"]
    if cfg is not None:
        genai_cfg = (cfg.get("cameras", {}).get(camera, {}).get("review", {}).get("genai")
                     or cfg.get("review", {}).get("genai") or {})
        if not genai_cfg.get("enabled"):
            logger.info("review.genai выключен во Frigate — сводку не ждём.")
            return None
        if not (genai_cfg.get("alerts", True) if severity == "alert"
                else genai_cfg.get("detections", False)):
            logger.info("review.genai не суммаризирует severity='%s' — сводку не ждём.", severity)
            return None
        logger.info("review.genai включён (alerts=%s, detections=%s) — ждём сводку для severity='%s'.",
                    genai_cfg.get("alerts", True), genai_cfg.get("detections", False), severity)
    # конфиг получить не удалось — ждём как обычно, хуже не станет

    started = time.time()
    deadline = started + GENAI_REVIEW_WAIT
    while True:
        meta = await asyncio.to_thread(_fetch_review_metadata, review_id)
        if meta:
            logger.info("Сводка review.genai готова через %.1f c: %s",
                        time.time() - started, json.dumps(meta, ensure_ascii=False))
            return meta
        if time.time() >= deadline:
            logger.info("Сводка review.genai для %s не появилась за %d c.",
                        review_id, GENAI_REVIEW_WAIT)
            return None
        await asyncio.sleep(GENAI_REVIEW_POLL)


def _request_export(camera, start_int, end_int):
    """Просит Frigate собрать ролик. Возвращает export_id или None."""
    try:
        r = requests.post(f"{FRIGATE_URL}/api/export/{camera}/start/{start_int}/end/{end_int}",
                          json={"source": "recordings", "name": f"tg_{camera}_{start_int}"},
                          timeout=FRIGATE_TIMEOUT)
    except requests.RequestException as e:
        logger.error("Не удалось запросить экспорт: %s", e)
        return None
    if r.status_code not in (200, 202):
        logger.error("Экспорт не создан: HTTP %s %s", r.status_code, r.text[:200])
        return None
    export_id = (r.json() or {}).get("export_id")
    logger.info("Экспорт поставлен в очередь: %s", export_id)
    return export_id


def _export_record(export_id):
    """Запись об экспорте (in_progress, video_path) или None, если её ещё нет / не получена.
    Параметр '_' обходит кеш nginx — он подтверждённо кеширует /api/ на несколько секунд."""
    try:
        r = requests.get(f"{FRIGATE_URL}/api/exports/{export_id}",
                         params={"_": int(time.time() * 1000)},
                         timeout=FRIGATE_TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


async def _wait_export(export_id):
    """Ждёт готовности экспорта. Возвращает путь к файлу или None."""
    started, deadline = time.time(), time.time() + EXPORT_WAIT_TIMEOUT
    interval = EXPORT_POLL_FIRST
    while time.time() < deadline:
        rec = await asyncio.to_thread(_export_record, export_id)

        if rec and not rec.get("in_progress", True):
            path = rec.get("video_path")
            if path and os.path.exists(path):
                logger.info("Экспорт %s готов за %.1f c (%.1f МБ)",
                            export_id, time.time() - started, os.path.getsize(path) / 1048576)
                return path
            logger.error("Экспорт %s завершён, но файла нет: %s", export_id, path)
            return None

        await asyncio.sleep(interval)
        interval = min(interval * EXPORT_POLL_FACTOR, EXPORT_POLL_MAX)

    logger.error("Экспорт %s не готов за %d c", export_id, EXPORT_WAIT_TIMEOUT)
    return None


def _delete_export(export_id):
    """Убирает экспорт: /media/frigate смонтирован только на чтение, удалять можно лишь через API."""
    try:
        r = requests.post(f"{FRIGATE_URL}/api/exports/delete", json={"ids": [export_id]},
                          timeout=FRIGATE_TIMEOUT)
        if r.status_code == 200:
            logger.info("Экспорт %s удалён", export_id)
        else:
            logger.error("Экспорт %s не удалён: HTTP %s %s", export_id, r.status_code, r.text[:150])
    except requests.RequestException as e:
        logger.error("Ошибка удаления экспорта %s: %s", export_id, e)


def _find_vaapi_device():
    """Ищет render-узел Intel (vendor 0x8086) в /dev/dri. Возвращает путь или None."""
    try:
        dri_path = "/dev/dri"
        if os.path.isdir(dri_path):
            for node in sorted(os.listdir(dri_path)):
                if node.startswith("renderD"):
                    vendor_path = f"/sys/class/drm/{node}/device/vendor"
                    if os.path.exists(vendor_path):
                        with open(vendor_path) as f:
                            if f.read().strip() == "0x8086":
                                return os.path.join(dri_path, node)
    except Exception as e:
        logger.error(f"Ошибка при поиске устройства VAAPI: {e}")
    return None


async def _prepare_video(export_path, review_dir):
    """Сжимает экспорт (VAAPI или CPU), делает превью и метаданные.
    Возвращает media item для отправки или None."""
    compressed_path = os.path.join(review_dir, "final.mp4")

    vaapi_device = _find_vaapi_device()
    if vaapi_device:
        logger.info(f"Intel VAAPI: {vaapi_device}")
    else:
        logger.warning("Intel VAAPI не найден — видео будет кодироваться на CPU (libx264): медленнее и грузит процессор.")

    # Аппаратная часть: флаги входа, фильтр масштабирования, кодек с его настройкой качества.
    if vaapi_device:
        hw_input = ["-hwaccel", "vaapi", "-hwaccel_device", vaapi_device, "-hwaccel_output_format", "vaapi"]
        scale    = f"scale_vaapi=w={OUTPUT_WIDTH}:h=-2"
        encoder  = ["-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", str(OUTPUT_QP)]
    else:
        hw_input = []
        scale    = f"scale={OUTPUT_WIDTH}:-2"
        encoder  = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(OUTPUT_QP)]

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        *hw_input,
        "-i", export_path,
        "-vf", f"fps={OUTPUT_FPS},{scale}",
        "-map", "0:v:0",
        "-map", "0:a:0?",          # '?' — дорожки может не быть, это не ошибка
        *encoder,
        "-g", str(OUTPUT_FPS), "-bf", "2",
        "-c:a", "aac", "-b:a", "64k", "-ar", "16000", "-ac", "1",
        "-movflags", "+faststart",
        compressed_path
    ]

    try:
        await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg завершился с ошибкой. Код: {e.returncode}\nstderr:\n{e.stderr}")
        return None
    except Exception:
        logger.exception("Критическая ошибка на этапе обработки видеофайла.")
        return None

    if not (os.path.exists(compressed_path) and os.path.getsize(compressed_path) > 1024):
        logger.error("FFmpeg отработал, но итоговый файл отсутствует или подозрительно мал.")
        return None

    size_mb = os.path.getsize(compressed_path) / (1024 * 1024)
    logger.info("Видео сжато: %s (%.2f МБ)", compressed_path, size_mb)

    thumb_path = os.path.join(review_dir, "thumb.jpg")
    try:
        thumb_cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", "00:00:01.00",
            "-i", compressed_path,
            "-frames:v", "1",
            # Bot API: не больше 320 px по длинной стороне и 200 КБ
            "-vf", "scale=320:-2",
            "-q:v", "5",
            thumb_path
        ]
        await asyncio.to_thread(subprocess.run, thumb_cmd, check=True, capture_output=True, text=True)
    except Exception as e:
        logger.error("Не удалось извлечь превью: %s", getattr(e, "stderr", e))
        thumb_path = None

    video_meta = {}
    try:
        ffprobe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "format=duration:stream=width,height,duration",
            "-of", "json",
            compressed_path
        ]
        result = await asyncio.to_thread(subprocess.run, ffprobe_cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        meta = (data.get("streams") or [{}])[0]
        dur = meta.get("duration") or data.get("format", {}).get("duration")
        video_meta = {'width': int(meta['width']), 'height': int(meta['height']),
                      'duration': int(float(dur))}
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError,
            IndexError, TypeError, ValueError) as e:
        logger.error(f"Не удалось получить метаданные видео: {e}")

    return {'type': 'video', 'path': compressed_path, 'thumb_path': thumb_path, 'meta': video_meta}


# --- 5. ФОРМАТТЕР ---
# Превращает нейтральное уведомление в список сообщений канала.
# Словарь сообщений — контракт пары «форматтер ↔ транспорт» одного канала.

def _split_text(text, limit):
    """Режет текст под лимит: по границе предложения, потом строки, потом слова."""
    chunks = []
    while len(text) > limit:
        cut = max(text.rfind(". ", 0, limit), text.rfind("\n", 0, limit))
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut + 1].strip())
        text = text[cut + 1:].strip()
    if text:
        chunks.append(text)
    return chunks


def messenger_style(notification, p):
    """Общий форматтер мессенджеров: медиа альбомами, подпись к последнему альбому,
    не влезла — отдельным сообщением. p — format_params канала."""
    media = [{'type': 'photo', 'path': path} for path in notification['photos']]
    separate_videos = []

    video = notification.get('video')
    if video:
        size_mb = os.path.getsize(video['path']) / 1048576
        if p['video_mb'] is not None and size_mb > p['video_mb']:
            logger.warning("Видео %.1f МБ превышает лимит канала (%d МБ) — уведомление уйдёт без видео.",
                           size_mb, p['video_mb'])
        elif p['video_in_album']:
            media.append(video)
        else:
            separate_videos.append(video)

    messages = [{'kind': 'media_group', 'items': media[i:i + p['album_size']], 'caption': ''}
                for i in range(0, len(media), p['album_size'])]
    messages += [{'kind': 'media_group', 'items': [v], 'caption': ''} for v in separate_videos]

    caption = notification.get('caption', '')
    if caption:
        if messages and len(caption) <= p['caption_limit']:
            messages[-1]['caption'] = caption
        else:
            messages += [{'kind': 'text', 'text': chunk}
                         for chunk in _split_text(caption, p['message_limit'])]
    return messages


# --- 6. ТРАНСПОРТЫ И РЕЕСТР КАНАЛОВ ---

async def _mtproto_send_media_group(client, chat_id, media_objects, caption=""):
    """(MTProto) Отправляет альбом из уже загруженных медиа и логирует ответ сервера."""
    logger.info(f"[MTProto] Отправка медиагруппы из {len(media_objects)} объектов...")
    sent_messages = await asyncio.wait_for(
        client.send_file(chat_id, file=media_objects, caption=caption),
        timeout=180   # файлы уже загружены — это только сборка альбома
    )

    if not sent_messages:
        logger.error("[MTProto] Сервер не вернул информацию об отправленных сообщениях!")
        return
    if not isinstance(sent_messages, list):
        sent_messages = [sent_messages]

    logger.info(f"[MTProto] Отправлено {len(sent_messages)} сообщений (ids: {[m.id for m in sent_messages]}).")
    for msg in sent_messages:
        info = f"id={msg.id} group={msg.grouped_id} media={type(msg.media).__name__ if msg.media else '-'}"
        attrs = getattr(getattr(msg.media, 'document', None), 'attributes', None) or []
        video = next((a for a in attrs if type(a).__name__ == 'DocumentAttributeVideo'), None)
        if video:
            info += f" video={video.w}x{video.h} {video.duration}с"
        logger.debug(f"[MTProto]   {info}")

def _send_bot_api_request(method, data, file_paths=None, max_retries=5):
    """(Bot API) Отправляет запрос. Возвращает успешный response или кидает RuntimeError.
    Повторяет попытки при rate limit (429), ошибках Telegram (5xx) и сетевых сбоях."""
    url = f"https://api.telegram.org/bot{TG_BOT_CONFIG['token']}/{method}"
    for _ in range(max_retries):
        files = {name: open(path, 'rb') for name, path in file_paths.items()} if file_paths else None
        try:
            # Один щедрый таймаут на всё (connect/write/read): при параллельной отправке
            # в несколько каналов аплинк насыщается, и write легально блокируется надолго —
            # раздельный короткий connect-таймаут душил именно отправку тела запроса.
            # Щедрость важна и против дублей: ретрай после таймаута может повторить
            # уже принятую Telegram'ом группу.
            response = requests.post(url, data=data, files=files, timeout=BOT_API_TIMEOUT)
            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                logger.warning(f"[BotAPI] Rate limit. Повтор через {retry_after} сек.")
                time.sleep(retry_after + 1)
                continue
            if response.status_code >= 500:
                logger.warning(f"[BotAPI] HTTP {response.status_code} от Telegram. Повтор через 5 сек.")
                time.sleep(5)
                continue
            if response.status_code != 200:
                # 4xx — постоянная ошибка, повторять бессмысленно
                raise RuntimeError(f"[BotAPI] {method}: HTTP {response.status_code}, {response.text[:300]}"
                                   f" (files: {list(file_paths) if file_paths else '-'})")
            return response
        except requests.RequestException as e:
            logger.error(f"[BotAPI] Сетевая ошибка: {e}")
            time.sleep(5)
        finally:
            if files:
                for f in files.values(): f.close()
    raise RuntimeError(f"[BotAPI] Запрос '{method}' провалился после {max_retries} попыток.")

def _bot_media_fields(item, data_or_payload, files):
    """(Bot API) Заполняет поля видео (размеры, длительность, превью) и регистрирует файлы."""
    attach_name = os.path.basename(item['path'])
    files[attach_name] = item['path']
    if item['type'] == 'video':
        meta = item.get('meta', {})
        data_or_payload.update({'width': meta.get('width'), 'height': meta.get('height'),
                                'duration': meta.get('duration'), 'supports_streaming': True})
        if item.get('thumb_path'):
            thumb_name = os.path.basename(item['thumb_path'])
            files[thumb_name] = item['thumb_path']
            data_or_payload['thumbnail'] = f"attach://{thumb_name}"
    return attach_name


async def _deliver_bot(msg):
    """(Bot API) Исполняет одно сообщение канала."""
    if msg['kind'] == 'text':
        await asyncio.to_thread(_send_bot_api_request, "sendMessage",
                                data={"chat_id": TG_BOT_CONFIG['chat_id'], "text": msg['text']})
        return

    items = msg['items']
    if len(items) == 1:
        # sendMediaGroup требует 2-10 элементов — одиночное медиа шлём своим методом
        item = items[0]
        field = "photo" if item['type'] == 'photo' else "video"
        data, files = {"chat_id": TG_BOT_CONFIG['chat_id']}, {}
        attach_name = _bot_media_fields(item, data, files)
        data[field] = f"attach://{attach_name}"
        if msg['caption']:
            data["caption"] = msg['caption']
        method = "sendPhoto" if field == "photo" else "sendVideo"
        await asyncio.to_thread(_send_bot_api_request, method, data=data, file_paths=files)
        return

    media_payload, files_to_attach = [], {}
    for item in items:
        payload_item = {'type': item['type']}
        attach_name = _bot_media_fields(item, payload_item, files_to_attach)
        payload_item['media'] = f'attach://{attach_name}'
        media_payload.append(payload_item)
    if msg['caption']:
        media_payload[-1]['caption'] = msg['caption']
    data = {"chat_id": TG_BOT_CONFIG['chat_id'], "media": json.dumps(media_payload)}
    await asyncio.to_thread(_send_bot_api_request, "sendMediaGroup", data=data, file_paths=files_to_attach)


async def _deliver_mtproto(msg):
    """(MTProto) Исполняет одно сообщение канала."""
    client = await tg_client.ensure()

    if msg['kind'] == 'text':
        await asyncio.wait_for(client.send_message(TG_MTPROTO_CONFIG['chat_id'], msg['text']), timeout=30)
        return

    # Загрузку не ограничиваем: медленная сеть — не сбой; мёртвую сеть
    # Telethon добьёт сам (reconnect x10 → исключение)
    items = msg['items']
    total_mb = sum(os.path.getsize(i['path']) for i in items) / 1048576
    logger.info(f"[MTProto] Загрузка {len(items)} файлов ({total_mb:.1f} МБ)...")
    media_objects = []
    for item in items:
        if item['type'] == 'photo':
            handle = await client.upload_file(item['path'])
            media_objects.append(InputMediaUploadedPhoto(file=handle))
        elif item['type'] == 'video':
            thumb_handle = await client.upload_file(item['thumb_path']) if item.get('thumb_path') else None
            video_handle = await client.upload_file(item['path'])
            meta = item.get('meta', {})
            attributes = [
                DocumentAttributeVideo(duration=meta.get('duration'), w=meta.get('width'), h=meta.get('height'), supports_streaming=True),
                DocumentAttributeFilename(file_name=os.path.basename(item['path']))]
            media_objects.append(InputMediaUploadedDocument(file=video_handle, thumb=thumb_handle, attributes=attributes, mime_type='video/mp4'))
    await _mtproto_send_media_group(client, TG_MTPROTO_CONFIG['chat_id'], media_objects, msg['caption'])


CHANNELS = {
    'TG_BOT': {
        'formatter': messenger_style,
        'transport': _deliver_bot,
        'format_params': dict(album_size=8, caption_limit=1024, message_limit=4096,
                              video_mb=49,    # лимит Bot API на загрузку (~50 МБ)
                              video_in_album=True),
    },
    'TG_MTPROTO': {
        'formatter': messenger_style,
        'transport': _deliver_mtproto,
        'lifecycle': tg_client.session,   # канал сам владеет своей MTProto-сессией

        'format_params': dict(album_size=8, caption_limit=1024, message_limit=4096,
                              video_mb=None,  # 2 ГБ, фактически без лимита
                              video_in_album=True),
    },
}


# Очереди доставки: у каждого включённого канала своя, каналы разгребают их
# в своём темпе — медленный не тормозит быстрых (см. delivery_queues_analysis.md)
_delivery_queues = {}


async def _delivery_loop(mode):
    """Вечный цикл воркера: взял (review_id, msg) из очереди — отдал транспорту.
    Ошибка сообщения логируется и не убивает воркера."""
    q = _delivery_queues[mode]
    transport = CHANNELS[mode]['transport']
    logger.info(f"[{mode}] воркер доставки запущен.")
    while True:
        review_id, msg = await q.get()
        try:
            await transport(msg)
            logger.info(f"<- [{mode}] {review_id}: {msg['kind']} доставлено (в очереди {q.qsize()}).")
        except tg_client.NotAuthorized as e:
            logger.error(f"<!> [{mode}] {review_id}: {e}")
        except Exception:
            logger.exception(f"<!> [{mode}] {review_id}: ПРОВАЛ {msg['kind']}.")
        finally:
            q.task_done()


async def _delivery_worker(mode, debug=False):
    """Воркер канала. Если у канала есть lifecycle (сессия MTProto) —
    владеет им: открывает на время своей жизни."""
    lifecycle = CHANNELS[mode].get('lifecycle')
    if lifecycle:
        async with lifecycle(debug):
            await _delivery_loop(mode)
    else:
        await _delivery_loop(mode)


def start_delivery_workers(debug=False):
    """Создаёт очереди и воркеров доставки для включённых каналов.
    Зовётся entry point'ом; возвращённые task'и держать — иначе соберёт GC."""
    tasks = []
    for mode in ENABLED_CHANNELS:
        if mode not in CHANNELS:
            logger.error(f"Неизвестный канал '{mode}' в ENABLED_CHANNELS — пропущен.")
            continue
        _delivery_queues[mode] = asyncio.Queue(maxsize=DELIVERY_QUEUE_SIZE)
        tasks.append(asyncio.create_task(_delivery_worker(mode, debug), name=f"delivery-{mode}"))
    return tasks


async def flush_delivery_queues():
    """Дождаться, пока воркеры разгребут все очереди (debug-режим)."""
    await asyncio.gather(*(q.join() for q in _delivery_queues.values()))


async def dispatch_notification(notification):
    """Форматирует уведомление под включённые каналы и раскладывает готовые
    сообщения по их очередям. Дальше каждый канал доставляет в своём темпе."""
    rid = notification['review_id']
    if not _delivery_queues:
        logger.error(f"{rid}: воркеры доставки не запущены (start_delivery_workers) — уведомление потеряно.")
        return
    for mode, q in _delivery_queues.items():
        ch = CHANNELS[mode]
        try:
            messages = ch['formatter'](notification, ch['format_params'])
        except Exception:
            logger.exception(f"<!> [{mode}] {rid}: ошибка форматтера — событие пропущено.")
            continue
        if q.full():
            logger.warning(f"[{mode}] очередь заполнена ({q.qsize()}) — канал не успевает, ждём место.")
        for msg in messages:
            await q.put((rid, msg))
        logger.info(f"-> [{mode}] {rid}: {len(messages)} сообщений в очередь (в очереди {q.qsize()}).")

# --- 7. ГЛАВНАЯ ФУНКЦИЯ ---

def _build_caption(raw_events, start_time, end_time, camera, review_id, review_metadata):
    """Собирает текст уведомления: время, метки с именами/номерами, зоны,
    сводка review.genai, ссылка на review. Чистая функция без I/O."""
    pretty_labels, zones = set(), set()
    for raw in raw_events:
        zones.update(raw.get("zones", []))
        label_info = LABEL_DICT.get(raw.get("label", "UFO"), {"emoji": "❓", "name": "НЕЧТО"})
        extra = set(filter(None, [raw.get('sub_label'), raw.get('data', {}).get('recognized_license_plate')]))
        extra_display = f" [{' | '.join(sorted(extra))}]" if extra else ""
        pretty_labels.add(f"{label_info['emoji']} {label_info['name']}{extra_display}")

    zone_display = f" в зонах {', '.join(sorted(zones))}" if zones else ""
    caption = (
        f"🕒 {format_time(start_time)} – {format_time(end_time)}\n"
        f"{', '.join(sorted(pretty_labels))} at {camera}{zone_display}"
    )

    if review_metadata and review_metadata.get("shortSummary"):
        threat_mark = {1: "⚠️ ", 2: "🚨 "}.get(review_metadata.get("potential_threat_level") or 0, "")
        caption += f"\n{threat_mark}📝 {review_metadata['shortSummary']}"

    if FRIGATE_PUBLIC_URL:
        caption += f"\n\n🔗 {FRIGATE_PUBLIC_URL.rstrip('/')}/review?id={review_id}"
    return caption


async def send_frigate_alert(payload_json: str):
    payload = json.loads(payload_json)

    if payload["type"] != "end":
        logger.debug(f"Skipped event type: {payload['type']}")
        return

    logger.info(f"Payload: {payload_json}")

    detections = payload["after"]["data"]["detections"]
    detection_times = sorted(detections, key=get_time_from_id)

    start_time = payload["after"]["start_time"]
    end_time = payload["after"]["end_time"]
    camera = payload["after"]["camera"]
    review_id = payload["after"]["id"]
    severity = payload["after"].get("severity", "alert")
    logger.info(f"Новый review: {review_id} (severity: {severity})")

    review_dir = f"{CLIP_DIR}/{review_id}"
    os.makedirs(review_dir, exist_ok=True)

    # Сводку review.genai ждём параллельно всей обработке:
    # Frigate генерирует её после конца активности, видео-пайплайн даёт ей фору
    summary_task = (asyncio.create_task(_get_review_summary(camera, severity, review_id))
                    if GENAI_REVIEW_SHOW else None)

    start_int = int(start_time) - EXPORT_START_SHIFT
    end_int = int(end_time) + EXPORT_END_SHIFT
    if end_int - start_int > EXPORT_MAX_LEN:
        start_int = end_int - EXPORT_MAX_LEN

    # Экспорт запрашиваем сразу: Frigate собирает ролик, пока мы качаем снапшоты
    export_id = await asyncio.to_thread(_request_export, camera, start_int, end_int)

    photos, video = [], None
    try:
        snapshots = [(d, os.path.join(review_dir, f"{d}.jpg")) for d in detection_times]
        fetched = await asyncio.gather(*(asyncio.to_thread(_fetch_snapshot, d, p) for d, p in snapshots))
        photos = [p for (d, p), ok in zip(snapshots, fetched) if ok]
        logger.info("Снапшоты: скачано %d из %d", len(photos), len(detection_times))

        export_path = await _wait_export(export_id) if export_id else None
        if export_path:
            video = await _prepare_video(export_path, review_dir)   # dict или None
    finally:
        # Экспорт удаляется всегда, даже если обработка упала
        if export_id:
            await asyncio.to_thread(_delete_export, export_id)

    raw_events = [r for r in await asyncio.gather(
        *(asyncio.to_thread(_fetch_detection, d) for d in detection_times)) if r]

    # Сводка review.genai: задача крутилась параллельно с самого начала.
    # Её сбой не должен стоить алерта — сводка необязательна
    review_metadata = None
    if summary_task:
        try:
            review_metadata = await summary_task
        except Exception:
            logger.exception(f"{review_id}: сбой задачи сводки review.genai — уведомление уйдёт без неё.")

    notification = {
        'review_id': review_id,
        'photos': photos,
        'video': video,
        'caption': _build_caption(raw_events, start_time, end_time, camera, review_id, review_metadata),
    }
    await dispatch_notification(notification)

# --- БЛОК ДЛЯ ЗАПУСКА ---
if __name__ == "__main__":
    log_config.setup("debug.log")
    if len(sys.argv) < 2:
        print("Ошибка: Необходимо передать JSON payload.", file=sys.stderr)
        sys.exit(1)

    payload_from_cli = sys.argv[1]

    async def debug_run():
        logger.info("Запуск в режиме отладки (сессия *_debug)...")
        workers = start_delivery_workers(debug=True)
        try:
            await send_frigate_alert(payload_from_cli)
            await flush_delivery_queues()   # дождаться, пока каналы всё отправят
        finally:
            for t in workers:
                t.cancel()
            # даём воркерам корректно закрыться (session() отключает клиента в finally)
            await asyncio.gather(*workers, return_exceptions=True)
        logger.info("Отладочный прогон завершён.")

    try:
        asyncio.run(debug_run())
    except Exception:
        logger.exception("Ошибка во время выполнения send_frigate_alert:")
        sys.exit(1)
