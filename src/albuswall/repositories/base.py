#
"""Base repository class providing common database access and transaction context."""

import logging
import threading
from contextlib import contextmanager
from sqlite3 import Cursor, Row, Connection
from typing import Optional, List, Generator, TYPE_CHECKING

from albuswall.infrastructure.database import Connector
from albuswall.utils.sql_log import format_params, format_query

try:
    from albuswall.log import TRACE
except ImportError:
    TRACE = 5

if TYPE_CHECKING:
    from albuswall.log import Logger

logger: "Logger" = logging.getLogger("albuswall.database")  # type: ignore
logger.trace = lambda msg, *args: Connector.logger.log(TRACE, msg, *args)
_WRITE_LOCK = threading.RLock()


class BaseRepository:
    """Base repository encapsulating thread-safe read/write operations and transactions."""

    def __init__(self, db: Connector):
        self._db: Connector = db
        self.logger = logger

    # ---------- convenience methods ----------
    def _execute(self, query: str, params=None) -> Cursor:
        """Execute a write query and commit the transaction."""
        with _WRITE_LOCK:
            logger.trace(
                "Executing query: %s | params: %s",
                format_query(query),
                format_params(params),
            )
            with self._db.connect() as conn:
                cursor = conn.execute(query, params or ())
                conn.commit()
                logger.debug("Query executed and committed.")
                return cursor

    def _fetchone(self, query: str, params=None) -> Optional[Row]:
        """Fetch a single row from the database."""
        logger.trace(
            "Fetching one row with query: %s | params: %s",
            format_query(query),
            format_params(params),
        )
        with self._db.connect() as conn:
            cursor = conn.execute(query, params or ())
            row = cursor.fetchone()
            logger.debug("Fetched one row: %s", row)
            return row

    def _fetchall(self, query: str, params=None) -> Optional[List[Row]]:
        """Fetch all rows from the database."""
        logger.trace(
            "Fetching all rows with query: %s | params: %s",
            format_query(query),
            format_params(params),
        )

        with self._db.connect() as conn:
            cursor = conn.execute(query, params or ())
            rows = cursor.fetchall()
            logger.debug("Fetched %d rows.", len(rows))
            return rows

    @contextmanager
    def _transaction(self) -> Generator[Connection, None, None]:
        """Provide a transactional scope around a series of operations."""
        with _WRITE_LOCK:
            conn: Connection = self._db.connect()
            # if conn is None:
            #     logger.error("Failed to acquire database connection.")
            #     raise RuntimeError("Database connection acquisition failed")
            logger.debug("Transaction started.")
            try:
                yield conn
                conn.commit()
                logger.debug("Transaction committed.")
            except Exception:
                conn.rollback()
                logger.exception("Transaction rolled back due to an exception.")
                raise

    def close_current_thread(self):
        self._db.close()
        logger.debug("Closed connection for thread %s",
                     threading.current_thread().name)
