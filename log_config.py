"""
Single logging setup.

The entry point calls once:
    log_config.setup("frigate_bot.log")
Modules get their logger via log_config.get_logger(__name__).
The file gets everything (DEBUG); the console (docker logs) follows config.LOG_LEVEL.
"""
import logging
import logging.handlers
import os
import sys

from config import LOG_LEVEL

if LOG_LEVEL not in ('DEBUG', 'INFO', 'WARNING', 'ERROR'):
    raise ValueError(f"LOG_LEVEL={LOG_LEVEL!r}: must be one of DEBUG / INFO / WARNING / ERROR")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MAX_LOG_SIZE = 5 * 1024 * 1024
BACKUP_COUNT = 2
FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'


def setup(log_filename):
    """Configures the root logger: rotating file + stderr. A repeated call is a no-op."""
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
    console_handler.setLevel(LOG_LEVEL)      # console follows config.LOG_LEVEL, the file always gets everything

    # root at INFO: library debug noise (urllib3, aiomqtt, ...) is cut off by itself;
    # our modules are at DEBUG via get_logger(), their records are filtered by the handlers.
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Chatty libraries — warnings only
    logging.getLogger('telethon').setLevel(logging.WARNING)


def get_logger(name):
    """Module logger: passes everything down to DEBUG, the handlers filter from there."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    return logger
