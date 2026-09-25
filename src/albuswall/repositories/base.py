#
"""Base repository class providing common database access and transaction context."""

import threading
from contextlib import contextmanager
from sqlite3 import Cursor, Row, Connection
from typing import Optional, List, Generator

from albuswall.infrastructure.database import Connector
from albuswall.log import getLogger
from albuswall.utils.sql_log import format_params, format_query

logger = getLogger("albuswall.database")

# SQLite 单写者：写操作必须串行。保持模块级 RLock 是当前最简且正确的选择。
# 若未来后端换成支持并发写的数据库，应把锁下移到 Connector 层，由后端决定
# 是否真的需要串行化。
_WRITE_LOCK = threading.RLock()


class BaseRepository:
    """Base repository encapsulating thread-safe read/write operations and transactions.

    Concurrency model
    -----------------
    * SQLite 在 WAL 模式下运行。读不阻塞写，``_fetchone`` / ``_fetchall``
      各自看到语句开始时刻的一致快照。
    * 所有写（``_execute`` 与 ``_transaction``）共用同一个 ``_WRITE_LOCK``
      串行化，避免 ``SQLITE_BUSY``。
    * ``Connector`` 保证每线程独立连接，同一线程内 ``connect()`` 返回同一条
      连接。事务块内所有读写看到同一视图。
    * 禁止在 ``_transaction`` 块内调用 ``_execute``：二者共用同一条线程本地
      连接，``_execute`` 的 ``commit()`` 会提前结束外层事务。检测到活动事务
      时直接抛错，让调用方显式发现误用。
    """

    def __init__(self, db: Connector):
        self._db: Connector = db
        self.logger = logger

    # ---------- write ----------
    def _execute(self, query: str, params=None) -> Cursor:
        """执行写语句并提交。

        不允许在 ``_transaction()`` 块内调用；事务内请直接使用 ``yield`` 出的
        连接 ``conn.execute(...)``。

        注意：不使用 ``with self._db.connect() as conn``。``sqlite3.Connection
        .__exit__`` 在异常时会 rollback，会把"拒绝调用"的异常副作用放大为
        "回滚外层事务"。这里手动管理 commit / rollback，边界清晰。
        """
        with _WRITE_LOCK:
            logger.trace(
                "Executing query: %s | params: %s",
                format_query(query),
                format_params(params),
            )
            conn = self._db.connect()
            if conn.in_transaction:
                raise RuntimeError(
                    "_execute() called inside an active transaction; "
                    "use the connection yielded by _transaction() instead."
                )
            try:
                cursor = conn.execute(query, params or ())
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("Write query failed; rolled back.")
                raise
            logger.debug("Query executed and committed.")
            return cursor

    # ---------- read ----------
    def _fetchone(self, query: str, params=None) -> Optional[Row]:
        """Fetch a single row from the database, or ``None`` if no row matches."""
        logger.trace(
            "Fetching one row with query: %s | params: %s",
            format_query(query),
            format_params(params),
        )
        conn = self._db.connect()
        cursor = conn.execute(query, params or ())
        row = cursor.fetchone()
        logger.debug("Fetched one row: %s", row)
        return row

    def _fetchall(self, query: str, params=None) -> List[Row]:
        """Fetch all rows from the database.

        Always returns a list; an empty list when no rows match. Callers never
        need to handle ``None`` here.
        """
        logger.trace(
            "Fetching all rows with query: %s | params: %s",
            format_query(query),
            format_params(params),
        )
        conn = self._db.connect()
        cursor = conn.execute(query, params or ())
        rows: List[Row] = cursor.fetchall()
        logger.debug("Fetched %d rows.", len(rows))
        return rows

    # ---------- transaction ----------
    @contextmanager
    def _transaction(self) -> Generator[Connection, None, None]:
        """Provide a transactional scope around a series of operations.

        连接生命周期
        ------------
        ``Connector.connect()`` 返回线程本地连接，不关闭。这里直接使用它，
        **不**走 ``with``：``sqlite3.Connection.__exit__`` 会在块退出时
        commit / rollback，与本方法手动管理的事务边界重叠，语义容易看走眼。
        手动 commit / rollback 边界明确，误用时不会意外提交。
        """
        with _WRITE_LOCK:
            conn: Connection = self._db.connect()
            logger.debug("Transaction started.")
            try:
                yield conn
                conn.commit()
                logger.debug("Transaction committed.")
            except Exception:
                conn.rollback()
                logger.exception("Transaction rolled back due to an exception.")
                raise

    # ---------- cleanup ----------
    def close_current_thread(self):
        """关闭当前线程的连接，防止线程池复用时的连接堆积。"""
        self._db.close()
        logger.debug(
            "Closed connection for thread %s", threading.current_thread().name
        )
