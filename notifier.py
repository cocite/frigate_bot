import requests
import json
import sys
import os
import subprocess
import functools
import time
import asyncio
from telethon.tl.types import InputMediaUploadedPhoto, InputMediaUploadedDocument, DocumentAttributeVideo, DocumentAttributeFilename
import log_config
import tg_client
from pretty_labels import LABEL_EMOJI, LABEL_NAMES
import config
from config import (ENABLED_CHANNELS, FRIGATE_PUBLIC_URL, PRETTY_LABELS,
                    GENAI_REVIEW_SHOW, GENAI_REVIEW_WAIT, GENAI_REVIEW_POLL,
                    CLIP_START_SHIFT, CLIP_END_SHIFT, CLIP_MAX_LEN,
                    OUTPUT_WIDTH, OUTPUT_FPS, OUTPUT_QP)

# --- 1. CONSTANTS (user settings live in config.py) ---
# Directories
CLIP_DIR    = "/app/data/media"
CLEANUP_INTERVAL = 3600           # how often old media in CLIP_DIR is cleaned up, seconds

# Telegram delivery (per-channel format params are in CHANNELS, section 6)
BOT_API_TIMEOUT = 300             # Bot API socket timeout (connect/write/read), seconds
DELIVERY_QUEUE_SIZE = 30          # messages per channel delivery queue

# Frigate API
FRIGATE_URL = "http://frigate:5000"
FRIGATE_TIMEOUT = (5, 15)          # (connect, read)
FRIGATE_CONFIG_TTL = 300           # Frigate config cache (used to check whether genai is enabled), seconds
# Frigate 0.18: annotated snapshots come only from the API, there are no .jpg files on disk
SNAPSHOT_PARAMS = {"bounding_box": 1, "timestamp": 1, "crop": 1, "quality": 80}

# Caption labels: the language must exist in pretty_labels.py
if PRETTY_LABELS and PRETTY_LABELS not in LABEL_NAMES:
    raise ValueError(f"PRETTY_LABELS={PRETTY_LABELS!r}: no such language in pretty_labels.py "
                     f"(available: {list(LABEL_NAMES)}); None = raw labels")

# --- 2. LOGGER ---
# Handlers are set up by the entry point via log_config.setup(); console level comes from config.LOG_LEVEL
logger = log_config.get_logger(__name__)
# --- 3. FRIGATE API ---

def _fetch_snapshot(detection_id, dest_path):
    """Downloads the annotated snapshot of a detection via the Frigate API."""
    try:
        r = requests.get(f"{FRIGATE_URL}/api/events/{detection_id}/snapshot.jpg",
                         params=SNAPSHOT_PARAMS, timeout=FRIGATE_TIMEOUT)
        if r.status_code != 200 or not r.content:
            logger.warning("Snapshot %s unavailable: HTTP %s", detection_id, r.status_code)
            return False
        with open(dest_path, "wb") as f:
            f.write(r.content)
        return True
    except requests.RequestException as e:
        logger.error("Snapshot %s download failed: %s", detection_id, e)
        return False

def _fetch_clip(camera, start_int, end_int, dest_path):
    """Downloads the recording clip for a time range: Frigate concatenates the segments
    without re-encoding and streams the result. Written to disk in chunks so a 100+ MB clip
    never sits in memory."""
    try:
        with requests.get(f"{FRIGATE_URL}/api/{camera}/start/{start_int}/end/{end_int}/clip.mp4",
                          stream=True, timeout=FRIGATE_TIMEOUT) as r:
            if r.status_code != 200:
                logger.error("Clip unavailable: HTTP %s %s", r.status_code, r.text[:200])
                return False
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        logger.info("Clip downloaded: %.1f MB", os.path.getsize(dest_path) / 1048576)
        return True
    except requests.RequestException as e:
        logger.error("Clip download failed: %s", e)
        return False


