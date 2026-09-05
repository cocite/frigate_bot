"""
Единственный владелец Telethon-клиента.

Правило: сессию открывает entry point ('async with tg_client.session()'),
библиотечный код берёт клиент через 'await tg_client.ensure()'.

Неавторизованная сессия сервис не роняет: он работает без MTProto,
а ensure() пробует подключиться заново при каждой отправке — создал
сессию, и следующий алерт уйдёт уже через MTProto, без рестарта.

Создание сессии (интерактивно спросит телефон и код):
  боевая:  docker compose exec -it frigate_bot python tg_client.py
  debug:   docker compose exec -it frigate_bot python tg_client.py --debug
"""
import os
import sys
import asyncio
import logging
from contextlib import asynccontextmanager

from telethon import TelegramClient
from config import MTPROTO_CONFIG, TELEGRAM_MODES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logger = logging.getLogger(__name__)

_client = None
_session = None  # имя сессии; не None = session() открыт и MTPROTO включён


class NotAuthorized(RuntimeError):
    """Ожидаемое состояние «сессия ещё не создана» — логируется без traceback."""


def _session_name(debug: bool) -> str:
    return MTPROTO_CONFIG['session_name'] + ('_debug' if debug else '')


def _build(name: str) -> TelegramClient:
    return TelegramClient(
        os.path.join(BASE_DIR, name),
        MTPROTO_CONFIG['api_id'],
        MTPROTO_CONFIG['api_hash'],
        connection_retries=10,
        retry_delay=5,
    )


async def _connect(name: str) -> TelegramClient:
    """Подключает авторизованную сессию или кидает RuntimeError с подсказкой.
    connect + явная проверка вместо client.start(): start() при отсутствующей
    сессии интерактивно спросит телефон через input() — в контейнере это зависание."""
    client = _build(name)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise NotAuthorized(
            f"Telethon-сессия '{name}' не авторизована. Создайте её:\n"
            "  боевая:  docker compose exec -it frigate_bot python tg_client.py\n"
            "  debug:   docker compose exec -it frigate_bot python tg_client.py --debug"
        )
    return client


async def ensure() -> TelegramClient:
    """Возвращает подключённый клиент. Если его нет — пробует подключиться
    прямо сейчас: вдруг сессию уже создали, пока сервис работал."""
    global _client
    if _client is not None:
        return _client
    if _session is None:
        raise RuntimeError(
            "MTProto вне сессии: entry point должен обернуть работу "
            "в 'async with tg_client.session()'."
        )
    _client = await _connect(_session)
    logger.info("Telethon-сессия '%s' подключена.", _session)
    return _client


@asynccontextmanager
async def session(debug: bool = False):
    """Открывает MTProto-сессию на время блока. Если MTPROTO выключен — no-op."""
    global _client, _session
    if 'MTPROTO' not in TELEGRAM_MODES:
        yield
        return

    _session = _session_name(debug)
    try:
        await ensure()
    except NotAuthorized as e:
        logger.warning("MTProto пока недоступен: %s", e)
        logger.warning("Продолжаю без MTProto — буду пробовать подключиться при каждой отправке.")

    try:
        yield
    finally:
        # Закрываем клиента, существующего на момент выхода:
        # он мог появиться и позже, через ensure() при отправке
        client, _client, _session = _client, None, None
        if client and client.is_connected():
            await client.disconnect()
            logger.info("Telethon-сессия отключена.")


if __name__ == "__main__":
    # Интерактивное создание сессии: телефон, код, пароль 2FA — файл сохранится рядом.
    import log_config
    log_config.setup("debug.log")
    name = _session_name(debug='--debug' in sys.argv)
    client = _build(name)

    async def _login():
        await client.start()
        me = await client.get_me()
        print(f"Сессия '{name}' авторизована как: {me.first_name} (id={me.id})")
        await client.disconnect()

    asyncio.run(_login())
