"""
Конфигурация frigate_bot
Скопируйте в config.py и заполните: cp config.example.py config.py
"""

# --- TELEGRAM MODES ---
# Which protocols to use: 'BOT' (Bot API) and/or 'MTPROTO' (Telethon)
# Just uncomment the one you need
TELEGRAM_MODES = ['BOT']
# TELEGRAM_MODES = ['MTPROTO']
# TELEGRAM_MODES = ['BOT', 'MTPROTO']

# --- BOT API CONFIGURATION ---
# Just uncomment the one you need
BOT_CONFIG = {
    "token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
    "chat_id": -1234567890  # First chat
}

# BOT_CONFIG = {
#     "token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
#     "chat_id": -9876543210  # Second chat
# }

# --- MTPROTO CONFIGURATION (Telethon) ---
# Get api_id and api_hash from https://my.telegram.org/apps
MTPROTO_CONFIG = {
    "api_id": 123456,
    "api_hash": 'abcdef1234567890abcdef1234567890',
    "session_name": 'frigate_session',
    "chat_id": -1234567890
}

# Публичный URL веб-интерфейса Frigate, например "http://192.168.1.10:5000"
# или "https://frigate.example.com" — внизу сообщения будет ссылка на review.
FRIGATE_PUBLIC_URL = ""  # "" — не выводить

# Сводка review.genai из Frigate (генерирует сам Frigate, см. README)
GENAI_REVIEW_SHOW = True        # добавлять сводку в подпись
GENAI_REVIEW_WAIT = 25          # сколько ждать генерацию с начала обработки, сек
GENAI_REVIEW_POLL = 2.0         # интервал опроса

# Настройки видео
EXPORT_START_SHIFT = 5          # запас до начала события, сек
EXPORT_END_SHIFT = 5            # запас после конца события, сек
EXPORT_MAX_LEN = 180            # максимум длины ролика, сек (влезает в лимит Bot API)

# Кодирование
OUTPUT_WIDTH = 1024
OUTPUT_FPS = 25
OUTPUT_QP = 26
