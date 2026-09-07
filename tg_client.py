"""
Sole owner of the Telethon client.

Rule: the session is opened by the TG_MTPROTO channel worker — session() is the
channel's lifecycle in CHANNELS and lives as long as the worker. The transport gets
the client via 'await tg_client.ensure()'.

An unauthorized session does not bring the service down: it runs without MTProto,
and ensure() retries the connection on every send — create the session, and the
next alert goes through MTProto without a restart.

Creating a session (asks for the phone number, the code and the 2FA password interactively):
  production: docker compose exec -it frigate_bot python tg_client.py
  debug:      docker compose exec -it frigate_bot python tg_client.py --debug
"""
import os
import sys
import asyncio
from contextlib import asynccontextmanager

from telethon import TelegramClient
import log_config
from config import TG_MTPROTO_CONFIG

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logger = log_config.get_logger(__name__)

_client = None
_session = None  # session name; not None = session() is open (the TG_MTPROTO channel is enabled)


class NotAuthorized(RuntimeError):
    """Expected state "session not created yet" — logged without a traceback."""


def _session_name(debug: bool) -> str:
    return TG_MTPROTO_CONFIG['session_name'] + ('_debug' if debug else '')


def _build(name: str) -> TelegramClient:
    return TelegramClient(
        os.path.join(BASE_DIR, name),
        TG_MTPROTO_CONFIG['api_id'],
        TG_MTPROTO_CONFIG['api_hash'],
        connection_retries=10,
        retry_delay=5,
    )


async def _connect(name: str) -> TelegramClient:
    """Connects an authorized session or raises NotAuthorized with a hint.
    connect + explicit check instead of client.start(): with no session start() asks
    for the phone number via input(), which hangs in a container."""
    client = _build(name)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise NotAuthorized(
            f"MTProto session '{name}' is not authorized. Create it:\n"
            "  production: docker compose exec -it frigate_bot python tg_client.py\n"
            "  debug:      docker compose exec -it frigate_bot python tg_client.py --debug"
        )
    return client


async def ensure() -> TelegramClient:
    """Returns the connected client. If there is none, tries to connect right now:
    the session may have been created while the service was running."""
    global _client
    if _client is not None:
        return _client
    if _session is None:
        raise RuntimeError(
            "MTProto client requested outside a session: "
            "ensure() must run inside the TG_MTPROTO worker"
        )
    _client = await _connect(_session)
    logger.info("MTProto session '%s' connected", _session)
    return _client


@asynccontextmanager
async def session(debug: bool = False):
    """Opens the MTProto session for the duration of the block (lifecycle of the TG_MTPROTO channel)."""
    global _client, _session
    _session = _session_name(debug)
    try:
        await ensure()
    except Exception as e:
        # Any startup failure (no session, no network, locked session file) is not fatal:
        # ensure() reconnects on the first send
        logger.warning("MTProto unavailable at startup (%s): %s — continuing without it, will retry on every send",
                       type(e).__name__, e)

    try:
        yield
    finally:
        # Close the client that exists at exit time:
        # it may have appeared later, via ensure() during a send
        client, _client, _session = _client, None, None
        if client and client.is_connected():
            await client.disconnect()
            logger.info("MTProto session disconnected")


if __name__ == "__main__":
    # Interactive session creation: phone number, code, 2FA password — the session file is saved next to this module
    log_config.setup("debug.log")
    name = _session_name(debug='--debug' in sys.argv)
    client = _build(name)

    async def _login():
        await client.start()
        me = await client.get_me()
        logger.info("Session '%s' authorized as %s (id=%s)", name, me.first_name, me.id)
        await client.disconnect()

    asyncio.run(_login())
