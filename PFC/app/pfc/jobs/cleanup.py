import asyncio
from datetime import UTC, datetime

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.sessions import purge_expired_sessions
from pfc.security.sessions import stamp


def purge(settings: Settings) -> int:
    with database(settings) as db:
        return purge_expired_sessions(db, stamp(datetime.now(UTC)))


async def session_cleanup(settings: Settings, interval: float = 900) -> None:
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(purge, settings)
