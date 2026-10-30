#
"""``optional`` / ``ignore_missing``：配置文件可以不存在。

语义（见 ``registry.FileInfo`` / ``Configue.load``）：

- ``optional=True`` 只豁免「文件不存在」；歧义（多候选）照常报错；
- 缺失时以 ``base_dir / file_path`` 作占位 ``resolved_path`` 标成已加载，
  默认值照常注入，首次 ``save()`` 会把文件创建出来；
- ``reg(exists_check=True, optional=True)`` 语义冲突 → ``ValueError``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue import (
    ConfigError,
    ConfigFile,
    ConfigMissingError,
    Configue,
    configclass,
    field,
    reg,
)
from albuswall.configue.bootstrap import setup_config
from albuswall.configue.registry import _FILES
from albuswall.core import Container


@pytest.fixture
def container() -> Container:
    return Container()


def _setup_optional(tmp_path: Path, container: Container, **kwargs):
    kwargs.setdefault("portable", True)  # 让 [path] 落在 tmp_path，断言与 base_dir 对齐
    return setup_config(
        container, base_dir=tmp_path, ignore_missing=True, **kwargs,
    )


# ==================== reg() 参数冲突 ====================

def test_reg_exists_check_and_optional_conflict(tmp_path):
    Configue(tmp_path)
    with pytest.raises(ValueError, match="mutually exclusive"):
        reg(
            ConfigFile.INI, "config.ini", name="conflict",
            exists_check=True, optional=True,
        )
    assert "conflict" not in _FILES


def test_reg_optional_is_recorded(tmp_path):
    reg(ConfigFile.INI, "maybe.ini", name="maybe", optional=True)
    assert _FILES["maybe"].optional is True

    reg(ConfigFile.INI, "strict.ini", name="strict")
    assert _FILES["strict"].optional is False


# ==================== 缺文件 ====================

def test_ignore_missing_no_file(tmp_path, container):
    config = _setup_optional(tmp_path, container)

    assert isinstance(config, Configue)
    assert _FILES["main"].loaded is True                       # 标成已加载
    expected = (tmp_path / "config.ini").resolve()
    assert _FILES["main"].resolved_path == expected            # 占位路径
    assert config.base_dir == tmp_path.resolve()

    # 默认值照常可读
    assert config.tree.main.path.config == tmp_path.resolve()
    assert config.tree.main.files.db == "db.db"
    assert config.check().ok is True                           # check 也不报错


def test_ignore_missing_false_raises(tmp_path, container):
    """默认行为不变：缺文件仍是硬错误。"""
    with pytest.raises(ConfigError, match="config file not found"):
        setup_config(container, base_dir=tmp_path)  # ignore_missing=False 默认


def test_ignore_missing_declared_field_defaults(tmp_path, container):
    @configclass("core", "main")
    class Core:
        port: int = field(default=8080)
        name: str = field(default="dflt")

    _setup_optional(tmp_path, container)

    assert Core().port == 8080
    assert Core().name == "dflt"


def test_ignore_missing_without_default_raises_config_missing(tmp_path, container):
    """文件都没有，声明了但没默认值的字段仍然读不到（同「文件存在但没写」）。"""
    @configclass("core2", "main")
    class NeedsValue:
        required: str

    _setup_optional(tmp_path, container)

    with pytest.raises(ConfigMissingError):
        _ = NeedsValue().required


# ==================== 写回创建文件 ====================

def test_ignore_missing_save_creates_file(tmp_path, container):
    config = _setup_optional(tmp_path, container)
    target = tmp_path / "config.ini"
    assert not target.exists()

    config.tree.main.files.db = "custom.db"
    written = config.tree.main.save()

    assert written == [target.resolve()]
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "db = custom.db" in text
    assert not text.startswith("\n"), "新建文件不该有前导空行"
    assert text.startswith("[files]")


def test_ignore_missing_class_view_write_creates_file(tmp_path, container):
    @configclass("core", "main")
    class Core:
        port: int = field(default=1)

    config = _setup_optional(tmp_path, container)

    Core().port = 4321
    config.flush()

    assert "port = 4321" in (tmp_path / "config.ini").read_text(encoding="utf-8")


def test_ignore_missing_load_after_unload_reuses_placeholder(tmp_path, container):
    config = _setup_optional(tmp_path, container)
    config.unload("main")

    assert _FILES["main"].loaded is False
    assert _FILES["main"].resolved_path is None

    report = config.load()

    assert report.ok is True
    assert _FILES["main"].loaded is True
    assert _FILES["main"].resolved_path == (tmp_path / "config.ini").resolve()


def test_ignore_missing_file_appears_later(tmp_path, container):
    """首次以 optional 加载（文件不存在），之后文件出现再 load 读到真实值。"""
    config = _setup_optional(tmp_path, container)
    assert config.tree.main.files.db == "db.db"

    (tmp_path / "config.ini").write_text(
        "[files]\ndb = late.db\n", encoding="utf-8",
    )
    config.unload("main")
    config.load()

    assert config.tree.main.files.db == "late.db"


# ==================== optional 不隐藏读盘错误 ====================

def test_ignore_missing_does_not_hide_ini_read_error(tmp_path):
    """文件存在但 INI 格式坏 → read_failed，不被 optional 豁免。"""
    bad = tmp_path / "config.ini"
    bad.write_text("[core\ninvalid_no_close", encoding="utf-8")
    c = Configue(tmp_path)
    c.reg(ConfigFile.INI, "config.ini", name="main", optional=True)

    report = c.load(partial=True)

    assert not report.ok
    assert any(e.kind == "read_failed" for e in report.errors)
    assert _FILES["main"].loaded is False          # 读失败不标成已加载


def test_ignore_missing_does_not_hide_json_read_error(tmp_path):
    """文件存在但 JSON 坏 → read_failed，不被 optional 豁免。"""
    bad = tmp_path / "state.json"
    bad.write_text("{not json", encoding="utf-8")
    c = Configue(tmp_path)
    c.reg(ConfigFile.JSON, "state.json", name="state", optional=True)

    report = c.load(partial=True)

    assert not report.ok
    assert any(e.kind == "read_failed" for e in report.errors)
    assert _FILES["state"].loaded is False


# ==================== 文件存在 ====================

def test_ignore_missing_file_exists(tmp_path, container):
    (tmp_path / "config.ini").write_text(
        "[files]\ndb = existing.db\n", encoding="utf-8",
    )

    config = _setup_optional(tmp_path, container)

    assert config.tree.main.files.db == "existing.db"
    assert _FILES["main"].resolved_path == (tmp_path / "config.ini").resolve()


# ==================== 歧义不受豁免 ====================

def test_ignore_missing_ambiguous_still_errors(tmp_path, container):
    (tmp_path / "amb.conf").write_text("[files]\n", encoding="utf-8")
    (tmp_path / "amb.conf.ini").write_text("[files]\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="ambiguous"):
        setup_config(
            container, base_dir=tmp_path, file_name="amb.conf",
            ignore_missing=True, register_aliases=False,
        )


def test_check_optional_ambiguous_still_reports(tmp_path, container):
    (tmp_path / "amb.conf").write_text("[files]\n", encoding="utf-8")
    (tmp_path / "amb.conf.ini").write_text("[files]\n", encoding="utf-8")
    reg(ConfigFile.INI, "amb.conf", name="amb", optional=True)

    report = Configue(tmp_path).check()

    assert report.ok is False
    assert report.errors[0].kind == "ambiguous_file"
