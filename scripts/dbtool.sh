#!/usr/bin/env bash
# dbtool.sh — AlbusWall DB 小工具
set -euo pipefail

DB="${ALBUSWALL_DB:-$HOME/.local/share/albuswall/db.db}"

if [[ ! -f "$DB" ]]; then
    echo "db not found: $DB" >&2
    exit 1
fi

_usage() {
    cat <<'EOF'
用法: dbtool.sh <command> [args]

命令:
  sources                 列出所有源（含软删）
  trashed                 列出软删源
  assets <source_id>      列出某源的 asset 行
  counts                  每个源的 asset 数
  mark <source_id>        标记源为软删
  unmark <source_id>      撤销软删
  purge-db <source_id>    直接删该源的 asset DB 行（危险）
  vacuum                  重建 DB 文件
  sql "<query>"           跑任意 SQL（只读提示）
EOF
}

cmd="${1:-}"; shift || true

case "$cmd" in
    sources)
        sqlite3 -header -column "$DB" \
            "SELECT id, title, substr(source_path,1,40) AS path, is_deleted, deleted_at
             FROM ingest_source ORDER BY id;"
        ;;
    trashed)
        sqlite3 -header -column "$DB" \
            "SELECT id, title, source_path, deleted_at
             FROM ingest_source WHERE is_deleted = 1 AND id != 0 ORDER BY id;"
        ;;
    assets)
        sid="${1:?source_id required}"
        sqlite3 -header -column "$DB" \
            "SELECT id, uuid, substr(file_path,1,60) AS path, is_deleted
             FROM assets WHERE source_id = $sid ORDER BY id;"
        ;;
    counts)
        sqlite3 -header -column "$DB" \
            "SELECT s.id, s.title,
                    COUNT(a.id) AS total,
                    SUM(CASE WHEN a.is_deleted=0 THEN 1 ELSE 0 END) AS live
             FROM ingest_source s
             LEFT JOIN assets a ON a.source_id = s.id
             GROUP BY s.id ORDER BY s.id;"
        ;;
    mark)
        sid="${1:?source_id required}"
        sqlite3 "$DB" \
            "UPDATE ingest_source
                SET is_deleted = 1, deleted_at = datetime('now')
              WHERE id = $sid;"
        echo "source $sid marked deleted"
        ;;
    unmark)
        sid="${1:?source_id required}"
        sqlite3 "$DB" \
            "UPDATE ingest_source
                SET is_deleted = 0, deleted_at = NULL
              WHERE id = $sid;"
        echo "source $sid restored"
        ;;
    purge-db)
        sid="${1:?source_id required}"
        read -rp "delete asset rows for source $sid? [y/N] " a
        [[ "$a" == [yY] ]] || exit 0
        sqlite3 "$DB" "DELETE FROM assets WHERE source_id = $sid;"
        echo "asset rows for source $sid deleted"
        ;;
    vacuum)
        sqlite3 "$DB" "VACUUM;"
        echo "vacuum done"
        ;;
    sql)
        q="${1:?query required}"
        sqlite3 -header -column "$DB" "$q"
        ;;
    *)
        _usage
        exit 1
        ;;
esac