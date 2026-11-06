import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

import sqlite_vec  # type: ignore[import-untyped]

from pfc.config import Settings

logger = logging.getLogger(__name__)


class Connection(sqlite3.Connection):
    vec_available: bool = False


def load_vec(connection: Connection) -> bool:
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        return True
    except (AttributeError, sqlite3.Error, OSError):
        logger.warning("sqlite-vec unavailable; using keyword-only fallback", exc_info=True)
        return False
    finally:
        try:
            connection.enable_load_extension(False)
        except (AttributeError, sqlite3.Error):
            pass


def connect(settings: Settings) -> Connection:
    path = settings.data_dir / "db" / "pfc.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, factory=Connection, check_same_thread=False)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.vec_available = load_vec(connection)
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def database(settings: Settings) -> Iterator[Connection]:
    """Own one connection per request or job, never a shared global connection."""
    connection = connect(settings)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def get_db() -> Iterator[Connection]:
    with database(Settings()) as connection:
        yield connection
