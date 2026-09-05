"""
Единая настройка логирования.

Entry point вызывает один раз:
    log_config.setup("frigate_bot.log")
Модули хендлеры не трогают — только logger = logging.getLogger(__name__)
(и setLevel(DEBUG) у себя, если нужны debug-строки).
"""
import logging
import logging.handlers
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MAX_LOG_SIZE = 5 * 1024 * 1024
BACKUP_COUNT = 2
FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'


def setup(log_filename):
    """Настраивает root-логгер: ротируемый файл + stderr. Повторный вызов — no-op."""
    root = logging.getLogger()
    if root.handlers:
        return

    formatter = logging.Formatter(FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(BASE_DIR, log_filename),
        maxBytes=MAX_LOG_SIZE, backupCount=BACKUP_COUNT)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)

    # root на INFO: debug-шум библиотек (urllib3 и пр.) отсекается сам,
    # а наши модули включают себе DEBUG локально через setLevel.
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Болтливые библиотеки — только предупреждения
    logging.getLogger('telethon').setLevel(logging.WARNING)