def _fetch_detection(detection_id):
    """Details of a detection (Frigate event). Returns a dict or None."""
    try:
        r = requests.get(f"{FRIGATE_URL}/api/events/{detection_id}", timeout=FRIGATE_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.error("Event %s fetch failed: %s", detection_id, e)
        return None

def _fetch_review_metadata(review_id):
    """review.genai summary from Frigate. Returns a dict (data.metadata) or None.
    The '_' parameter bypasses the nginx cache: Frigate caches /api/ JSON responses for 5 s."""
    try:
        r = requests.get(f"{FRIGATE_URL}/api/review/{review_id}",
                         params={"_": int(time.time() * 1000)},
                         timeout=FRIGATE_TIMEOUT)
        if r.status_code != 200:
            logger.info("Review %s summary unavailable: HTTP %s", review_id, r.status_code)
            return None
        return (r.json() or {}).get("data", {}).get("metadata")
    except (requests.RequestException, ValueError) as e:
        logger.error("Review %s summary request failed: %s", review_id, e)
        return None

_frigate_config_cache = {"data": None, "ts": 0.0}

async def _get_review_summary(camera, severity, review_id):
    """Background task: checks in the Frigate config that a summary is coming at all
    (review.genai enabled and covering this severity), then waits for it up to
    GENAI_REVIEW_WAIT seconds. Started at the very beginning of event processing,
    so the logged time is the full generation time on the Frigate side."""
    if time.time() - _frigate_config_cache["ts"] > FRIGATE_CONFIG_TTL:
        try:
            r = await asyncio.to_thread(
                requests.get, f"{FRIGATE_URL}/api/config", timeout=FRIGATE_TIMEOUT)
            r.raise_for_status()
            _frigate_config_cache["data"] = r.json()
            _frigate_config_cache["ts"] = time.time()
        except (requests.RequestException, ValueError) as e:
            logger.error("Frigate config fetch failed: %s", e)

    cfg = _frigate_config_cache["data"]
    if cfg is not None:
        genai_cfg = (cfg.get("cameras", {}).get(camera, {}).get("review", {}).get("genai")
                     or cfg.get("review", {}).get("genai") or {})
        if not genai_cfg.get("enabled"):
            logger.debug("Review %s: review.genai is disabled in Frigate, not waiting for a summary", review_id)
            return None
        if not (genai_cfg.get("alerts", True) if severity == "alert"
                else genai_cfg.get("detections", False)):
            logger.debug("Review %s: review.genai does not summarize severity=%s, not waiting", review_id, severity)
            return None
        logger.debug("Review %s: waiting for review.genai summary (severity=%s; alerts=%s, detections=%s)",
                     review_id, severity, genai_cfg.get("alerts", True), genai_cfg.get("detections", False))
    # config unavailable — wait as usual, nothing to lose

    started = time.time()
    deadline = started + GENAI_REVIEW_WAIT
    while True:
        meta = await asyncio.to_thread(_fetch_review_metadata, review_id)
        if meta:
            logger.info("Review %s: summary ready in %.1f s (threat level %s)",
                        review_id, time.time() - started, meta.get("potential_threat_level"))
            logger.debug("Review %s: summary %s", review_id, json.dumps(meta, ensure_ascii=False))
            return meta
        if time.time() >= deadline:
            logger.warning("Review %s: summary not ready after %d s — sending without it",
                           review_id, GENAI_REVIEW_WAIT)
            return None
        await asyncio.sleep(GENAI_REVIEW_POLL)

# --- 4. MEDIA ---

@functools.lru_cache(maxsize=None)   # the device never changes — look it up and log it once
def _find_vaapi_device():
    """Looks for an Intel render node (vendor 0x8086) in /dev/dri. Returns the path or None."""
    device = None
    try:
        dri_path = "/dev/dri"
        if os.path.isdir(dri_path):
            for node in sorted(os.listdir(dri_path)):
                if node.startswith("renderD"):
                    vendor_path = f"/sys/class/drm/{node}/device/vendor"
                    if os.path.exists(vendor_path):
                        with open(vendor_path) as f:
                            if f.read().strip() == "0x8086":
                                device = os.path.join(dri_path, node)
                                break
    except Exception as e:
        logger.error(f"VAAPI device lookup failed: {e}")
    if device:
        logger.info("Intel VAAPI found: %s", device)
    else:
        logger.warning("Intel VAAPI not found — encoding on CPU (libx264)")
    return device

async def _prepare_video(source_path, review_dir):
    """Compresses the clip (VAAPI or CPU), extracts a thumbnail and metadata.
    Returns a media item for sending or None."""
    compressed_path = os.path.join(review_dir, "final.mp4")
    started = time.time()

    vaapi_device = _find_vaapi_device()

    # Hardware-dependent part: input flags, scale filter, encoder with its quality setting.
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
        "-i", source_path,
        "-vf", f"fps={OUTPUT_FPS},{scale}",
        "-map", "0:v:0",
        "-map", "0:a:0?",          # '?' — the audio track may be missing, that is not an error
        *encoder,
        "-g", str(OUTPUT_FPS), "-bf", "2",
        "-c:a", "aac", "-b:a", "64k", "-ar", "16000", "-ac", "1",
        "-movflags", "+faststart",
        compressed_path
    ]

    try:
        await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"ffmpeg failed (exit {e.returncode}):\n{e.stderr}")
        return None
    except Exception:
        logger.exception("Video encoding failed unexpectedly")
        return None

    if not (os.path.exists(compressed_path) and os.path.getsize(compressed_path) > 1024):
        logger.error("ffmpeg finished but the output file is missing or suspiciously small")
        return None

    size_mb = os.path.getsize(compressed_path) / (1024 * 1024)
    logger.info("Video encoded in %.1f s (%s): %s (%.2f MB)",
                time.time() - started, "VAAPI" if vaapi_device else "CPU", compressed_path, size_mb)

    thumb_path = os.path.join(review_dir, "thumb.jpg")
    try:
        thumb_cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", "00:00:01.00",
            "-i", compressed_path,
            "-frames:v", "1",
            # Bot API: at most 320 px on the longer side and 200 KB
            "-vf", "scale=320:-2",
            "-q:v", "5",
            thumb_path
        ]
        await asyncio.to_thread(subprocess.run, thumb_cmd, check=True, capture_output=True, text=True)
    except Exception as e:
        logger.warning("Thumbnail extraction failed: %s", getattr(e, "stderr", e))
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
        logger.warning(f"Video metadata (ffprobe) failed: {e}")

    return {'type': 'video', 'path': compressed_path, 'thumb_path': thumb_path, 'meta': video_meta}

