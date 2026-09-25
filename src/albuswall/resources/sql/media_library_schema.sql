-- =============================================================================
-- AlbusWall 媒体库初始建表脚本（幂等）
-- 创建表、索引、约束、触发器，保证数据完整性和查询性能
--
-- 全局约定：
--   - 所有时间字段均为 TEXT，ISO-8601 格式（UTC），由 SQLite 默认值生成
--   - 所有布尔字段均为 INTEGER，取值 0/1，并用 CHECK 约束兜底
--   - 所有 JSON 字段均为 TEXT，存储序列化后的 JSON 字符串
--   - 除 created_at / modified_at 外，业务可写字段均由应用层维护
--
-- 路径语义（重要）：
--   - ingest_source.source_path：源目录的绝对路径；id=0 的特殊源为空串
--   - assets.file_path         ：相对路径
--       · source_id = 0：POSIX 相对 '/'，如 "home/user/a.jpg"
--                        Windows 带盘符，如 "C:/Users/a.jpg"
--       · 其他 source_id：相对 ingest_source.source_path
--   - assets.thumb_path        ：缩略图主目录的**绝对路径**（服务层落盘时拼出的 thumb_root/uuid/version）
--   - assets.thumb_*_path      ：**相对 thumb_path** 的 spec 文件名/子路径
--       完整路径 = thumb_path.rstrip('/') + '/' + thumb_<spec>_path
--   - asset_candidate_cache.path：相对路径，相对 ingest_source.source_path
-- =============================================================================

-- 启用 WAL 模式（持久化，仅需执行一次；重复执行无害）
PRAGMA journal_mode = WAL;

-- 启用外键约束，代码会二次调用开启，虽然这是幂等的
PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 1. 导入源配置表（素材摄取任务）
--    注意：此表需在 assets 之前创建，因为 assets 引用其主键
--    特殊源：
--      id=0 为「手动导入 / 虚拟根」，禁止删除；其 source_path 为空串
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_source (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    title                   TEXT    NOT NULL,                    -- 显示名称
    description             TEXT,                                -- 备注说明，可空
    source_path             TEXT    NOT NULL,                    -- 源目录绝对路径；特殊源为空串
    target_path             TEXT,                                -- 处理后落盘目录，可空（沿用默认）
    mount_point             TEXT,                                -- 挂载点，可空（非挂载源为空）
    auto_mount              INTEGER NOT NULL DEFAULT 0,          -- 0/1 布尔：是否自动挂载
    file_type_check         TEXT    NOT NULL DEFAULT 'suffix',   -- 'suffix' 按扩展名 / 'magic' 按文件头
    file_types              TEXT    NOT NULL DEFAULT '[]',       -- JSON 数组字符串，如 '["jpg","png"]'
    tags                    TEXT    NOT NULL DEFAULT '[]',       -- JSON 数组字符串，用户标签
    subfolder_recursion     INTEGER NOT NULL DEFAULT 0,          -- 0/1 布尔：是否递归子目录
    subfolder_recursion_depth INTEGER,                           -- 递归深度；NULL 表示不限
    trigger_config          TEXT,                                -- JSON 对象，见下方结构示例
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    modified_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    CONSTRAINT chk_file_type_check CHECK (file_type_check IN ('suffix', 'magic')),
    CONSTRAINT chk_auto_mount_bool CHECK (auto_mount IN (0,1)),
    CONSTRAINT chk_subfolder_recursion_bool CHECK (subfolder_recursion IN (0,1))
);

-- 特殊源：id = 0，虚拟根 / 手动导入入口
-- 使用 INSERT OR IGNORE 保证幂等；重复执行不会覆盖已有数据
INSERT OR IGNORE INTO ingest_source (
    id, title, description, source_path, target_path, mount_point,
    auto_mount, file_type_check, file_types, tags,
    subfolder_recursion, subfolder_recursion_depth, trigger_config
) VALUES (
    0,
    '__manual__',
    '手动导入 / 虚拟根；file_path 语义见应用层',
    '',
    NULL, NULL, 0, 'suffix', '[]', '[]', 0, NULL,
    '{"update_mode":"manual"}'
);

