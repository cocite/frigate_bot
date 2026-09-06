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
from config import (TELEGRAM_MODES, BOT_CONFIG, MTPROTO_CONFIG, FRIGATE_PUBLIC_URL,
                    GENAI_REVIEW_SHOW, GENAI_REVIEW_WAIT, GENAI_REVIEW_POLL,
                    EXPORT_START_SHIFT, EXPORT_END_SHIFT, EXPORT_MAX_LEN,
                    OUTPUT_WIDTH, OUTPUT_FPS, OUTPUT_QP)

# --- 1. КОНСТАНТЫ (пользовательские настройки — в config.py) ---
# Директории
CLIP_DIR    = "/app/data/media"

# Отправка в Telegram
SEND_VIDEO_SEPARATELY = False     # отладочное: слать видео отдельной группой от фото
MAX_MEDIA_PER_GROUP = 8
MAX_CAPTION_LENGTH = 1024
MAX_MESSAGE_LENGTH = 4096
BOT_MAX_VIDEO_MB = 49             # лимит Bot API на загрузку — 50 МБ, минус запас на служебные данные
BOT_API_TIMEOUT = 300             # сокет-таймаут Bot API (connect/write/read), сек

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
os.makedirs(CLIP_DIR, exist_ok=True)

def get_time_from_id(detection_id): return float(detection_id.split('-')[0])

def format_time(ts): return time.strftime('%H:%M:%S', time.localtime(ts))

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
    """Персистентная запись об экспорте: in_progress и video_path.
    Параметр '_' обходит кеш nginx — он подтверждённо кеширует /api/ на несколько секунд.
    Возвращает None, если записи ещё нет (404), {} если ответ не получен."""
    try:
        r = requests.get(f"{FRIGATE_URL}/api/exports/{export_id}",
                         params={"_": int(time.time() * 1000)},
                         timeout=FRIGATE_TIMEOUT)
        if r.status_code == 404:
            return None
        return (r.json() or {}) if r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return {}


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


async def _prepare_video(export_path, review_dir, vaapi_device):
    """Сжимает экспорт (VAAPI или CPU), делает превью и метаданные.
    Возвращает media item для отправки или None."""
    compressed_path = os.path.join(review_dir, "final.mp4")

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


# --- 5. НИЗКОУРОВНЕВЫЕ ФУНКЦИИ-ОТПРАВЩИКИ ---

async def _mtproto_send_message(chat_id, text):
    """(MTProto) Отправляет текстовое сообщение."""
    client = await tg_client.ensure()
    await asyncio.wait_for(client.send_message(chat_id, text), timeout=30)

