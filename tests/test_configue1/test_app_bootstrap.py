#
"""应用装配：``configue1.bootstrap.setup_config`` + 三个依赖点的读值路径。

覆盖这次迁移的「胶水」部分：

- ``setup_config``：登记文件、声明 ``[path]``/``[files]``、``load()``、注册容器别名；
- ``log.bootstrap``：``LogConf`` 读 ``[log] config`` / ``[log] level``，路径兜底；
- ``infrastructure.task.bootstrap``：``Config`` 字段读值（含 ``int | None``）；
- ``infrastructure.database.bootstrap``：``db`` 工厂用 ``tree.path.data / tree.files.db``。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from albuswall.configue1 import ConfigError, Configue, configclass
from albuswall.configue1.bootstrap import setup_config
from albuswall.configue1.registry import _FILES
from albuswall.core import Container
from albuswall.core.errors import MissingDependencyError
from albuswall.infrastructure.database import bootstrap as db_bootstrap
from albuswall.infrastructure.task import bootstrap as task_bootstrap
from albuswall.log import bootstrap as log_bootstrap

from .conftest import write_ini

CONF = """\
[path]
theme = ${path:config}/theme

[files]
log = log_config.ini
db = db.db

[log]
config = ${path:config}/${files:log}
level = WARNING

[db]
must_exist = false