-- trigger_config JSON 结构示例
-- {
--   "update_mode": "scheduled_time",
--   // 可选值：
--   //   scheduled_time —— 每天定时触发，使用 scheduled.time
--   //   interval_time  —— 按固定间隔触发，使用 scheduled.interval
--   //   device_trigger —— 设备接入时触发，使用 device_trigger.enabled
--   //   manual         —— 仅手动触发，忽略其他字段
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
--    file_path 语义见文件头「路径语义」小节
--    软删除约定：
--      - is_deleted=1 表示逻辑删除，deleted_at 记录删除时间
--      - 唯一索引均带 WHERE is_deleted=0，允许同一 hash 在删除后重新导入
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid             TEXT    NOT NULL UNIQUE,          -- 全局唯一标识，跨库同步用
    file_path        TEXT    NOT NULL,                 -- 相对路径，语义见文件头
    source_id        INTEGER NOT NULL DEFAULT 0,       -- 关联导入源；0 为特殊源，见 ingest_source

    -- 缩略图：base 为绝对主目录，spec 为相对 base 的子路径
    thumb_path        TEXT,                            -- 缩略图主目录，绝对路径
    thumb_small_path  TEXT,                            -- 相对 thumb_path
    thumb_medium_path TEXT,                            -- 相对 thumb_path
    thumb_large_path  TEXT,                            -- 相对 thumb_path

    original_name    TEXT    NOT NULL,                 -- 原始文件名（含扩展名）
    mime_type        TEXT    NOT NULL,                 -- MIME 类型，如 image/jpeg
    file_hash        TEXT    NOT NULL,                 -- 内容哈希，用于去重
    file_size        INTEGER NOT NULL DEFAULT 0,       -- 字节数
    width            INTEGER NOT NULL DEFAULT 0,       -- 图像宽度（像素）
    height           INTEGER NOT NULL DEFAULT 0,       -- 图像高度（像素）
    taken_at         TEXT,                             -- 拍摄时间，来自 EXIF；可空
    city             TEXT,                             -- 拍摄城市，来自 EXIF/地理信息；可空
    exif_json        TEXT,                             -- 完整 EXIF JSON 字符串
    is_favorite      INTEGER NOT NULL DEFAULT 0,       -- 0/1 布尔：收藏
    is_deleted       INTEGER NOT NULL DEFAULT 0,       -- 0/1 布尔：软删除
    deleted_at       TEXT,                             -- 软删除时间；is_deleted=0 时为 NULL
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    modified_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),

    CONSTRAINT chk_assets_favorite_bool CHECK (is_favorite IN (0,1)),
    CONSTRAINT chk_assets_deleted_bool  CHECK (is_deleted  IN (0,1)),
    FOREIGN KEY (source_id) REFERENCES ingest_source(id) ON DELETE RESTRICT
);