async def cleanup_worker():
    """Periodic cleanup of CLIP_DIR: deletes media older than a day.
    Started by the service entry point."""
    os.makedirs(CLIP_DIR, exist_ok=True)   # the directory must exist before the first find
    while True:
        await asyncio.to_thread(subprocess.run, ["find", CLIP_DIR, "-type", "f", "-mmin", "+1440", "-delete"])
        await asyncio.to_thread(subprocess.run, ["find", CLIP_DIR, "-type", "d", "-empty", "-delete"])
        await asyncio.sleep(CLEANUP_INTERVAL)

# --- 5. FORMATTER ---
# Turns an internal notification (photos, video, caption) into the channel's messages.
# The message dict is the contract between the formatter and the transport of one channel:
#   {'kind': 'media_group', 'items': [media...], 'caption': ''}  /  {'kind': 'text', 'text': ...}

def _split_text(text, limit):
    """Splits text to fit the limit: at the last sentence end or line break, else at a space, else hard cut."""
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

def messenger_style(notification, params):
    """Common messenger formatter: media in albums; the caption goes on the last media message
    if it fits caption_limit, otherwise (or when there is no media) as separate text messages
    split by message_limit. params — the channel's format_params."""
    media = [{'type': 'photo', 'path': path} for path in notification['photos']]
    separate_videos = []

    video = notification.get('video')
    if video:
        size_mb = os.path.getsize(video['path']) / 1048576
        if params['video_mb'] is not None and size_mb > params['video_mb']:
            logger.warning("Video %.1f MB exceeds the channel limit (%d MB) — sending without video",
                           size_mb, params['video_mb'])
        elif params['video_in_album']:
            media.append(video)
        else:
            separate_videos.append(video)

    messages = [{'kind': 'media_group', 'items': media[i:i + params['album_size']], 'caption': ''}
                for i in range(0, len(media), params['album_size'])]
    messages += [{'kind': 'media_group', 'items': [v], 'caption': ''} for v in separate_videos]

    caption = notification.get('caption', '')
    if caption:
        if messages and len(caption) <= params['caption_limit']:
            messages[-1]['caption'] = caption
        else:
            messages += [{'kind': 'text', 'text': chunk}
                         for chunk in _split_text(caption, params['message_limit'])]
    return messages