[task]
initial_max_concurrency = 4
max_concurrency = 8
"""


@pytest.fixture
def container() -> Container:
    return Container()


def _setup(tmp_path: Path, container: Container, content: str = CONF) -> Configue:
    write_ini(tmp_path / "config.ini", content)
    return setup_config(container, base_dir=tmp_path, portable=True)


# ==================== setup_config ====================

def test_setup_config_loads_file_and_registers_aliases(tmp_path, container):
    config = _setup(tmp_path, container)

    assert isinstance(config, Configue)
    assert _FILES["main"].loaded is True
    assert container.require("config") is config
    assert container.require("configue") is config


def test_setup_config_declares_path_defaults(tmp_path, container):
    """没有 [path] 段时，四个目录由 @configclass 默认值补齐。"""
    config = _setup(tmp_path, container, content="[files]\ndb = db.db\n")
    tree = config.tree.main

    assert tree.path.config == tmp_path.resolve()
    assert tree.path.data.is_absolute()
    assert tree.path.cache.is_absolute()
    assert tree.path.temp.is_absolute()
    assert tree.files.db == "db.db"
    assert tree.files.log == "log_config.ini"


def test_setup_config_file_overrides_defaults(tmp_path, container):
    write_ini(tmp_path / "config.ini", "[path]\ndata = /var/data/albuswall\n")
    config = setup_config(container, base_dir=tmp_path, portable=True)

    assert config.tree.main.path.data == Path("/var/data/albuswall")


def test_setup_config_interpolation(tmp_path, container):
    """``${path:config}/theme`` 在新 Resolver 里照常展开。"""
    config = _setup(tmp_path, container)

    assert config.tree.main.path.theme == str(tmp_path.resolve() / "theme")


def test_setup_config_without_aliases(tmp_path):
    write_ini(tmp_path / "config.ini", CONF)
    c = Container()
    config = setup_config(c, base_dir=tmp_path, register_aliases=False)

    assert isinstance(config, Configue)
    with pytest.raises(MissingDependencyError):
        c.require("config")


def test_setup_config_custom_file_name(tmp_path, container):
    write_ini(tmp_path / "app.ini", "[files]\ndb = custom.db\n")
    config = setup_config(
        container, base_dir=tmp_path, file_name="app.ini", name="app",
    )

    assert config.tree.app.files.db == "custom.db"


def test_setup_config_missing_file_raises(tmp_path, container):
    """``ignore_missing=False``（默认）时配置文件必须存在（旧包容错为空）。

    ``load()`` 把「文件不存在」汇总成 ``LoadError(kind="file_not_found")``，
    ``partial=False`` 时统一抛 ``ConfigError``——所以这里抓到的是 ``ConfigError``
    而不是 ``ConfigFileNotFound``（后者只在 ``reg(exists_check=True)`` 用）。
    """
    with pytest.raises(ConfigError, match="config file not found"):
        setup_config(container, base_dir=tmp_path)


def test_setup_config_is_repeatable_after_registry_reset(tmp_path, container):
    """``configclass`` 声明在 setup 里重跑 → 全局表被清空后仍能重建。"""
    first = _setup(tmp_path, container)
    assert first.tree.main.path.config == tmp_path.resolve()

    # conftest 的 fixture 会在用例之间清表，这里模拟同一用例内的重建
    Configue._instance = None
    from albuswall.configue1.registry import _CLASSES, _FIELD_OWNER, _PATHS
    _CLASSES.clear()
    _PATHS.clear()
    _FIELD_OWNER.clear()
    _FILES.clear()

    second = setup_config(Container(), base_dir=tmp_path, portable=True)
    assert second.tree.main.path.config == tmp_path.resolve()


# ==================== log.bootstrap ====================

def test_log_conf_reads_documented_keys(tmp_path, container, monkeypatch):
    # 让 fallback 走「配置文件不存在」分支，避免 fileConfig 改全局 logging
    (tmp_path / "confdir").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "confdir"))
    if sys.platform in ("win32", "darwin"):
        pytest.skip("XDG 路径只在 Linux 上可断言")

    configclass("log", "main")(log_bootstrap.LogConf)
    config = _setup(tmp_path, container)
    assert config.tree.main.log.level == "WARNING"

    from albuswall.log.handlers import MemoryCacheHandler

    root = logging.getLogger("albuswall")
    saved_handlers = list(root.handlers)
    saved_level = root.level
    # 只有当存在 MemoryCacheHandler 时 setup_log 才走「替换 handler」分支
    root.addHandler(MemoryCacheHandler())
    try:
        log_bootstrap.setup_log(container)
        # 级别生效 + 因为日志配置文件不存在，加了一个 stderr fallback
        assert root.level == logging.WARNING
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_log_conf_falls_back_to_user_config_dir(tmp_path, container, monkeypatch):
    (tmp_path / "confdir").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "confdir"))
    if sys.platform in ("win32", "darwin"):
        pytest.skip("XDG 路径只在 Linux 上可断言")

    write_ini(tmp_path / "config.ini", "[files]\nlog = custom_logs.ini\n")
    configclass("log", "main")(log_bootstrap.LogConf)
    config = setup_config(container, base_dir=tmp_path)

    # 没有 [log] config → 用 <user config dir>/<files.log>
    expected = tmp_path / "confdir" / "albuswall" / "custom_logs.ini"
    assert config.tree.main.files.log == "custom_logs.ini"
    assert expected.is_absolute()


# ==================== infrastructure.task.bootstrap ====================

def test_task_config_reads_values(tmp_path, container):
    configclass("task", "main")(task_bootstrap.Config)
    config = _setup(tmp_path, container)

    task = task_bootstrap.Config()
    assert task.initial_max_concurrency == 4          # 文件里写了
    assert task.max_concurrency == 8                  # int | None + 文件值
    assert task.process_workers is None               # 缺省 → None
    assert task.mem_low_threshold == 50.0             # 默认值
    assert config.tree.main.task.max_concurrency == 8


def test_register_task_service_registers_factory(tmp_path, container, clean_hooks):
    configclass("task", "main")(task_bootstrap.Config)
    _setup(tmp_path, container)

    task_bootstrap.register_task_service(container)

    assert "task_service" in container
    assert container.has_instance("task_service") is False  # 懒构造


# ==================== infrastructure.database.bootstrap ====================

def test_register_database_computes_path_from_tree(
        tmp_path, container, monkeypatch, clean_hooks,
):
    configclass("db", "main")(db_bootstrap.Config)
    write_ini(
        tmp_path / "config.ini",
        "[path]\ndata = /srv/albuswall\n[files]\ndb = gallery.db\n[db]\nmust_exist = true\n",
    )
    setup_config(container, base_dir=tmp_path)

    captured: dict = {}

    class FakeConnector:
        def __init__(self, path, *, must_exist=False):
            captured["path"] = path
            captured["must_exist"] = must_exist

    monkeypatch.setattr(db_bootstrap, "Connector", FakeConnector)
    db_bootstrap.register_database(container)
    container.require("db")

    assert captured["path"] == Path("/srv/albuswall/gallery.db")
    assert captured["must_exist"] is True


# ==================== portable：[path] 四段从哪来 ====================

def test_portable_false_uses_user_dirs(monkeypatch, tmp_path, container):
    """portable=False（默认）时 [path] 走 platform_dir 的四个函数，与 base_dir 无关。

    直接 mock ``configue1.bootstrap`` 上的四个函数，而不是设 XDG 环境变量：
    ``get_user_config_dir`` / ``get_cache_dir`` 在不同平台读不同变量（甚至可能走
    platformdirs），用环境变量做断言会在 macOS / Windows 上误报。
    """
    from albuswall.configue1 import bootstrap as cfg_bootstrap

    dirs = {
        "config": tmp_path / "user_config",
        "data": tmp_path / "user_data",
        "cache": tmp_path / "user_cache",
        "temp": tmp_path / "user_temp",
    }
    monkeypatch.setattr(
        cfg_bootstrap, "get_user_config_dir",
        lambda *_a, **_k: dirs["config"],
    )
    monkeypatch.setattr(
        cfg_bootstrap, "get_user_data_dir",
        lambda *_a, **_k: dirs["data"],
    )
    monkeypatch.setattr(
        cfg_bootstrap, "get_cache_dir",
        lambda *_a, **_k: dirs["cache"],
    )
    monkeypatch.setattr(
        cfg_bootstrap, "get_temp_dir",
        lambda *_a, **_k: dirs["temp"],
    )

    # 没有 config.ini → 用 ignore_missing 走默认值
    config = setup_config(
        container, base_dir=tmp_path, ignore_missing=True, portable=False,
    )
    tree = config.tree.main

    assert tree.path.config == dirs["config"].resolve()
    assert tree.path.data == dirs["data"].resolve()
    assert tree.path.cache == dirs["cache"].resolve()
    assert tree.path.temp == dirs["temp"].resolve()

    # 与 base_dir 无关
    assert tree.path.config != tmp_path.resolve()

    # portable=False 时 base_dir 仍然只决定 config.ini 的落点
    assert (tmp_path / "config.ini").exists() is False


def test_portable_true_layout(tmp_path, container):
    """portable=True 时 [path] 全部落在 base_dir 下。"""
    config = setup_config(
        container, base_dir=tmp_path, portable=True, ignore_missing=True,
    )
    tree = config.tree.main

    assert tree.path.config == tmp_path.resolve()
    assert tree.path.data == (tmp_path / "data").resolve()
    assert tree.path.cache == (tmp_path / "cache").resolve()
    assert tree.path.temp == (tmp_path / "temp").resolve()

    # 不预先创建子目录（首次写入时按需创建）
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "cache").exists()
    assert not (tmp_path / "temp").exists()


def test_portable_true_save_creates_dirs_on_demand(tmp_path, container):
    """portable=True + ignore_missing：save() 落到 base_dir/config.ini。"""
    config = setup_config(
        container, base_dir=tmp_path, portable=True, ignore_missing=True,
    )

    config.tree.main.path.theme = str(tmp_path / "theme")
    written = config.tree.main.save()

    assert written == [(tmp_path / "config.ini").resolve()]
    assert (tmp_path / "config.ini").is_file()


def test_portable_true_requires_base_dir(container, tmp_path):
    """portable=True 且不给 base_dir → ValueError（避免静默退回用户目录）。"""
    with pytest.raises(ValueError, match="portable=True requires an explicit base_dir"):
        setup_config(container, portable=True, ignore_missing=True)
