-- =============================================================================
-- AlbusWall 媒体库初始建表脚本
-- 创建表、索引、约束，保证数据完整性和查询性能
-- =============================================================================

-- 启用 WAL 模式（持久化，仅需执行一次；重复执行无害）
PRAGMA journal_mode = WAL;

-- 启用外键约束
PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 1. 导入源配置表（素材摄取任务）
--    注意：此表需在 assets 之前创建，因为 assets 引用其主键
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_source (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    title                   TEXT    NOT NULL,
    description             TEXT,
    source_path             TEXT    NOT NULL,
    target_path             TEXT,
    mount_point             TEXT,
    auto_mount              INTEGER NOT NULL DEFAULT 0,          -- 0/1 布尔
    file_type_check         TEXT    NOT NULL DEFAULT 'suffix',   -- 'suffix' 或 'magic'
    file_types              TEXT    NOT NULL DEFAULT '[]',       -- JSON 数组字符串，如 '["jpg","png"]'
    tags                    TEXT    NOT NULL DEFAULT '[]',       -- JSON 数组字符串
    subfolder_recursion     INTEGER NOT NULL DEFAULT 0,          -- 0/1 布尔
    subfolder_recursion_depth INTEGER,
    trigger_config          TEXT,                                -- JSON 对象，包含 update_mode、device_trigger、scheduled
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    modified_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    CONSTRAINT chk_file_type_check CHECK (file_type_check IN ('suffix', 'magic')),
    CONSTRAINT chk_auto_mount_bool CHECK (auto_mount IN (0,1)),
    CONSTRAINT chk_subfolder_recursion_bool CHECK (subfolder_recursion IN (0,1))
);

-- trigger_config JSON 结构示例
-- {
--   "update_mode": "scheduled_time",
-- // 可选值：scheduled_time、interval_time、device_trigger、manual
--   "device_trigger": {
--     "enabled": true
--   },
--   "scheduled": {
--     "enabled": false,
--     "time": null,        // 当 update_mode=scheduled_time 时非空，如 "02:30"
--     "interval": null     // 当 update_mode=interval_time 时非空，如 "12h"
--   }
-- }

-- -----------------------------------------------------------------------------
-- 2. 资产主表
--    file_path 语义：
--      - 当 source_id 不为空时，存储相对路径（相对于 ingest_source.source_path）
--      - 当 source_id 为空时，存储绝对路径（手动导入等场景）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid             TEXT    NOT NULL UNIQUE,
    file_path        TEXT    NOT NULL,    -- 相对路径，相对于源路径
    source_id        INTEGER NOT NULL,    -- 关联导入源，为file_path的起始路径
    thumb_path       TEXT,
    thumb_small_path TEXT,
    thumb_medium_path TEXT,
    original_name    TEXT    NOT NULL,
    mime_type        TEXT    NOT NULL,
    file_hash        TEXT    NOT NULL,
    file_size        INTEGER NOT NULL DEFAULT 0,
    width            INTEGER NOT NULL DEFAULT 0,
    height           INTEGER NOT NULL DEFAULT 0,
    taken_at         TEXT,
    city             TEXT,
    exif_json        TEXT,
    is_favorite      INTEGER NOT NULL DEFAULT 0,
    is_deleted       INTEGER NOT NULL DEFAULT 0,
    deleted_at       TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    modified_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    FOREIGN KEY (source_id) REFERENCES ingest_source(id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- 3. 相簿表
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS albums (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid           TEXT    NOT NULL UNIQUE,
    title          TEXT    NOT NULL,
    album_type     INTEGER NOT NULL DEFAULT 0,
    cover_asset_id INTEGER,
    description    TEXT    NOT NULL DEFAULT '',
    sort_order     INTEGER NOT NULL DEFAULT 0,
    is_deleted     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    modified_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    FOREIGN KEY (cover_asset_id) REFERENCES assets(id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- 4. 相簿-资产关联表
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS album_assets (
    album_id       INTEGER NOT NULL,
    asset_id       INTEGER NOT NULL,
    asset_taken_at TEXT,
    added_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    sort_order     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (album_id, asset_id),
    FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE,
    FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
);


-- -----------------------------------------------------------------------------
-- 5. 资产候选缓存表（存放待二次处理的文件，处理完成后删除记录）
--    注意：path 为相对路径，相对于 ingest_source.source_path
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_candidate_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid        TEXT    NOT NULL UNIQUE,                -- 候选资产唯一标识
    path        TEXT    NOT NULL,                       -- 绝对路径
    source_id   INTEGER NOT NULL,                       -- 所属导入源
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    mime_type   TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    claimed_by  TEXT,
    claimed_at  TEXT,
    FOREIGN KEY (source_id) REFERENCES ingest_source(id) ON DELETE CASCADE
);

-- 候选缓存表索引
CREATE INDEX IF NOT EXISTS idx_asset_candidate_cache_source ON asset_candidate_cache(source_id);
-- uuid 字段已通过 UNIQUE 约束自动创建索引，无需额外显式索引
-- statue
--ALTER TABLE asset_candidate_cache ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
--
-- 可选索引加速
CREATE INDEX IF NOT EXISTS idx_asset_candidate_cache_source ON asset_candidate_cache(source_id);
CREATE INDEX IF NOT EXISTS idx_candidate_pending ON asset_candidate_cache(status, source_id);

-- =============================================================================
-- 索引 —— 覆盖所有常用查询与排序，确保大数据量下的性能
-- =============================================================================

-- 活跃资产哈希唯一，防止重复导入
CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_active_hash
    ON assets(file_hash) WHERE is_deleted = 0;

-- 常用布尔字段过滤
CREATE INDEX IF NOT EXISTS idx_assets_deleted  ON assets(is_deleted);
CREATE INDEX IF NOT EXISTS idx_assets_favorite ON assets(is_favorite);

-- 按导入源查询资产
CREATE INDEX IF NOT EXISTS idx_assets_source_id ON assets(source_id);

-- 相簿 UUID 快速查找
CREATE INDEX IF NOT EXISTS idx_albums_uuid ON albums(uuid);

-- 专辑内资产关联查询
CREATE INDEX IF NOT EXISTS idx_album_assets_album ON album_assets(album_id, asset_id);

-- 相簿资产查询的各种排序
CREATE INDEX IF NOT EXISTS idx_album_assets_added ON album_assets(album_id, added_at);
CREATE INDEX IF NOT EXISTS idx_album_assets_sort  ON album_assets(album_id, sort_order, asset_id);
CREATE INDEX IF NOT EXISTS idx_album_assets_taken ON album_assets(album_id, asset_taken_at);

-- ingest_source 常用查询
CREATE INDEX IF NOT EXISTS idx_ingest_source_title       ON ingest_source(title);
CREATE INDEX IF NOT EXISTS idx_ingest_source_source_path ON ingest_source(source_path);
