#
"""``utils.serialize`` 投影层的最小单元测试。

只测本批新增的 ``missing_thumbnail_row_to_dict``（其余 6 个已在批次 A
逐个人工验证；这里补上「有没有漏字段」的程序化断言，避免以后加字段时
静默漏投影）。
"""

from __future__ import annotations

import json
import uuid

from conftest import make_missing_row

from albuswall.dto.thumbnail.query import MissingThumbnailRow
from albuswall.utils.serialize import missing_thumbnail_row_to_dict


def test_missing_row_uuid_and_nested_paths_are_json_safe():
    row_uuid = uuid.uuid4()
    row = make_missing_row(uuid=row_uuid, source_id=None)

    result = missing_thumbnail_row_to_dict(row)

    assert json.dumps(result)                        # 不抛 TypeError
    assert result["uuid"] == str(row_uuid)           # UUID → str
    assert result["source_id"] is None               # Optional 的 None 分支
    assert isinstance(result["paths"], dict)         # 嵌套 dataclass 已递归
    assert set(result["paths"]) == {"base", "small", "medium", "large"}


def test_missing_row_covers_every_dto_field():
    """字段集合与 DTO 完全一致（新字段出现而忘了投影会在这里炸）。"""
    from dataclasses import fields

    result = missing_thumbnail_row_to_dict(make_missing_row())
    assert set(result) == {f.name for f in fields(MissingThumbnailRow)}