# --- 6. TRANSPORTS AND CHANNELS ---

def _send_bot_api_request(method, data, file_paths=None, max_retries=5):
    """(Bot API) Sends a request. Returns the successful response or raises RuntimeError.
    Retries on rate limit (429), Telegram errors (5xx) and network failures."""
    url = f"https://api.telegram.org/bot{config.TG_BOT_CONFIG['token']}/{method}"
    for _ in range(max_retries):
        files = {name: open(path, 'rb') for name, path in file_paths.items()} if file_paths else None
        try:
            # One generous timeout for everything (connect/write/read): with several channels
            # uploading in parallel the uplink saturates and write legitimately blocks for a long
            # time — a separate short connect timeout was killing exactly the request body upload.
            # Being generous also guards against duplicates: a retry after a timeout may resend
            # a media group Telegram has already accepted.
            response = requests.post(url, data=data, files=files, timeout=BOT_API_TIMEOUT)
            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                logger.warning(f"[TG:BotAPI] rate limited — retrying in {retry_after}s")
                time.sleep(retry_after + 1)
                continue
            if response.status_code >= 500:
                logger.warning(f"[TG:BotAPI] HTTP {response.status_code} from Telegram — retrying in 5s")
                time.sleep(5)
                continue
            if response.status_code != 200:
                # 4xx is a permanent error, retrying is pointless
                raise RuntimeError(f"[TG:BotAPI] {method}: HTTP {response.status_code}, {response.text[:300]}"
                                   f" (files: {list(file_paths) if file_paths else '-'})")
            return response
        except requests.RequestException as e:
            logger.warning(f"[TG:BotAPI] network error: {e} — retrying in 5s")
            time.sleep(5)
        finally:
            if files:
                for f in files.values(): f.close()
    raise RuntimeError(f"[TG:BotAPI] {method} failed after {max_retries} attempts")

def _bot_media_fields(item, fields, files):
    """(Bot API) Registers the media file (and the thumbnail) for upload;
    for a video also fills width/height/duration."""
    attach_name = os.path.basename(item['path'])
    files[attach_name] = item['path']
    if item['type'] == 'video':
        meta = item.get('meta', {})
        fields.update({'width': meta.get('width'), 'height': meta.get('height'),
                                'duration': meta.get('duration'), 'supports_streaming': True})
        if item.get('thumb_path'):
            thumb_name = os.path.basename(item['thumb_path'])
            files[thumb_name] = item['thumb_path']
            fields['thumbnail'] = f"attach://{thumb_name}"
    return attach_name

async def _deliver_bot(msg):
    """(Bot API) Sends one channel message."""
    if msg['kind'] == 'text':
        await asyncio.to_thread(_send_bot_api_request, "sendMessage",
                                data={"chat_id": config.TG_BOT_CONFIG['chat_id'], "text": msg['text']})
        return

    items = msg['items']
    if len(items) == 1:
        # sendMediaGroup requires 2–10 items — a single media item goes through its own method (sendPhoto / sendVideo)
        item = items[0]
        field = "photo" if item['type'] == 'photo' else "video"
        data, files = {"chat_id": config.TG_BOT_CONFIG['chat_id']}, {}
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
    data = {"chat_id": config.TG_BOT_CONFIG['chat_id'], "media": json.dumps(media_payload)}
    await asyncio.to_thread(_send_bot_api_request, "sendMediaGroup", data=data, file_paths=files_to_attach)