async def _mtproto_send_media_group(chat_id, media_objects, caption=""):
    """(MTProto) Отправляет группу медиа-объектов и детально логирует ответ от сервера."""
    if not media_objects:
        logger.warning("[MTProto] Попытка отправить пустую медиагруппу.")
        return
    
    logger.info(f"[MTProto] Отправка медиагруппы из {len(media_objects)} объектов...")
    client = await tg_client.ensure()
    sent_messages = await asyncio.wait_for(
        client.send_file(chat_id, file=media_objects, caption=caption),
        timeout=180   # видео грузится дольше — потолок щедрее
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
    url = f"https://api.telegram.org/bot{BOT_CONFIG['token']}/{method}"
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

# --- 6. УНИВЕРСАЛЬНЫЕ ФУНКЦИИ-ДИСПЕТЧЕРЫ (ОТКАЗОУСТОЙЧИВЫЕ) ---

async def send_telegram_message(text):
    """Главный диспетчер: отправляет текст через все включенные режимы параллельно;
    ошибка одного режима не мешает остальным."""
    async def _send_via(mode):
        try:
            logger.info(f"-> [Диспетчер] Попытка отправки текста через {mode}...")
            if mode == 'BOT':
                await asyncio.to_thread(_send_bot_api_request, "sendMessage", data={"chat_id": BOT_CONFIG['chat_id'], "text": text})
            elif mode == 'MTPROTO':
                await _mtproto_send_message(MTPROTO_CONFIG['chat_id'], text)
            logger.info(f"<- [Диспетчер] Текст через {mode} успешно отправлен.")
        except tg_client.NotAuthorized as e:
            logger.error(f"<!> [Диспетчер] {mode} недоступен: {e}")
        except Exception:
            logger.exception(f"<!> [Диспетчер] НЕУДАЧА при отправке текста через {mode}.")

    await asyncio.gather(*(_send_via(mode) for mode in TELEGRAM_MODES))

async def send_telegram_media_group(media_items: list, caption=""):
    """Главный диспетчер: отправляет медиагруппу через все включенные режимы параллельно;
    ошибка одного режима не мешает остальным."""
    async def _send_via(mode):
        try:
            logger.info(f"-> [Диспетчер] Попытка отправки медиагруппы ({len(media_items)} шт.) через {mode}...")
            if mode == 'BOT':
                # Bot API не примет видео тяжелее лимита — шлём группу без него (в MTPROTO лимит 2 ГБ)
                items = []
                for item in media_items:
                    if item['type'] == 'video' and os.path.getsize(item['path']) > BOT_MAX_VIDEO_MB * 1024 * 1024:
                        logger.error("[BotAPI] Видео %.1f МБ превышает лимит Bot API (%d МБ) — группа уйдёт без видео.",
                                     os.path.getsize(item['path']) / 1048576, BOT_MAX_VIDEO_MB)
                        continue
                    items.append(item)
                if not items:
                    if caption:
                        await asyncio.to_thread(_send_bot_api_request, "sendMessage",
                                                data={"chat_id": BOT_CONFIG['chat_id'], "text": caption})
                    logger.info("<- [Диспетчер] BOT: медиа после фильтра не осталось, отправлен только текст.")
                    return
                media_payload, files_to_attach = [], {}
                for item in items:
                    path, attach_name = item['path'], os.path.basename(item['path'])
                    files_to_attach[attach_name] = path
                    payload_item = {'type': item['type'], 'media': f'attach://{attach_name}'}
                    if item['type'] == 'video':
                        meta = item.get('meta', {})
                        payload_item.update({'width': meta.get('width'), 'height': meta.get('height'), 'duration': meta.get('duration'), 'supports_streaming': True})
                        if item.get('thumb_path'):
                            thumb_path, thumb_attach_name = item['thumb_path'], os.path.basename(item['thumb_path'])
                            files_to_attach[thumb_attach_name] = thumb_path
                            payload_item['thumbnail'] = f"attach://{thumb_attach_name}"
                    media_payload.append(payload_item)
                if caption and media_payload: media_payload[-1]['caption'] = caption
                data = {"chat_id": BOT_CONFIG['chat_id'], "media": json.dumps(media_payload)}
                await asyncio.to_thread(_send_bot_api_request, "sendMediaGroup", data=data, file_paths=files_to_attach)

            elif mode == 'MTPROTO':
                # Загрузку не ограничиваем: медленная сеть — не сбой; мёртвую сеть
                # Telethon добьёт сам (reconnect x10 → исключение)
                total_mb = sum(os.path.getsize(i['path']) for i in media_items) / 1048576
                logger.info(f"[MTProto] Загрузка {len(media_items)} файлов ({total_mb:.1f} МБ)...")
                client = await tg_client.ensure()
                media_objects = []
                for item in media_items:
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
                await _mtproto_send_media_group(MTPROTO_CONFIG['chat_id'], media_objects, caption)

            logger.info(f"<- [Диспетчер] Медиагруппа через {mode} успешно отправлена.")
        except tg_client.NotAuthorized as e:
            logger.error(f"<!> [Диспетчер] {mode} недоступен: {e}")
        except Exception:
            logger.exception(f"<!> [Диспетчер] НЕУДАЧА при отправке медиагруппы через {mode}.")

    await asyncio.gather(*(_send_via(mode) for mode in TELEGRAM_MODES))

# --- 7. ГЛАВНАЯ ФУНКЦИЯ ---

async def send_frigate_alert(payload_json: str):
    payload = json.loads(payload_json)

    if payload["type"] != "end":
        logger.debug(f"Skipped event type: {payload['type']}")
        return

    logger.info(f"Payload: {payload_json}")

    await asyncio.to_thread(subprocess.run, ["find", CLIP_DIR, "-type", "f", "-mmin", "+1440", "-delete"])
    await asyncio.to_thread(subprocess.run, ["find", CLIP_DIR, "-type", "d", "-empty", "-delete"])

    VAAPI_DEVICE = None
    try:
        dri_path = "/dev/dri"
        if os.path.isdir(dri_path):
            for node in sorted(os.listdir(dri_path)):
                if node.startswith("renderD"):
                    vendor_path = f"/sys/class/drm/{node}/device/vendor"
                    if os.path.exists(vendor_path):
                        with open(vendor_path, 'r') as f:
                            if f.read().strip() == "0x8086": # Нашли Intel!
                                VAAPI_DEVICE = os.path.join(dri_path, node)
                                logger.info(f"Найдено устройство Intel VAAPI: {VAAPI_DEVICE}")
                                break
    except Exception as e:
        logger.error(f"Ошибка при поиске устройства VAAPI: {e}")

    if not VAAPI_DEVICE:
        logger.warning("Intel VAAPI не найден — видео будет кодироваться на CPU (libx264): медленнее и грузит процессор.")

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

    media_items = []
    try:
        snapshots = [(d, os.path.join(review_dir, f"{d}.jpg")) for d in detection_times]
        fetched = await asyncio.gather(*(asyncio.to_thread(_fetch_snapshot, d, p) for d, p in snapshots))
        media_items += [{'type': 'photo', 'path': p} for (d, p), ok in zip(snapshots, fetched) if ok]
        logger.info("Снапшоты: скачано %d из %d", len(media_items), len(detection_times))

        export_path = await _wait_export(export_id) if export_id else None
        if export_path:
            video_item = await _prepare_video(export_path, review_dir, VAAPI_DEVICE)
            if video_item:
                media_items.append(video_item)
    finally:
        # Экспорт удаляется всегда, даже если обработка упала
        if export_id:
            await asyncio.to_thread(_delete_export, export_id)

    detections_data, all_zones = [], set()
    raw_list = await asyncio.gather(*(asyncio.to_thread(_fetch_detection, d) for d in detection_times))
    for raw_data in raw_list:
        if not raw_data:
            continue
        all_zones.update(raw_data.get("zones", []))
        label_info = LABEL_DICT.get(raw_data.get("label", "UFO"), {"emoji": "❓", "name": "НЕЧТО"})
        additional_info = set(filter(None, [raw_data.get('sub_label'), raw_data.get('data', {}).get('recognized_license_plate')]))
        additional_display = f" [{' | '.join(sorted(additional_info))}]" if additional_info else ""
        detections_data.append({'raw': raw_data,
                                'pretty_label': f"{label_info['emoji']} {label_info['name']}{additional_display}"})
    
    unique_pretty_labels = {d['pretty_label'] for d in detections_data}
    labels_display = ", ".join(sorted(unique_pretty_labels))
    zone_display = f" в зонах {', '.join(sorted(all_zones))}" if all_zones else ""
    main_block = (
        f"🕒 {format_time(start_time)} – {format_time(end_time)}\n"
        f"{labels_display} at {camera}{zone_display}"
    )

    # Сводка review.genai: задача крутилась параллельно с самого начала
    review_metadata = await summary_task if summary_task else None
    if review_metadata and review_metadata.get("shortSummary"):
        threat_mark = {1: "⚠️ ", 2: "🚨 "}.get(review_metadata.get("potential_threat_level") or 0, "")
        main_block += f"\n{threat_mark}📝 {review_metadata['shortSummary']}"

    message_blocks = [main_block]
    if FRIGATE_PUBLIC_URL:
        message_blocks.append(f"🔗 {FRIGATE_PUBLIC_URL.rstrip('/')}/review?id={review_id}")

    caption_text = "\n\n".join(message_blocks)
    send_caption_separately = len(caption_text) > MAX_CAPTION_LENGTH
    
    send_queue = []
    if SEND_VIDEO_SEPARATELY:
        photo_items = [item for item in media_items if item['type'] == 'photo']
        video_items = [item for item in media_items if item['type'] == 'video']
        if photo_items:
            for i in range(0, len(photo_items), MAX_MEDIA_PER_GROUP): send_queue.append(photo_items[i:i + MAX_MEDIA_PER_GROUP])
        if video_items:
            for video_item in video_items: send_queue.append([video_item])
    else:
        for i in range(0, len(media_items), MAX_MEDIA_PER_GROUP): send_queue.append(media_items[i:i + MAX_MEDIA_PER_GROUP])

    caption_for_last_group = ""
    if not send_caption_separately and caption_text:
        caption_for_last_group = caption_text

    if send_queue:
        for media_group in send_queue[:-1]:
            await send_telegram_media_group(media_group)
        await send_telegram_media_group(send_queue[-1], caption=caption_for_last_group)
    elif caption_text and not send_caption_separately:
        await send_telegram_message(caption_text)
    
    if send_caption_separately and caption_text:
        current_msg = ""
        for block in message_blocks:
            if len(current_msg) + len(block) + 2 <= MAX_MESSAGE_LENGTH:
                current_msg += block + "\n\n"
            else:
                if current_msg.strip(): await send_telegram_message(current_msg.strip())
                current_msg = block + "\n\n"
        if current_msg.strip(): await send_telegram_message(current_msg.strip())

# --- БЛОК ДЛЯ ЗАПУСКА ---
if __name__ == "__main__":
    log_config.setup("debug.log")
    if len(sys.argv) < 2:
        print("Ошибка: Необходимо передать JSON payload.", file=sys.stderr)
        sys.exit(1)

    payload_from_cli = sys.argv[1]

    async def debug_run():
        logger.info("Запуск в режиме отладки (сессия *_debug)...")
        # Отключение клиента гарантирует finally внутри session(), даже при ошибке
        async with tg_client.session(debug=True):
            await send_frigate_alert(payload_from_cli)
        logger.info("Функция send_frigate_alert завершена.")

    try:
        asyncio.run(debug_run())
    except Exception:
        logger.exception("Ошибка во время выполнения send_frigate_alert:")
        sys.exit(1)
