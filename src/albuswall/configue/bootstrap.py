#
"""加载阶段的 boot 门面。

两部分，边界很清楚：

1. **通用部分**：``load_file(finfo)`` —— ``FileInfo`` → 原始字典。只做「分派给
   后端并读盘」，**不碰 Resolver**：注入、排序、类型注册、默认值都是
   ``Configue.load()`` 的事。格式实现全部在 ``backends/``（INI / JSON）。
2. **应用装配（albuswall 专用）**：``setup_config(container)`` —— 替代旧包
   ``setup_static_config`` 的那一步：登记配置文件、声明 ``[path]`` / ``[files]``
   内建段、``load()``、把单例注册进依赖容器。

第 2 部分是按 review 决定放在「配置器自己的 boot 代码」里的：它把 albuswall 的
路径约定（``get_user_config_dir`` 等）带进了这个包。如果将来出现第二个使用方，
把它整段搬到 app 侧模块即可（对外只有 ``setup_config`` 一个入口，搬迁是一处
import 的改动）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import albuswall
from albuswall.log import getLogger

from .backends import get_backend
from .errors import ConfigError
from .registry import (
    AutosavePolicy,
    ConfigFile,
    FileInfo,
    configclass,
    reg,
)
from .utils.platform_dir import (
    get_cache_dir,
    get_temp_dir,
    get_user_config_dir,
    get_user_data_dir,
)

__all__ = [
    "load_file",
    "setup_config",
    "CONFIG_FILE_NAME",
    "MAIN_FILE_NAME",
    # 应用级段的类视图（业务代码从这里 import，见各自的 docstring）
    "Files",
    "Paths",
]

#: 用户配置文件（放 ``base_dir`` 下）。
CONFIG_FILE_NAME = "config.ini"

#: 主配置的注册名——``c.tree.main`` / ``c.flush(file="main")`` 用它。
MAIN_FILE_NAME = "main"

logger = getLogger(__package__)


# ===========================================================================
# 1. 通用：FileInfo → 原始字典
# ===========================================================================

def load_file(finfo: FileInfo) -> dict[tuple[str, str], Any]:
    """按 ``finfo.backend`` 分派后端读盘。

    Args:
        finfo: 必须已经有 ``resolved_path``（``Configue.load()`` 在解析之后才
            构造它；未加载的 ``FileInfo`` 直接调用是编程错误）。

    Raises:
        ConfigError: 未解析路径 / 后端未注册 / 各后端自己的读盘错误。
    """
    if finfo.resolved_path is None:
        raise ConfigError(
            f"file {finfo.name!r} has no resolved_path; "
            f"it must be resolved before reading"
        )
    backend = get_backend(finfo.backend)
    return backend.read(finfo.resolved_path)


# ===========================================================================
# 2. 应用装配：albuswall 的内建段 + setup_config
# ===========================================================================

class Paths:
    """``[path]``：应用自己的目录。

    **字段不带默认值**：四个目录依赖 ``portable`` / ``base_dir``，只能在
    ``setup_config`` 运行期算出来，由它 ``resolver.inject_raw`` 注入（效果与
    ``@configclass`` 默认值等价——``load()`` 注入文件值时用 ``overwrite=True``，
    文件里写了的段照样赢）。

    ``@configclass`` 装饰发生在 :func:`_declare_app_sections`（装配期）而不是
    模块导入期，有两个原因：

    - 登记键是 ``(file, class_path)``，``file`` 来自 ``setup_config(name=...)``
      参数，导入期还不知道；也正因为键里带 file，**同一段不能声明两份**
      （后装饰者会把先注册的类从 ``_CLASSES`` 顶掉），所以业务代码要读
      ``[path]`` 只能 import 本类，不能再写一份「只读视图」；
    - 导入 ``configue`` 不往全局登记表里塞节点（未装配时 ``load()`` 不会因为
      「引用了未 reg 的 file」而报错）。

    业务代码用法（**必须在 ``setup_config`` 之后**）::

        from albuswall.configue.bootstrap import Paths

        db_file = Paths().data / "db.db"
    """

    config: Path
    data: Path
    cache: Path
    temp: Path


class Files:
    """``[files]``：文件名，相对 ``[path]`` 使用。

    默认值与平台无关，所以直接写在字段上（``load()`` 时由 ``@configclass``
    注入）；装饰时机与用法同 :class:`Paths`。
    """

    log: str = "log_config.ini"
    db: str = "db.db"
    qss: str = "qss.qss"
    icons: str = "*.svg"


def _declare_app_sections(name: str) -> None:
    """把 :class:`Paths` / :class:`Files` 登记到 ``file=name`` 名下。

    幂等：``configclass`` 对同一 ``(file, class_path)`` 是覆盖注册，测试清空
    全局表之后仍能重建。两个类是**模块级对象**，所以业务代码 import 到的
    就是被登记的那个类（装饰是原地修改，不产生新类）。
    """
    configclass("path", name)(Paths)
    configclass("files", name)(Files)


def _inject_path_defaults(
        config: Any,
        path_config: Path,
        path_data: Path,
        path_cache: Path,
        path_temp: Path,
) -> None:
    """注入 ``[path]`` 四段的默认值（替代 ``@configclass`` 字段默认值）。

    必须在 ``load()`` **之前**调用：``load()`` 注入文件值的 ``overwrite=True``，
    文件里写了的段会盖掉这里，语义与「默认值只在缺值时补位」一致；同时默认值
    在 ``check_all`` / 加载钩子之前就已就位（与改前逐字段默认值的时机相同）。

    ``inject_raw`` 不带 origin，所以这些值不会被当成「用户改过的」而回写——
    与 ``@configclass`` 默认值完全一致。
    """
    resolver = config.resolver
    for key, value in (
            ("config", path_config),
            ("data", path_data),
            ("cache", path_cache),
            ("temp", path_temp),
    ):
        resolver.inject_raw("path", key, value)


def setup_config(
        container,
        *,
        base_dir: Path | str | None = None,
        file_name: str = CONFIG_FILE_NAME,
        name: str = MAIN_FILE_NAME,
        autosave: AutosavePolicy = "atexit",
        order: int = 10,
        register_aliases: bool = True,
        ignore_missing: bool = False,
        portable: bool = False,
):
    """装配 albuswall 的配置并注册进依赖容器，返回 ``Configue`` 单例。

    等价于旧包的 ``setup_static_config(container, config_file_path)``，但：

    - **不再处理 include**（旧包的 ``[configue] include`` 多文件合并尚未移植）；
    - **不再有全局 persistence 策略**，改为 ``reg(..., autosave=...)`` 的文件级策略；
    - ``[path]`` / ``[files]`` 是**声明默认值**（``@configclass``），不再靠
      ``ConfigDeclaration.add_builtin``；
    - 配置文件不存在时 ``load()`` **直接抛** ``ConfigFileNotFound``
      （旧的 ``load_ini_`` 容错为空——本阶段明确改为要求文件存在）。

    Args:
        container: 依赖容器（``Container``）。注册两个别名：
            ``config`` 与 ``configue``，都是本单例。
        base_dir: 配置文件所在目录；``None`` → ``get_user_config_dir()``。
        file_name: 配置文件名；``None`` 不合法。
        name: 注册名（``tree.<name>``）。
        autosave: 文件级回写策略；默认 ``"atexit"``（与旧包全局默认一致）。
        order: ``FileInfo.order``；单文件无所谓，留给将来加覆盖文件。
        register_aliases: 是否注册容器别名（测试可直接关掉）。
        ignore_missing: 透传给 ``reg(optional=...)``——``True`` 时「配置文件
            不存在」不算错误：``load()`` 以 ``base_dir/file_name`` 占位把文件
            标成已加载，默认值照常可读，首次 ``save()`` 时创建文件。
            ``False``（默认）缺文件直接抛 ``ConfigError``。**歧义仍然报错**。
        portable: 决定 ``[path]`` 四段的默认值从哪来（详见下方「portable」）。

    portable:
        ``False``（默认）：``[path]`` 四段全部走 ``get_user_config_dir`` /
        ``get_user_data_dir`` / ``get_cache_dir`` / ``get_temp_dir``，与
        ``base_dir`` 无关。
        ``True``：``[path]`` 四段全部落在 ``base_dir`` 下（``config = base_dir``，
        ``data`` / ``cache`` / ``temp`` 为 ``base_dir/data`` 等子目录），用于测试
        隔离或便携部署。
        **只改 ``[path]`` 四段，不改 ``config.ini`` 的落点**（仍为
        ``base_dir / file_name``）。另外 ``portable=True`` 时**不预先创建**
        ``data`` / ``cache`` / ``temp`` 目录，首次写入时按需创建；``False`` 时
        ``get_temp_dir()`` 会顺带 ``mkdir`` 出 temp 目录（现有行为）。

    Raises:
        ValueError: ``portable=True`` 但未给 ``base_dir``。
        ConfigError: 缺文件（``ignore_missing=False``）/ 声明 / 类型 / 钩子错误。
    """
    from .configue import Configue

    if portable and base_dir is None:
        raise ValueError(
            "setup_config: portable=True requires an explicit base_dir "
            "(portable means 'everything under base_dir')"
        )

    target_dir = (
        Path(base_dir).resolve() if base_dir is not None
        else get_user_config_dir(albuswall.__name__, albuswall.__author__).resolve()
    )

    if portable:
        # 便携 / 测试隔离：四段全部收在 base_dir 下，不碰用户目录。
        # 注意不预先 mkdir（data/cache/temp 在首次写入时按需创建）。
        path_config = target_dir
        path_data = target_dir / "data"
        path_cache = target_dir / "cache"
        path_temp = target_dir / "temp"
    else:
        path_config = get_user_config_dir(
            albuswall.__name__, albuswall.__author__,
        ).resolve()
        path_data = get_user_data_dir(
            albuswall.__name__, albuswall.__author__,
        ).resolve()
        path_cache = get_cache_dir(albuswall.__name__).resolve()
        path_temp = get_temp_dir(albuswall.__name__).resolve()

    _declare_app_sections(name)

    reg(
        ConfigFile.INI, file_name, name=name, autosave=autosave, order=order,
        optional=ignore_missing,
    )
    config: Configue = Configue(target_dir)
    # [path] 默认值先注入再 load()：文件里写了的段由 load() 的 overwrite 赢。
    _inject_path_defaults(
        config, path_config, path_data, path_cache, path_temp,
    )
    config.load()
    logger.info("Conf loaded: %s", base_dir / file_name)

    if register_aliases:
        container.reg("config", lambda: config, returns=Configue)
        container.reg("configue", lambda: config, returns=Configue)

    return config
