#
"""
Thread-safe SQLite database connector. Each thread keeps its own connection.

线程模型:
    - 每个线程通过 threading.local 持有一个独立连接（正常使用路径）
    - 连接创建时 check_same_thread=False，允许 close_all() 从任意线程关闭所有连接
    - 每个连接的使用约束仍由 _local 保证：一个连接只被创建它的线程使用
    - close_all() 是唯一的跨线程操作，调用方应先停止 worker
"""

import sqlite3
import logging
import threading
from pathlib import Path
from typing import Optional, Set, Tuple

import albuswall
from albuswall.log import TRACE, Logger
from albuswall.resources import schema

__all__ = ["DEFAULT_SCHEMA_FILE", "Connector"]

DEFAULT_SCHEMA_FILE = schema
MEMORY_DB_PATH = ":memory:"

PRAGMA_FOREIGN_KEYS_ON = "PRAGMA foreign_keys = ON"
PRAGMA_JOURNAL_WAL = "PRAGMA journal_mode = WAL"
PRAGMA_BUSY_TIMEOUT = "PRAGMA busy_timeout = 5000"
PRAGMA_RESOURCE_TRIGGERS = "PRAGMA recursive_triggers = OFF"

# 当前 schema 版本；每次结构变化时 +1，并在 _run_migrations 里补一段
SCHEMA_VERSION = 1

_package_name = __name__.split('.', 2)[-1] if '.' in __name__ else __name__

logger: Logger = logging.getLogger(f"{albuswall.__name__}.{_package_name}")  # type: ignore
logger.trace = lambda msg, *args: logger.log(TRACE, msg, *args)


class Connector:
    # 进程级：同一进程内多个 Connector 共用，避免重复初始化同一文件
    # key = (resolved_path, schema_sql_hash, SCHEMA_VERSION)
    _initialized_dbs: Set[Tuple[str, int, int]] = set()
    _init_dbs_lock = threading.Lock()

    logger = logger

    # 实例属性类型声明
    _resolved_path: Optional[str]

    def __init__(self, db_path: Optional[str] = None,
                 schema_file=None,
                 must_exist: bool = False):
        self.db_path = db_path
        self._must_exist = must_exist
        self._schema_file = schema_file or DEFAULT_SCHEMA_FILE
        self._schema_sql = self._load_schema(self._schema_file)

        # 修复①：必须初始化，_resolve_db_path 依赖它做双重检查
        self._resolved_path: Optional[str] = None
        self._resolve_lock = threading.Lock()

        self._local = threading.local()
        self._connections_lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []

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

    # noinspection bad-return
    def _resolve_db_path(self) -> str:
        if self._resolved_path is not None:
            return self._resolved_path
        with self._resolve_lock:
            if self._resolved_path is not None:
                return self._resolved_path
            if self.db_path is None:
                self.logger.info("No db_path given; using in-memory database")
                self._resolved_path = MEMORY_DB_PATH
            else:
                self._resolved_path = str(Path(self.db_path).resolve())
                self.logger.info("Database path resolved: %s", self._resolved_path)
            return self._resolved_path

    def _check_file_must_exist(self, resolved_path: str) -> None:
        if self._must_exist and resolved_path != MEMORY_DB_PATH:
            if not Path(resolved_path).exists():
                raise FileNotFoundError(
                    f"Database file does not exist and must_exist=True: {resolved_path}"
                )

    # ---------------------------------------------------------------- connect
    # noinspection bad-argument-type,bad-return
    def connect(self) -> sqlite3.Connection:
        """
        返回当前线程的连接。若连接已被 close_all() 从其他线程关闭，
        存活校验会失败，自动重建。
        """
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            # 存活校验：close_all 可能已从其他线程关闭了它
            try:
                # noinspection unresolved-references
                conn.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                self.logger.debug(
                    "Stale connection for thread %s; recreating",
                    threading.current_thread().name,
                )
                self._local.connection = None
                with self._connections_lock:
                    try:
                        self._connections.remove(conn)
                    except ValueError:
                        pass
                # conn = None
            else:
                self.logger.log(TRACE, "Reusing connection for thread %s",
                                threading.current_thread().name)
                return conn

        self.logger.debug("Creating connection for thread %s",
                          threading.current_thread().name)
        conn = self._create_connection()
        self._local.connection = conn
        with self._connections_lock:
            self._connections.append(conn)
        return conn

    def _create_connection(self) -> sqlite3.Connection:
        resolved = self._resolve_db_path()
        self._check_file_must_exist(resolved)

        if resolved != MEMORY_DB_PATH:
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False：允许 close_all() 跨线程关闭
        conn = sqlite3.connect(resolved, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(PRAGMA_FOREIGN_KEYS_ON)
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
        # 内存库每个连接都是全新的，必须每个连接都跑一遍 schema
        if resolved == MEMORY_DB_PATH:
            conn.executescript(self._schema_sql)
            return

        # 修复③：把 schema 内容哈希和版本也纳入 key，避免不同 schema 复用同一 db 时被跳过
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

        新增迁移时：
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
            # 示例：把旧库升级到 v1
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
        """关闭当前线程的连接。"""
        conn = getattr(self._local, "connection", None)
        if conn is None:
            return
        try:
            # noinspection unresolved-references
            conn.close()
        except Exception as e:
            self.logger.warning("Close error: %s", e)
        finally:
            self._local.connection = None
            with self._connections_lock:
                try:
                    # noinspection bad-argument-type
                    self._connections.remove(conn)
                except ValueError:
                    pass
            self.logger.debug("Closed connection for thread %s",
                              threading.current_thread().name)

    def close_all(self):
        """
        关闭本 Connector 追踪的所有连接。

        依赖 check_same_thread=False，可以从任意线程调用。
        调用方应保证各 worker 线程已停止使用连接。
        调用后当前线程的 _local 引用也会清空；其他线程的引用
        会在它们下次 connect() 时由存活校验自动重建。
        """
        with self._connections_lock:
            count = len(self._connections)
            for conn in list(self._connections):
                try:
                    conn.close()
                except Exception as e:
                    self.logger.warning("Close error: %s", e)
            self._connections.clear()

        # 修复②：清掉当前线程的 local 引用，避免复用到已关闭连接
        self._local.connection = None

        self.logger.info("Closed all connections, total %d", count)

    # ------------------------------------------------------------- context mgr
    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 不关闭，保留给同线程复用
        return False
