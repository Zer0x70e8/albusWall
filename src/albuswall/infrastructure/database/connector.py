#
"""
Thread-safe SQLite database connector. Each thread keeps its own connection.

线程模型:
    - 每个线程通过 threading.local 持有一个独立连接（正常使用路径）
    - check_same_thread 保持默认 True：sqlite3 层强制线程隔离，
      任何跨线程误用会立即抛 ProgrammingError，而不是静默产生数据竞争
    - 不提供 close_all()：它需要跨线程 close，与本模型的约束直接冲突。
      线程退出前自行 close()，或用 keep_alive() / release_keep_alive()

内存库:
    - db_path 为 None 时使用每实例唯一的 shared-cache URI：
      ``file:albuswall_<uuid>?mode=memory&cache=shared``
    - 同一 Connector 实例内所有线程看到同一个库；不同实例相互隔离
    - 只要还有任意一条连接活着，内存库就存在；最后一条连接关闭后库销毁。
      若想在 worker 全部退出后继续访问数据，让主线程调用 keep_alive()
      作为库的"锚点"。
"""

import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional, Set, Tuple

import albuswall
from albuswall.log import getLogger
from albuswall.resources import schema

__all__ = ["DEFAULT_SCHEMA_FILE", "Connector"]

DEFAULT_SCHEMA_FILE = schema

PRAGMA_FOREIGN_KEYS_ON = "PRAGMA foreign_keys = ON"
PRAGMA_JOURNAL_WAL = "PRAGMA journal_mode = WAL"
PRAGMA_BUSY_TIMEOUT = "PRAGMA busy_timeout = 5000"
PRAGMA_RESOURCE_TRIGGERS = "PRAGMA recursive_triggers = OFF"

# 当前 schema 版本；每次结构变化时 +1，并在 _run_migrations 里补一段
SCHEMA_VERSION = 1  # TODO 目前是早期开发，在稳定之前都不改

_package_name = __name__.split('.', 2)[-1] if '.' in __name__ else __name__

logger = getLogger(f"{albuswall.__name__}.{_package_name}")


