"""
Конфигурация frigate_bot
Скопируйте в config.py и заполните: cp config.example.py config.py
"""

# --- КАНАЛЫ ДОСТАВКИ ---
# Доступные: TG_BOT (Telegram Bot API), TG_MTPROTO (Telegram юзер-аккаунт, видео до 2 ГБ)
# Just uncomment the one you need
ENABLED_CHANNELS = ['TG_BOT']
# ENABLED_CHANNELS = ['TG_MTPROTO']
# ENABLED_CHANNELS = ['TG_BOT', 'TG_MTPROTO']

# --- TG_BOT: Telegram Bot API ---
# Токен у @BotFather; chat_id группы отрицательный
TG_BOT_CONFIG = {
    "token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
    "chat_id": -1234567890
}

# --- TG_MTPROTO: Telegram юзер-аккаунт (Telethon) ---
# api_id и api_hash с https://my.telegram.org/apps
TG_MTPROTO_CONFIG = {
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