async def _mtproto_send_media_group(client, chat_id, media_objects, caption=""):
    """(MTProto) Sends an album of already uploaded media and logs the server response."""
    sent_messages = await asyncio.wait_for(
        client.send_file(chat_id, file=media_objects, caption=caption),
        timeout=180   # files are already uploaded — this is only the album assembly
    )

    if not sent_messages:
        logger.error("[TG:MTProto] server returned no message info after send")
        return
    if not isinstance(sent_messages, list):
        sent_messages = [sent_messages]

    logger.debug(f"[TG:MTProto] sent {len(sent_messages)} messages (ids: {[m.id for m in sent_messages]})")
    for msg in sent_messages:
        info = f"id={msg.id} group={msg.grouped_id} media={type(msg.media).__name__ if msg.media else '-'}"
        attrs = getattr(getattr(msg.media, 'document', None), 'attributes', None) or []
        video = next((a for a in attrs if type(a).__name__ == 'DocumentAttributeVideo'), None)
        if video:
            info += f" video={video.w}x{video.h} {video.duration}s"
        logger.debug(f"[TG:MTProto]   {info}")

async def _deliver_mtproto(msg):
    """(MTProto) Sends one channel message."""
    client = await tg_client.ensure()

    if msg['kind'] == 'text':
        await asyncio.wait_for(client.send_message(config.TG_MTPROTO_CONFIG['chat_id'], msg['text']), timeout=30)
        return

    # Uploads are not time-limited: a slow network is not a failure, and a dead one
    # Telethon gives up on itself (connection_retries=10 → exception)
    items = msg['items']
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
    await _mtproto_send_media_group(client, config.TG_MTPROTO_CONFIG['chat_id'], media_objects, msg['caption'])

# Channel registry. tag — log label; formatter + format_params — how a notification
# becomes messages; transport — how a message is sent; lifecycle (optional) — async
# context manager the delivery worker opens for its lifetime (MTProto session).
# A channel is active when its name is in ENABLED_CHANNELS; its <NAME>_CONFIG must exist in config.py
# (checked below), the config of a disabled channel may be absent.
CHANNELS = {
    'TG_BOT': {
        'tag': 'TG:BotAPI',                 # channel tag in logs
        'formatter': messenger_style,
        'transport': _deliver_bot,
        'format_params': dict(album_size=8, caption_limit=1024, message_limit=4096,
                              video_mb=49,    # Bot API upload limit (~50 MB)
                              video_in_album=True),
    },
    'TG_MTPROTO': {
        'tag': 'TG:MTProto',
        'formatter': messenger_style,
        'transport': _deliver_mtproto,
        'lifecycle': tg_client.session,   # the channel owns its MTProto session (opened by its delivery worker)
        'format_params': dict(album_size=8, caption_limit=1024, message_limit=4096,
                              video_mb=None,  # 2 GB, effectively no limit
                              video_in_album=True),
    },
}

# Every enabled channel must exist and have its <NAME>_CONFIG in config.py
for _channel in ENABLED_CHANNELS:
    if _channel not in CHANNELS:
        raise ValueError(f"Unknown channel '{_channel}' in ENABLED_CHANNELS (available: {list(CHANNELS)})")
    if not hasattr(config, f"{_channel}_CONFIG"):
        raise ValueError(f"{_channel} is in ENABLED_CHANNELS but {_channel}_CONFIG is missing in config.py")

# --- 7. DELIVERY QUEUES ---

# Delivery queues: one per enabled channel, each channel drains its own at its own pace —
# a slow channel never delays a fast one
_delivery_queues = {}

def _describe(msg):
    """Short message description for logs: 'media_group (4 files, 18.3 MB)' / 'text (1420 chars)'."""
    if msg['kind'] == 'media_group':
        mb = sum(os.path.getsize(i['path']) for i in msg['items']) / 1048576
        return f"media_group ({len(msg['items'])} files, {mb:.1f} MB)"
    return f"text ({len(msg['text'])} chars)"

