#
"""时间字符串契约。

全项目落库 / 跨模块传递的时间统一为同一种格式::

    YYYY-MM-DDTHH:MM:SS.sss     （UTC，固定 3 位毫秒，无时区后缀）

选择该格式的原因：

* 与 schema 默认值 ``strftime('%Y-%m-%dT%H:%M:%f','now')`` **完全同构**
  （SQLite 的 ``%f`` 是 3 位毫秒），因此 SQL 内生成的值与 Python 生成的
  值可以安全地做字符串比较 / 排序；
* 定长、无时区后缀，字典序 == 时间序，``deleted_at <= ?`` 这类比较成立；
* 若混用 ``datetime.isoformat()``（带 ``+00:00`` 且 6 位微秒），边界处会
  出现「不报错但比较结果偏了」的隐性 bug——所以禁止在落库路径上直接
  ``isoformat()``，统一走本模块。

用法::

    from albuswall.utils.time import now_str, cutoff_str

    deleted_at = now_str()                       # 当前 UTC 时间
    cutoff = cutoff_str(timedelta(days=30))      # 30 天前的同一格式
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = ["cutoff_str", "format_iso", "now_str", "parse_iso"]

# Python 侧格式化模板；``%f`` 是微秒，配合 ``format_iso`` 截断到毫秒。
_ISO_MS_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def format_iso(dt: datetime) -> str:
    """把 ``datetime`` 转成统一落库格式（UTC + 3 位毫秒）。

    无时区的 ``dt`` 视为 UTC（与 schema 的 ``strftime(...,'now')`` 一致）。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc = dt.astimezone(timezone.utc)
    # %f 输出 6 位微秒，截断到 3 位毫秒，与 SQLite 的 %f 对齐
    return utc.strftime(_ISO_MS_FORMAT)[:-3]


def now_str() -> str:
    """当前 UTC 时间的落库格式字符串。"""
    return format_iso(datetime.now(timezone.utc))


def cutoff_str(delta: timedelta) -> str:
    """``now - delta`` 的落库格式字符串（用于 ``col <= ?`` 的截止时间）。"""
    return format_iso(datetime.now(timezone.utc) - delta)


def parse_iso(raw: str) -> datetime:
    """容错解析：兼容带/不带 ``Z``、带/不带 tzinfo。

    schema 生成的时间串不带 tz，视为 UTC。
    """
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s, _ISO_MS_FORMAT)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