-- -----------------------------------------------------------------------------
-- 3. 相簿表
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS albums (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid           TEXT    NOT NULL UNIQUE,       -- 全局唯一标识
    title          TEXT    NOT NULL,              -- 相簿名称
    album_type     INTEGER NOT NULL DEFAULT 0,    -- 类型：0=普通，可扩展自定义
    cover_asset_id INTEGER,                       -- 封面资产；可空
    description    TEXT    NOT NULL DEFAULT '',   -- 相簿描述
    sort_order     INTEGER NOT NULL DEFAULT 0,    -- 排序权重，越小越靠前
    is_deleted     INTEGER NOT NULL DEFAULT 0,    -- 0/1 布尔：软删除
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    modified_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    CONSTRAINT chk_albums_deleted_bool CHECK (is_deleted IN (0,1)),
    FOREIGN KEY (cover_asset_id) REFERENCES assets(id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- 4. 相簿-资产关联表（多对多）
--    asset_taken_at 冗余存储拍摄时间，避免列表排序时回表 assets
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS album_assets (
    album_id       INTEGER NOT NULL,               -- 所属相簿
    asset_id       INTEGER NOT NULL,               -- 关联资产
    asset_taken_at TEXT,                           -- 冗余：资产拍摄时间
    added_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),  -- 加入时间
    sort_order     INTEGER NOT NULL DEFAULT 0,     -- 相簿内手动排序
    PRIMARY KEY (album_id, asset_id),
    FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE,
    FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- 5. 资产候选缓存表（存放待二次处理的文件，处理完成后可清理）
--    语义：
--      - path  为相对路径，相对于 ingest_source.source_path
--      - uuid  为候选资产的唯一标识，与 assets.uuid 无关联
--      - status 流转：pending → processing → done/failed/skipped
--      - claimed_by / claimed_at 用于多 worker 抢占，避免重复处理
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_candidate_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid        TEXT    NOT NULL UNIQUE,                -- 候选资产唯一标识
    path        TEXT    NOT NULL,                       -- 相对路径，相对 source_path
    source_id   INTEGER NOT NULL,                       -- 所属导入源
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    mime_type   TEXT    NOT NULL DEFAULT 'application/octet-stream',  -- 预判 MIME
    status      TEXT    NOT NULL DEFAULT 'pending',     -- 见上方状态流转
    claimed_by  TEXT,                                   -- 抢占者标识（worker id）
    claimed_at  TEXT,                                   -- 抢占时间
    CONSTRAINT chk_candidate_status CHECK (
        status IN ('pending','processing','done','failed','skipped')
    ),
    FOREIGN KEY (source_id) REFERENCES ingest_source(id) ON DELETE CASCADE
);

-- =============================================================================
-- 索引 —— 覆盖所有常用查询与排序，确保大数据量下的性能
-- =============================================================================

-- 活跃资产哈希唯一，防止重复导入（软删除后允许重新导入同一 hash）
CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_active_hash
    ON assets(file_hash) WHERE is_deleted = 0;

-- 同一源内活跃资产路径唯一，防止重复导入同一文件
CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_source_file_path_active
    ON assets(source_id, file_path) WHERE is_deleted = 0;

-- 按导入源查询资产
CREATE INDEX IF NOT EXISTS idx_assets_source_id ON assets(source_id);

-- 按拍摄时间排序（活跃资产）
CREATE INDEX IF NOT EXISTS idx_assets_taken_at
    ON assets(taken_at) WHERE is_deleted = 0;

-- 收藏过滤（partial：只索引收藏项，体积更小）
CREATE INDEX IF NOT EXISTS idx_assets_favorite
    ON assets(is_favorite) WHERE is_favorite = 1;

-- 已删除过滤（partial）
CREATE INDEX IF NOT EXISTS idx_assets_deleted
    ON assets(is_deleted) WHERE is_deleted = 1;

-- 缩略图待生成任务扫描：按 modified_at 排序，只命中缺缩略图的活跃资产
CREATE INDEX IF NOT EXISTS idx_assets_thumb_pending
    ON assets(modified_at)
    WHERE is_deleted = 0
      AND (thumb_path        IS NULL
        OR thumb_small_path  IS NULL
        OR thumb_medium_path IS NULL
        OR thumb_large_path  IS NULL);

-- 相簿封面反查（删除资产时置 NULL 用）
CREATE INDEX IF NOT EXISTS idx_albums_cover_asset_id ON albums(cover_asset_id);

-- 相簿 UUID 快速查找：uuid 已有 UNIQUE 约束，SQLite 会自动建索引，无需显式创建

-- 专辑内资产反查（按资产找相簿）
CREATE INDEX IF NOT EXISTS idx_album_assets_asset ON album_assets(asset_id);

-- 相簿资产查询的各种排序
CREATE INDEX IF NOT EXISTS idx_album_assets_added ON album_assets(album_id, added_at);
CREATE INDEX IF NOT EXISTS idx_album_assets_sort  ON album_assets(album_id, sort_order, asset_id);
CREATE INDEX IF NOT EXISTS idx_album_assets_taken ON album_assets(album_id, asset_taken_at);

-- ingest_source 常用查询
CREATE INDEX IF NOT EXISTS idx_ingest_source_title       ON ingest_source(title);
CREATE INDEX IF NOT EXISTS idx_ingest_source_source_path ON ingest_source(source_path);

-- asset_candidate_cache：
--   按 source 过滤候选列表
CREATE INDEX IF NOT EXISTS idx_asset_candidate_cache_source
    ON asset_candidate_cache(source_id);

--   worker 抢占待处理任务
CREATE INDEX IF NOT EXISTS idx_candidate_pending
    ON asset_candidate_cache(status, source_id);

--   同一源内路径唯一，防止重复入队
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_source_path
    ON asset_candidate_cache(source_id, path);

-- =============================================================================
-- modified_at 自动更新触发器
--   仅当应用层未显式修改 modified_at（NEW == OLD）时，才由触发器刷新为当前时间
--   注意：recursive_triggers 默认关闭；应用层若开启需自行评估递归风险
-- =============================================================================

CREATE TRIGGER IF NOT EXISTS trg_ingest_source_modified_at
AFTER UPDATE ON ingest_source FOR EACH ROW
WHEN NEW.modified_at = OLD.modified_at
BEGIN
    UPDATE ingest_source SET modified_at = strftime('%Y-%m-%dT%H:%M:%f','now')
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_assets_modified_at
AFTER UPDATE ON assets FOR EACH ROW
WHEN NEW.modified_at = OLD.modified_at
BEGIN
    UPDATE assets SET modified_at = strftime('%Y-%m-%dT%H:%M:%f','now')
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_albums_modified_at
AFTER UPDATE ON albums FOR EACH ROW
WHEN NEW.modified_at = OLD.modified_at
BEGIN
    UPDATE albums SET modified_at = strftime('%Y-%m-%dT%H:%M:%f','now')
    WHERE id = NEW.id;
END;