async def _delivery_loop(channel):
    """Endless worker loop: take (review_id, msg) from the queue, hand it to the transport.
    A failed message is logged and does not kill the worker."""
    q = _delivery_queues[channel]
    tag, transport = CHANNELS[channel]['tag'], CHANNELS[channel]['transport']
    logger.info(f"[{tag}] delivery worker started")
    while True:
        review_id, msg = await q.get()
        logger.info(f"[{tag}] {review_id}: sending {_describe(msg)}, queue {q.qsize()}")
        started = time.monotonic()
        try:
            await transport(msg)
            logger.info(f"[{tag}] {review_id}: {msg['kind']} delivered in {time.monotonic() - started:.1f} s, queue {q.qsize()}")
        except tg_client.NotAuthorized as e:
            logger.error(f"[{tag}] {review_id}: {e}")
        except Exception:
            logger.exception(f"[{tag}] {review_id}: {msg['kind']} FAILED")
        finally:
            q.task_done()

async def _delivery_worker(channel, debug=False):
    """Channel worker. If the channel has a lifecycle (MTProto session),
    the worker owns it: opens it for its whole lifetime."""
    lifecycle = CHANNELS[channel].get('lifecycle')
    if lifecycle:
        async with lifecycle(debug):
            await _delivery_loop(channel)
    else:
        await _delivery_loop(channel)

def start_delivery_workers(debug=False):
    """Creates the queues and delivery workers for the enabled channels.
    Called by the entry point; keep the returned tasks referenced, otherwise they can be garbage-collected."""
    tasks = []
    for channel in ENABLED_CHANNELS:
        _delivery_queues[channel] = asyncio.Queue(maxsize=DELIVERY_QUEUE_SIZE)
        tasks.append(asyncio.create_task(_delivery_worker(channel, debug), name=f"delivery-{channel}"))
    return tasks

async def flush_delivery_queues():
    """Waits until the workers drain all queues (debug run)."""
    await asyncio.gather(*(q.join() for q in _delivery_queues.values()))

async def dispatch_notification(notification):
    """Formats the notification for every enabled channel and puts the resulting messages
    into the channel queues. From there each channel delivers at its own pace."""
    rid = notification['review_id']
    if not _delivery_queues:
        logger.error(f"{rid}: delivery workers not started (start_delivery_workers) — notification lost")
        return
    for channel, q in _delivery_queues.items():
        ch, tag = CHANNELS[channel], CHANNELS[channel]['tag']
        try:
            messages = ch['formatter'](notification, ch['format_params'])
        except Exception:
            logger.exception(f"[{tag}] {rid}: formatter failed — event skipped")
            continue
        if q.full():
            logger.warning(f"[{tag}] queue is full ({q.qsize()}) — channel can't keep up, waiting for a slot")
        for msg in messages:
            await q.put((rid, msg))
        logger.info(f"[{tag}] {rid}: {len(messages)} messages enqueued, queue {q.qsize()}")

# --- 8. REVIEW HANDLER ---

def _time_from_id(detection_id): return float(detection_id.split('-')[0])
def _format_time(ts): return time.strftime('%H:%M:%S', time.localtime(ts))

def _build_caption(raw_events, start_time, end_time, camera, review_id, review_metadata):
    """Builds the notification text: time span, labels with names/plates, zones,
    review.genai summary, review link. Pure function, no I/O."""
    labels, zones = set(), set()
    for raw in raw_events:
        zones.update(raw.get("zones", []))
        label = raw.get("label", "*")
        if PRETTY_LABELS:
            emoji = LABEL_EMOJI.get(label, LABEL_EMOJI["*"])
            name  = LABEL_NAMES[PRETTY_LABELS].get(label, LABEL_NAMES[PRETTY_LABELS]["*"])
            label = f"{emoji} {name}"
        extra = set(filter(None, [raw.get('sub_label'), raw.get('data', {}).get('recognized_license_plate')]))
        extra_display = f" [{' | '.join(sorted(extra))}]" if extra else ""
        labels.add(f"{label}{extra_display}")

    zone_display = f" in {', '.join(sorted(zones))}" if zones else ""
    caption = (
        f"🕒 {_format_time(start_time)} – {_format_time(end_time)}\n"
        f"{', '.join(sorted(labels))} on {camera}{zone_display}"
    )

    if review_metadata and review_metadata.get("shortSummary"):
        threat_mark = {1: "⚠️ ", 2: "🚨 "}.get(review_metadata.get("potential_threat_level") or 0, "")
        caption += f"\n{threat_mark}📝 {review_metadata['shortSummary']}"

    if FRIGATE_PUBLIC_URL:
        caption += f"\n\n🔗 {FRIGATE_PUBLIC_URL.rstrip('/')}/review?id={review_id}"
    return caption

