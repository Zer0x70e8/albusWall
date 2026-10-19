#
"""文本归一化工具。"""

from __future__ import annotations

import unicodedata


def normalize_tag_name(name: str) -> str:
    """标签名归一化——契约函数。

    所有写入 ``tags.norm_name`` / ``tag_aliases.norm_name`` 的路径，
    以及所有按 ``norm_name`` 查询的路径，都必须经过此函数。
    任何地方复制一份归一化逻辑，都会让
    ``idx_tags_parent_norm_uq`` 唯一索引部分失效——
    DB 只比较存进去的值，不会发现"语义上重复"。

    规则：Unicode NFKC（全角→半角、兼容字符合并）+ strip + lower。

    注意：
        · 用 ``lower()`` 而非 ``casefold()``：与 schema 注释一致，
          且更贴近用户直觉（``ß`` 不展开成 ``ss``）；
        · 若将来要改规则，需同步写迁移脚本重算 ``tags.norm_name``，
          ``name`` 列保留了原始值，可逆。
    """
    return unicodedata.normalize("NFKC", name).strip().lower()