class Connector:
    """SQLite 连接管理器：每线程独立连接，不跨线程共享。

    说明
    ----
    * ``check_same_thread`` 保持默认 True。跨线程误用会被 sqlite3 立即
      拒绝（ProgrammingError），而不是静默产生数据竞争。
    * 不提供 ``close_all()``：跨线程 close 与上面的约束直接冲突。线程应在
      退出前自行 ``close()``，或用 ``keep_alive()`` / ``release_keep_alive()``。
    * 内存库使用每实例唯一的 shared-cache URI，使同实例内多个线程看到同一
      个库，与文件库的语义一致（同一 Connector = 同一数据源）。
    * Connector 是工厂，不是上下文管理器；事务语义由 sqlite3.Connection
      提供：``with connector.connect() as conn: ...``
    """

    # 进程级：同一进程内多个 Connector 共用，避免重复初始化同一文件
    # key = (resolved_path_or_memory_uri, schema_sql_hash, SCHEMA_VERSION)
    _initialized_dbs: Set[Tuple[str, int, int]] = set()
    _init_dbs_lock = threading.Lock()

    logger = logger

    _resolved_path: Optional[str]

    def __init__(self, db_path: Optional[str] = None,
                 schema_file=None,
                 must_exist: bool = False):
        self.db_path = db_path
        self._must_exist = must_exist
        self._schema_file = schema_file or DEFAULT_SCHEMA_FILE
        self._schema_sql = self._load_schema(self._schema_file)

        self._resolved_path: Optional[str] = None
        self._resolve_lock = threading.Lock()

        self._local = threading.local()

        # 每实例唯一的 shared-cache 内存库 URI；用 uuid 而非 id(self)，
        # 避免对象回收后 id 复用导致不同实例撞到同一个 URI。
        self._memory_uri = (
            f"file:albuswall_{uuid.uuid4().hex}?mode=memory&cache=shared"
        )

        self.logger.debug("Connector init: db_path=%s, must_exist=%s",
                          self.db_path, self._must_exist)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _load_schema(schema_resource):
        try:
            return schema_resource.read_text(encoding='utf-8')
        except AttributeError:
            with open(str(schema_resource), 'r', encoding='utf-8') as f:
                return f.read()

    @property
    def _is_memory(self) -> bool:
        return self.db_path is None

    def _resolve_db_path(self) -> str:
        if self._resolved_path is not None:
            return self._resolved_path
        with self._resolve_lock:
            if self._resolved_path is not None:
                return self._resolved_path
            if self.db_path is None:
                self.logger.info("No db_path given; using in-memory database")
                self._resolved_path = self._memory_uri
            else:
                self._resolved_path = str(Path(self.db_path).resolve())
                self.logger.info("Database path resolved: %s", self._resolved_path)
            return self._resolved_path

    def _check_file_must_exist(self, resolved_path: str) -> None:
        if self._must_exist and not self._is_memory:
            if not Path(resolved_path).exists():
                raise FileNotFoundError(
                    f"Database file does not exist and must_exist=True: {resolved_path}"
                )

    # ---------------------------------------------------------------- connect
    def connect(self) -> sqlite3.Connection:
        """返回当前线程的连接；首次调用时创建。

        线程内重复调用返回同一条连接（``threading.local`` 保证）。
        """
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            self.logger.trace("Reusing connection for thread %s",
                              threading.current_thread().name)
            return conn

        self.logger.debug("Creating connection for thread %s",
                          threading.current_thread().name)
        conn = self._create_connection()
        self._local.connection = conn
        return conn

    def _create_connection(self) -> sqlite3.Connection:
        resolved = self._resolve_db_path()
        self._check_file_must_exist(resolved)

        if self._is_memory:
            # uri=True 是必须的：否则 sqlite3 会把 "file:..." 当成普通文件名，
            # 在当前目录创建一个名为 file:albuswall_xxx?mode=memory... 的文件。
            conn = sqlite3.connect(resolved, uri=True)
        else:
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread 保持默认 True
            conn = sqlite3.connect(resolved)

        try:
            conn.row_factory = sqlite3.Row
            conn.execute(PRAGMA_FOREIGN_KEYS_ON)
            if not self._is_memory:
                # WAL 对内存库无意义
                conn.execute(PRAGMA_JOURNAL_WAL)
            conn.execute(PRAGMA_BUSY_TIMEOUT)
            conn.execute(PRAGMA_RESOURCE_TRIGGERS)
            self._ensure_initialized(conn, resolved)
        except Exception:
            # 初始化失败时主动关掉，避免半初始化连接泄漏
            try:
                conn.close()
            except Exception as close_err:
                self.logger.debug("Cleanup close failed: %s", close_err)
            raise
        return conn

    # --------------------------------------------------------- initialization
    def _ensure_initialized(self, conn: sqlite3.Connection, resolved: str) -> None:
        # 内存库用每实例唯一的 URI 作 key → 不同实例互不影响
        # 文件库用解析后的路径作 key → 同一文件在同进程内只初始化一次
        key = (resolved, hash(self._schema_sql), SCHEMA_VERSION)

        with Connector._init_dbs_lock:
            if key in Connector._initialized_dbs:
                return
            self.logger.info("Initializing database: %s", resolved)
            conn.executescript(self._schema_sql)
            self._run_migrations(conn)
            Connector._initialized_dbs.add(key)
            self.logger.info("Database ready: %s", resolved)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """
        在 schema 之后执行，只处理非幂等结构变更。

        新增迁移时:
          1. SCHEMA_VERSION += 1
          2. 在下面追加 `if current < N:` 分支
          3. 同步更新 schema 文件里的 CREATE TABLE 定义（给新库用）
             —— schema 文件应始终反映 *最新版本*，迁移代码只负责补齐旧库
        """
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current >= SCHEMA_VERSION:
            return

        self.logger.info("Migrating schema: %d -> %d", current, SCHEMA_VERSION)

        if current < 1:
            self._add_column_if_not_exists(
                conn, 'asset_candidate_cache', 'status',
                "TEXT NOT NULL DEFAULT 'pending'"
            )
            self._add_column_if_not_exists(
                conn, 'asset_candidate_cache', 'claimed_by', "TEXT"
            )
            self._add_column_if_not_exists(
                conn, 'asset_candidate_cache', 'claimed_at', "TEXT"
            )

        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.logger.info("Migration done, schema version = %d", SCHEMA_VERSION)

    @staticmethod
    def _add_column_if_not_exists(conn, table, column_name, column_definition):
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        if column_name not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column_name} {column_definition}"
            )
            Connector.logger.debug("Added column %s.%s", table, column_name)

    # ------------------------------------------------------------------ close
    def close(self):
        """关闭当前线程的连接。只影响本线程，不影响其他线程的连接。"""
        conn = getattr(self._local, "connection", None)
        if conn is None:
            return
        # 先清引用，即使 close() 抛错也不会留下坏引用
        self._local.connection = None
        try:
            conn.close()
        except Exception as e:
            self.logger.warning("Close error: %s", e)
        else:
            self.logger.debug("Closed connection for thread %s",
                              threading.current_thread().name)

    # -------------------------------------------------------------- keep-alive
    def keep_alive(self) -> None:
        """在当前线程持有一条连接，用于防止 shared-cache 内存库被销毁。

        - 内存库：只要还有任意一条连接活着，库就存在。让主线程调用本方法
          作为"锚点"，可以确保 worker 全部退出后数据仍然可访问。
        - 文件库：本方法等价于 ``connect()``，无副作用。
        - 必须在同一线程调用 ``release_keep_alive()``。
        - 返回的连接就是 ``connect()`` 的线程本地连接，可与常规读写混用；
          但不要在本连接上开启长期未提交的写事务，否则会阻塞其他线程的写。
        """
        self.connect()

    def release_keep_alive(self) -> None:
        """释放当前线程的 keep-alive 连接。等价于 ``close()``。"""
        self.close()