async def handle_review(payload_json: str):
    """Handles one frigate/reviews message: on 'end' downloads the snapshots and the clip,
    encodes the video, fetches detection details and the genai summary, builds the
    notification and dispatches it to the channels."""
    payload = json.loads(payload_json)

    if payload["type"] != "end":
        logger.debug(f"Skipped event type: {payload['type']}")
        return

    logger.debug(f"Payload: {payload_json}")

    detections = payload["after"]["data"]["detections"]
    detection_ids = sorted(detections, key=_time_from_id)

    start_time = payload["after"]["start_time"]
    end_time = payload["after"]["end_time"]
    camera = payload["after"]["camera"]
    review_id = payload["after"]["id"]
    severity = payload["after"].get("severity", "alert")
    logger.info(f"Review {review_id}: severity {severity}, camera {camera}, {len(detection_ids)} detections")

    review_dir = f"{CLIP_DIR}/{review_id}"
    os.makedirs(review_dir, exist_ok=True)

    # The review.genai summary is awaited in parallel with everything else:
    # Frigate generates it after the activity ends, so the video pipeline gives it a head start
    summary_task = (asyncio.create_task(_get_review_summary(camera, severity, review_id))
                    if GENAI_REVIEW_SHOW else None)

    start_int = int(start_time) - CLIP_START_SHIFT
    end_int = int(end_time) + CLIP_END_SHIFT
    if end_int - start_int > CLIP_MAX_LEN:
        start_int = end_int - CLIP_MAX_LEN

    # Clip and snapshots are downloaded in parallel
    raw_clip = os.path.join(review_dir, "clip.mp4")
    snapshots = [(d, os.path.join(review_dir, f"{d}.jpg")) for d in detection_ids]
    has_clip, *fetched = await asyncio.gather(
        asyncio.to_thread(_fetch_clip, camera, start_int, end_int, raw_clip),
        *(asyncio.to_thread(_fetch_snapshot, d, p) for d, p in snapshots),
    )
    photos = [p for (d, p), ok in zip(snapshots, fetched) if ok]
    logger.info("Review %s: snapshots downloaded %d of %d", review_id, len(photos), len(detection_ids))

    video = await _prepare_video(raw_clip, review_dir) if has_clip else None   # dict or None

    raw_events = [r for r in await asyncio.gather(
        *(asyncio.to_thread(_fetch_detection, d) for d in detection_ids)) if r]

    # review.genai summary: the task has been running since the very beginning.
    # Its failure must not cost the alert — the summary is optional
    review_metadata = None
    if summary_task:
        try:
            review_metadata = await summary_task
        except Exception:
            logger.exception(f"Review {review_id}: summary task failed — sending without it")

    notification = {
        'review_id': review_id,
        'photos': photos,
        'video': video,
        'caption': _build_caption(raw_events, start_time, end_time, camera, review_id, review_metadata),
    }
    await dispatch_notification(notification)

# --- DEBUG RUN ---
# Manual run for one event: python notifier.py '<json payload>' (uses the *_debug MTProto session)
if __name__ == "__main__":
    log_config.setup("debug.log")
    if len(sys.argv) < 2:
        print("Usage: python notifier.py '<json payload>'", file=sys.stderr)
        sys.exit(1)

    payload_from_cli = sys.argv[1]

    async def debug_run():
        logger.info("Debug run started (session *_debug)")
        workers = start_delivery_workers(debug=True)
        try:
            await handle_review(payload_from_cli)
            await flush_delivery_queues()   # wait until every channel has sent everything
        finally:
            for t in workers:
                t.cancel()
            # let the workers shut down cleanly (session() disconnects the client in its finally)
            await asyncio.gather(*workers, return_exceptions=True)
        logger.info("Debug run finished")

    try:
        asyncio.run(debug_run())
    except Exception:
        logger.exception("Debug run failed")
        sys.exit(1)
