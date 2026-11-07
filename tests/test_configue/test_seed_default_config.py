#
"""``seed_defaults``：配置目录缺文件时复制随包默认配置。

语义（写死在测试里，改实现前先看这里）：

* 只在**文件不存在**时复制，已存在绝不覆盖（用户改过的值不能丢）；
* 主配置模板是随包 ``resources/config/conf.ini``，落成调用方给的文件名；
* 同目录的 ``log_config.ini`` 一起补齐（否则 ``setup_log`` 只能退回 stderr）；
* 用户快捷键覆盖表（``shortcuts.ini``）**不**复制：随包默认表是只读兜底；
* ``seed_defaults=False``（默认）保持旧行为：缺文件、``ignore_missing=False``
  时照旧在 ``load()`` 里报 ``ConfigError``（"config file not found"）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue.bootstrap import seed_default_config, setup_config
from albuswall.configue.errors import ConfigError
from albuswall.core import Container


def test_seed_copies_bundled_defaults(tmp_path: Path) -> None:
    setup_config(
        Container(),
        base_dir=tmp_path,
        file_name="config.ini",
        seed_defaults=True,
        ignore_missing=False,
    )

    config_file = tmp_path / "config.ini"
    assert config_file.is_file()
    text = config_file.read_text(encoding="utf-8")
    assert "[path]" in text and "[log]" in text
    # 落下的就是随包模板本身（不是手写的子集）
    from albuswall.resources import get

    assert text == get("default_config").read_text(encoding="utf-8")
    assert (tmp_path / "log_config.ini").is_file()
    # 快捷键用户表不复制
    assert not (tmp_path / "shortcuts.ini").exists()


def test_seed_is_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    config_file = tmp_path / "config.ini"
    config_file.write_text("[ui]\ntheme = custom\n", encoding="utf-8")

    written = seed_default_config(tmp_path, "config.ini")

    # 已存在的主配置不在写入列表里；只有缺失的日志配置被补上
    assert [path.name for path in written] == ["log_config.ini"]
    assert config_file.read_text(encoding="utf-8") == "[ui]\ntheme = custom\n"


def test_seeded_config_is_loadable(tmp_path: Path) -> None:
    config = setup_config(
        Container(),
        base_dir=tmp_path,
        file_name="config.ini",
        seed_defaults=True,
        ignore_missing=False,
    )
    assert config.resolver.get_typed("ui", "theme") == "default"
    assert config.resolver.get_typed("files", "log") == "log_config.ini"


def test_seed_disabled_still_raises_on_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        setup_config(
            Container(),
            base_dir=tmp_path,
            file_name="config.ini",
            ignore_missing=False,
        )
    assert list(tmp_path.iterdir()) == []
