#
"""
Thread-safe SQLite database connector. Each thread maintains its own connection.
"""

import sqlite3
import logging
import threading
from pathlib import Path
# from importlib import resources
from typing import Optional

import albuswall
from albuswall.log import TRACE, Logger
from albuswall.resources import schema

__all__ = [
    "DEFAULT_SCHEMA_FILE",
    "Connector"
]

# DEFAULT_SCHEMA_FILE = (
#         resources.files('albuswall.resources') /
#         'sql' /
#         'media_library_schema.sql'
# )
DEFAULT_SCHEMA_FILE = schema

MEMORY_DB_PATH = ":memory:"

PRAGMA_FOREIGN_KEYS_ON = "PRAGMA foreign_keys = ON"
PRAGMA_JOURNAL_WAL = "PRAGMA journal_mode=WAL"

CHECK_ASSETS_TABLE_SQL = (
    "SELECT name FROM sqlite_master "
    "WHERE type='table' AND name='assets'"
)

_package_name = __name__.split('.', 2)[-1] if '.' in __name__ else __name__


class Connector:
    """
    Thread-safe SQLite database access layer. Each thread maintains its own connection.

    Parameters
    ----------
    db_path : str or None
        Database file path. If None, an in-memory database (':memory:') will be used automatically on connect.
    schema_file : Traversable, optional
        Resource object containing the table creation SQL. Defaults to the built-in resource.
    must_exist : bool, optional
        If True, and db_path points to a file that does not exist, the first connection attempt
        will raise FileNotFoundError. Defaults to False (allows automatic creation of new files).

     Usage:
        with db as Conn:
            conn.execute( "...")
    """
    logger = logging.getLogger(f"{albuswall.__name__}.{_package_name}")
    logger: Logger
    logger.trace = lambda msg, *args, **kwargs: Connector.logger.log(TRACE, msg, *args, **kwargs)

    def __init__(self, db_path: Optional[str] = None,
                 schema_file=None,
                 must_exist: bool = False):
        # Store original parameters only; no validation or conversion is performed here.
        self.db_path: Optional[str] = db_path
        self._must_exist = must_exist
        self._schema_file = schema_file or DEFAULT_SCHEMA_FILE
        self._schema_sql = self._load_schema(self._schema_file)

        # Thread safety and connection management (variables are only declared in __init__).
        self._init_lock = threading.Lock()
        self._initialized = False
        self._local = threading.local()
        self._connections_lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []

        self.logger.debug("DB Connector init completed, db_path=%s, must_exist=%s",
                          self.db_path, self._must_exist)

    @staticmethod
    def _load_schema(schema_resource):
        """Load SQL text from an importlib.resources resource."""
        try:
            return schema_resource.read_text(encoding='utf-8')
        except AttributeError:
            # Fallback for very old versions
            with open(str(schema_resource), 'r', encoding='utf-8') as f:
                return f.read()

    def _resolve_db_path(self) -> str:
        """
        Return the actual path string used for connection based on the original db_path.
        - None -> ':memory:'
        - Non-empty string -> resolved to an absolute path
        """
        if self.db_path is None:
            self.logger.info("No database file specified; using in-memory database '%s'", MEMORY_DB_PATH)
            return MEMORY_DB_PATH
        # Convert to absolute path to ensure the same file is used even if the working directory changes.
        absolute = Path(self.db_path).resolve()
        self.logger.info("Database path resolved to: %s", absolute)
        return str(absolute)

    def _check_file_must_exist(self, resolved_path: str):
        """
        If must_exist=True and not an in-memory database, check whether the file exists.
        """
        if self._must_exist and resolved_path != MEMORY_DB_PATH:
            path = Path(resolved_path)
            if not path.exists():
                raise FileNotFoundError(f"Database file does not exist and must_exist=True: {resolved_path}")
            self.logger.debug("Database file existence check passed: %s", resolved_path)

    def connect(self) -> sqlite3.Connection:
        """Get the current thread's database connection (created lazily)."""
        if not hasattr(self._local, "connection") or self._local.connection is None:
            self.logger.debug("Creating new connection for thread %s", threading.current_thread().name)
            conn = self._create_connection()
            self._local.connection = conn
            with self._connections_lock:
                self._connections.append(conn)
        else:
            self.logger.log(TRACE, "Reusing existing connection for thread %s", threading.current_thread().name)
        return self._local.connection

    def _create_connection(self) -> sqlite3.Connection:
        """
        Initialize a new connection for the current thread, including:
        - Path resolution and validation
        - Parent directory creation (if needed)
        - Database connection and pragma settings
        - One-time DDL initialization across all threads
        """
        # 1. Resolve the final database path
        resolved = self._resolve_db_path()

        # 2. Check must_exist (whether the file actually exists)
        self._check_file_must_exist(resolved)

        # 3. Ensure parent directory exists for non-memory databases
        if resolved != MEMORY_DB_PATH:
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
            self.logger.debug("Database parent directory ensured to exist: %s", Path(resolved).parent)

        # 4. Establish connection and configure parameters
        conn = sqlite3.connect(resolved)
        conn.row_factory = sqlite3.Row
        conn.execute(PRAGMA_FOREIGN_KEYS_ON)
        conn.execute(PRAGMA_JOURNAL_WAL)
        self.logger.debug("New connection created, path: %s, foreign keys enabled, journal mode: WAL", resolved)

        # 5. One-time DDL initialization across all threads
        if not self._initialized:
            with self._init_lock:
                if not self._initialized:  # Double-check
                    table_exists = conn.execute(CHECK_ASSETS_TABLE_SQL).fetchone()
                    if not table_exists:
                        self.logger.info("First-time database creation; executing schema ...")
                        conn.executescript(self._schema_sql)
                        self.logger.info("Schema creation completed")
                    else:
                        self.logger.debug("Database already exists; applying idempotent schema updates")
                        # 执行仅包含 CREATE TABLE IF NOT EXISTS 和 CREATE INDEX IF NOT EXISTS 的部分
                        # 或者拆分 schema 文件，这里直接手动执行条件 ALTER
                        self._add_column_if_not_exists(
                            conn, 'asset_candidate_cache', 'status', "TEXT NOT NULL DEFAULT 'pending'"
                        )
                        self._add_column_if_not_exists(
                            conn, 'asset_candidate_cache', 'claimed_by', "TEXT"
                        )
                        self._add_column_if_not_exists(
                            conn, 'asset_candidate_cache', 'claimed_at', "TEXT"
                        )
                        # 执行索引创建（幂等）
                        conn.executescript("""
                            CREATE INDEX IF NOT EXISTS idx_asset_candidate_cache_source ON asset_candidate_cache(source_id);
                            CREATE INDEX IF NOT EXISTS idx_candidate_pending ON asset_candidate_cache(status, source_id);
                            -- 其他索引也可以加在这里，或者保留原有 schema 中的索引部分
                        """)
                    self._initialized = True

        return conn

    def _add_column_if_not_exists(self, conn, table, column_name, column_definition):
        """Add a column to a table if it doesn't already exist."""
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_definition}")
            self.logger.debug("Added column %s to table %s", column_name, table)

    def close(self):
        """Close the current thread's connection."""
        if hasattr(self._local, "connection") and self._local.connection is not None:
            conn = self._local.connection
            conn.close()
            self._local.connection = None
            with self._connections_lock:
                if conn in self._connections:
                    self._connections.remove(conn)
            self.logger.debug("Connection for thread %s has been closed", threading.current_thread().name)

    def close_all(self):
        """Close all threads' database connections (typically called at program exit)."""
        is_prompted = False # is has already been prompted

        with self._connections_lock:
            count = len(self._connections)
            for conn in list(self._connections):
                try:
                    conn.close()
                except sqlite3.ProgrammingError as e:
                    # 仅对跨线程关闭错误进行降级处理
                    if "SQLite objects created in a thread can only be used in that same thread" in str(e):
                        if not is_prompted:
                            is_prompted = True
                            self.logger.debug("This is normal when closing, please ignore it.")
                        self.logger.debug("Ignoring cross-thread close error: %s", e)
                    else:
                        self.logger.warning("Exception while closing connection: %s", e)
                except Exception as e:
                    self.logger.warning("Exception while closing connection: %s", e)
            self._connections.clear()
        self.logger.info("Closed all threads' database connections, total %d", count)

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Do not close the connection on exit, keep it for reuse by the thread.
        return False
