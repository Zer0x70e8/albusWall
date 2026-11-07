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

**快捷键也走这里**：``setup_config`` 把 ``shortcuts.ini`` 登记成**两个**文件
（随包默认表 + 用户覆盖表），于是 ``[shortcuts]`` 和 ``[ui]`` 一样是配置器的
一等公民——可枚举、可校验、可 ``flush`` 写回。记录类型
:class:`albuswall.common.keymap.ShortcutRecord` 放在 ``common``（无 Qt），
转换器与加载钩子在这里登记；``albuswall.ui.keymap`` 只负责把记录绑成
``QKeySequence``。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import albuswall
from albuswall import resources
from albuswall.common.constants.ui import (
    DEFAULT_THEME,
    ICON_DIR_NAME,
    ICON_KEYS,
    ICON_SUFFIX,
    THEME_QSS_NAME,
)
from albuswall.log import getLogger

from albuswall.common.keymap import (
    FILE_NAME as SHORTCUT_FILE_NAME,
    ShortcutRecord,
    type_shortcut_keys,
)

from .backends import get_backend
from .errors import ConfigError
from .registry import (
    _FILES,
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
    "ICON_KEYS",
    "MAIN_FILE_NAME",
    "SHORTCUT_DEFAULTS_FILE",
    "SHORTCUT_USER_FILE",
    # 应用级段的类视图（业务代码从这里 import，见各自的 docstring）
    "Files",
    "Paths",
    "UiSelection",
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
    """``[path]``：应用自己的目录 + 用户主题目录。

    **字段不带默认值**：目录依赖 ``portable`` / ``base_dir``，只能在
    ``setup_config`` 运行期算出来，由它 ``resolver.inject_raw`` 注入（效果与
    ``@configclass`` 默认值等价——``load()`` 注入文件值时用 ``overwrite=True``，
    文件里写了的段照样赢）。

    ``theme`` / ``icons`` 是主题那一对（布局见
    :mod:`albuswall.common.constants.ui`）：

    * ``theme`` —— 用户主题根目录，``<theme>/<name>/main_window.qss``；
    * ``icons`` —— **当前主题**的图标目录 ``<theme>/<name>/icons/``，即
      ``${theme}/${ui:theme}/icons``（含 ``${ui:theme}``，切主题时一起变）。

    它们在 ``[ui]`` 里靠 ``${...}`` 与 ``${ui:theme}`` / ``${files:main_window_qss}``
    组合成绝对路径（见 :mod:`albuswall.ui.presenter.theme`），因此这里**只**声明
    目录，不拼文件名。

    ``@configclass`` 装饰发生在 :func:`_declare_app_sections`（装配期）而不是
    模块导入期，有两个原因：

    - 登记键是 ``(file, class_path)``，``file`` 来自 ``setup_config(name=...)``
      参数，导入期还不知道；
    - 导入 ``configue`` 不往全局登记表里塞节点（未装配时 ``load()`` 不会因为
      「引用了未 reg 的 file」而报错）。

    **同名 section 允许多份声明**：别的模块想写一份「只读视图」是合法的，
    只要字段的类型与默认值与本类一致（不一致会在装饰期抛
    ``ConfigDeclarationConflict``，声明不一致时应当直接 import 本类）。

    业务代码用法（**必须在 ``setup_config`` 之后**）::

        from albuswall.configue.bootstrap import Paths

        db_file = Paths().data / "db.db"
        theme_dir = Paths().theme / "default"      # ~/.config/albuswall/theme/default
    """

    config: Path
    data: Path
    cache: Path
    temp: Path
    theme: Path
    icons: Path


class Files:
    """``[files]``：文件名，相对 ``[path]`` 使用。

    默认值与平台无关，所以直接写在字段上（``load()`` 时由 ``@configclass``
    注入）；装饰时机与用法同 :class:`Paths`。

    主题相关的那几个（``main_window_qss`` 与 :data:`ICON_KEYS`）不是给人手算
    路径用的：``[ui]`` 用 ``${path:theme}/${ui:theme}/${files:main_window_qss}``
    与 ``${path:icons}/${files:<icon key>}`` 把它们组合成绝对路径，UI 只读
    ``[ui]``。改这里 = 换主题里的文件名。
    """

    log: str = "log_config.ini"
    db: str = "db.db"
    #: 主题入口样式表文件名（``[ui] style_sheet`` 的最后一段）。
    main_window_qss: str = THEME_QSS_NAME
    #: 图标文件名（``[ui] <同名 key>`` 的最后一段）。
    app_icon: str = f"albuswall{ICON_SUFFIX}"
    album_button: str = f"album_button{ICON_SUFFIX}"
    search_button: str = f"search_button{ICON_SUFFIX}"
    more_button: str = f"more_button{ICON_SUFFIX}"
    favorite_icon: str = f"favorite_icon{ICON_SUFFIX}"
    #: 内置主题补齐时用的图标 glob（预留：把随包主题复制到用户目录）。
    icon_glob: str = f"*{ICON_SUFFIX}"


class UiSelection:
    """``[ui]`` 段里**加载期就必须存在**的那一个 key。

    只声明 ``theme``，不声明 UI 的其它键——它们属于
    :class:`albuswall.ui.presenter.theme.ThemeConfig`（同一个 section 的另一份
    声明，二者共享 Resolver 节点；``theme`` 在两处必须写成同一类型与默认值）。

    为什么要在 ``load()`` 之前（也就是在 UI import 之前）就把 ``theme`` 声明出来：
    ``[path]`` / ``[files]`` / ``[ui]`` 里的组合式都含 ``${ui:theme}``，而
    ``load()`` 的 ``check_all()`` 会展开**文件里所有**字符串节点。若那时
    ``[ui] theme`` 还不存在（用户配置文件里没写、UI 包也还没 import），
    展开会抛 ``KeyError``，整个启动会 ``ConfigError``——一个「默认值写得太晚」
    把用户挡住。早点给它默认值，组合式在加载期就能自洽展开。
    """

    theme: str = DEFAULT_THEME


def _declare_app_sections(name: str) -> None:
    """把 :class:`Paths` / :class:`Files` / :class:`UiSelection` 登记到
    ``file=name`` 名下。

    幂等：``configclass`` 对同一 ``(file, class_path)`` 是覆盖注册，测试清空
    全局表之后仍能重建。三个类都是**模块级对象**，所以业务代码 import 到的
    就是被登记的那个类（装饰是原地修改，不产生新类）。
    """
    configclass("path", name)(Paths)
    configclass("files", name)(Files)
    configclass("ui", name)(UiSelection)


def _inject_path_defaults(
        config: Any,
        path_config: Path,
        path_data: Path,
        path_cache: Path,
        path_temp: Path,
) -> None:
    """注入 ``[path]`` 各段的默认值（替代 ``@configclass`` 字段默认值）。

    必须在 ``load()`` **之前**调用：``load()`` 注入文件值的 ``overwrite=True``，
    文件里写了的段会盖掉这里，语义与「默认值只在缺值时补位」一致；同时默认值
    在 ``check_all`` / 加载钩子之前就已就位（与改前逐字段默认值的时机相同）。

    ``theme`` / ``icons`` 也在这里补：它们的值由 ``path_config`` 决定，
    单独写在 ``Paths`` 的字段默认值里会让「``portable`` 布局」看不出来。
    ``icons`` 是**组合式**（``${theme}/${ui:theme}/icons``），与随包
    ``conf.ini`` 写成同一份，用户没写这一行时行为也一样。

    ``inject_raw`` 不带 origin，所以这些值不会被当成「用户改过的」而回写——
    与 ``@configclass`` 默认值完全一致。
    """
    resolver = config.resolver
    for key, value in (
            ("config", path_config),
            ("data", path_data),
            ("cache", path_cache),
            ("temp", path_temp),
            ("theme", path_config / "theme"),
            # 主题图标目录跟着 ``[ui] theme`` 走（与随包 conf.ini 的组合式逐字
            # 等价）：默认值写成**插值字符串**而不是拼好的 Path，切主题时才能
            # 跟着 ``${ui:theme}`` 一起失效、重新展开。
            ("icons", "${theme}/${ui:theme}/" + ICON_DIR_NAME),
    ):
        resolver.inject_raw("path", key, value)


#: 随包默认快捷键表的注册名（只读，``[shortcuts]`` 的第一份定义）。
SHORTCUT_DEFAULTS_FILE = "shortcuts.default"

#: 用户快捷键覆盖表的注册名（可写，``tree.shortcuts[...]`` 写这里）。
SHORTCUT_USER_FILE = "shortcuts"

#: 两份快捷键文件的 ``FileInfo.order``：用户表必须比默认表大（后写、赢）。
SHORTCUT_DEFAULTS_ORDER = 20
SHORTCUT_USER_ORDER = 30


def _register_shortcut_files(user_file_name: str) -> None:
    """把快捷键表登记成两个配置文件（随包默认 + 用户覆盖）。

    为什么要两个文件、而不是一个：

    * 默认表随包分发（``resources/config/shortcuts.ini``），必须**只读**：
      用户升级后要能拿到新默认值，而不是被自己那份旧拷贝永久钉死；
    * 用户表（``<base_dir>/shortcuts.ini``）只存**与默认不同的条目**，可写、
      可删（删掉即恢复全默认）。

    合并语义靠 ``FileInfo.order``：``load()`` 按 order 升序注入并
    ``overwrite=True``，所以 order 大的用户表**逐 key** 覆盖默认表，
    用户没写的 key 仍然拿默认值——这正是 ``shortcuts.ini`` 顶部注释承诺的
    「User overrides live in ``<user_config_dir>/shortcuts.ini`` and win
    per command」。

    两个文件都带 ``optional=True``：随包表缺失是打包事故（记 WARNING 即可，
    不该让整个应用起不来），用户表在第一次保存前**本来就不存在**。
    """
    try:
        bundled = Path(str(resources.get("shortcuts")))
    except (FileNotFoundError, KeyError, ValueError) as exc:
        logger.warning(
            "bundled shortcut table is unavailable (%s); "
            "shortcuts fall back to the slots' default keys", exc,
        )
    else:
        reg(
            ConfigFile.INI, str(bundled), name=SHORTCUT_DEFAULTS_FILE,
            autosave="off", order=SHORTCUT_DEFAULTS_ORDER, optional=True,
        )

    reg(
        ConfigFile.INI, user_file_name, name=SHORTCUT_USER_FILE,
        # 用户表由界面显式写回（``ShortcutRegistry.save_shortcut`` 里自带
        # flush），不需要 atexit 兜底，也就不会在退出时莫名多写一次。
        autosave="manual", order=SHORTCUT_USER_ORDER, optional=True,
    )


def _wire_shortcut_config(config: Any) -> None:
    """快捷键表的转换器 / 加载钩子 / 只读标记 / 段级写回目标。

    全部必须在 ``config.load()`` **之前或之后立刻**完成：

    * 转换器：``load()`` 的 Step 4 前置会应用「构造前登记的转换器」，这里直接
      登记在当前实例上，效果等价且不依赖登记时机；
    * 加载钩子：Step 4.5（类型自检之前）给 ``[shortcuts]`` 的每个 key 补
      ``ShortcutRecord`` 类型，否则它们会被当成「没有类型的原始字符串」；
    * 段级写回落点：``[shortcuts]`` 没有 ``@configclass`` 字段，
      ``target_file_of`` 需要知道「这个段该写用户表」。

    **只读标记不在这里**：``resolved_path`` 要等 ``load()`` 解析路径之后才有，
    见 :func:`_mark_shortcut_defaults_readonly`。
    """
    from . import register_load_hook

    config.resolver.register_converter(
        ShortcutRecord,
        ShortcutRecord.from_ini,
        writer=lambda record: record.to_ini(),
    )
    register_load_hook(type_shortcut_keys)
    config.register_section_file(SHORTCUT_USER_FILE, SHORTCUT_USER_FILE)


def _mark_shortcut_defaults_readonly(config: Any) -> None:
    """把随包默认快捷键表标记成只读（必须在 ``load()`` 之后调用）。

    只读 = ``flush`` 不会写它、``target_file_of`` 会改挑用户表。必须**显式**
    标记而不能指望文件权限：默认表装在包目录里，开发机上它照样可写；以 root
    运行时 ``os.access(..., W_OK)`` 更是恒真。
    """
    finfo = _FILES.get(SHORTCUT_DEFAULTS_FILE)
    if finfo is not None and finfo.resolved_path is not None:
        config.resolver.mark_readonly_file(finfo.resolved_path)


#: 随包默认配置（``resources/config/`` 下的文件 → 目标文件名）。
#:
#: 只用 ``conf.ini`` 作为主配置模板：``[log] config`` 指向 ``[files] log``
#: （``log_config.ini``），所以日志配置也一起复制，``setup_log()`` 才不会退化
#: 成 stderr。用户快捷键覆盖表（``shortcuts.ini``）**不复制**：随包默认表本来
#: 就是只读兜底，用户表由第一次保存时创建。
_SEED_FILES: tuple[tuple[str, str | None], ...] = (
    ("default_config", None),                # → file_name（生产即 config.ini）
    ("default_log_config", "log_config.ini"),  # → [files] log 的默认文件名
)


def seed_default_config(
        target_dir: Path | str, file_name: str = "config.ini",
) -> list[Path]:
    """配置目录里缺哪个默认文件就复制哪个（幂等，已存在不覆盖）。

    「首次启动 / 用户误删配置」时让程序有一份可读可写的完整默认配置，而不是
    靠 ``ignore_missing=True`` 在内存里装作有；用户改过的文件永远不会被覆盖。

    Args:
        target_dir: 用户配置目录（``setup_config`` 的 ``base_dir``）。
        file_name: 主配置文件名（默认 ``config.ini``）；随包模板是 ``conf.ini``。

    Returns:
        实际写入的文件路径（可能为空列表）。
    """
    target = Path(target_dir)
    written: list[Path] = []
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("cannot create config dir %s", target, exc_info=True)
        return written

    for alias, default_name in _SEED_FILES:
        destination = target / (default_name or file_name)
        if destination.exists():
            continue
        source = resources.get(alias)
        try:
            shutil.copyfile(source, destination)
        except OSError:
            logger.warning(
                "cannot seed %s from %s", destination, source, exc_info=True,
            )
            continue
        written.append(destination)
        logger.info("Seeded default config: %s", destination)
    return written


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
        shortcut_file_name: str = SHORTCUT_FILE_NAME,
        seed_defaults: bool = False,
):
    """装配 albuswall 的配置并注册进依赖容器，返回 ``Configue`` 单例。

    等价于旧包的 ``setup_static_config(container, config_file_path)``，但：

    - **不再处理 include**（旧包的 ``[configue] include`` 多文件合并尚未移植）；
    - **不再有全局 persistence 策略**，改为 ``reg(..., autosave=...)`` 的文件级策略；
    - ``[path]`` / ``[files]`` / ``[ui] theme`` 是**声明默认值**
      （``@configclass``），不再靠 ``ConfigDeclaration.add_builtin``；
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
        portable: 决定 ``[path]`` 各目录默认值从哪来（详见下方「portable」）。
        shortcut_file_name: 用户快捷键覆盖表的文件名，默认
            ``shortcuts.ini``，落在 ``base_dir`` 下（生产环境即
            ``~/.config/albuswall/shortcuts.ini``）。随包默认表由
            ``resources/config/shortcuts.ini`` 提供，**不受本参数影响**。
        seed_defaults: 配置文件（及同目录的 ``log_config.ini``）不存在时，
            先把随包默认配置复制过去再加载——首次启动即可得到一份可读可写的
            完整配置。**已存在的文件绝不覆盖**；默认关闭，应用入口显式打开
            （测试里通常自己写最小配置，不希望被默认值填满）。

    portable:
        ``False``（默认）：``[path]`` 的 ``config`` / ``data`` / ``cache`` /
        ``temp`` 走 ``get_user_config_dir`` / ``get_user_data_dir`` /
        ``get_cache_dir`` / ``get_temp_dir``，与 ``base_dir`` 无关；
        ``theme`` / ``icons`` 是 ``<config>/theme`` 与
        ``${theme}/${ui:theme}/icons``（当前主题的图标目录）。
        ``True``：``[path]`` 全部落在 ``base_dir`` 下（``config = base_dir``，
        ``data`` / ``cache`` / ``temp`` 为 ``base_dir/data`` 等子目录，
        ``theme`` 为 ``base_dir/theme``），用于测试隔离或便携部署。
        **只改 ``[path]``，不改 ``config.ini`` 的落点**（仍为
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
        # 便携 / 测试隔离：各目录全部收在 base_dir 下，不碰用户目录。
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

    if seed_defaults:
        seed_default_config(target_dir, file_name)

    reg(
        ConfigFile.INI, file_name, name=name, autosave=autosave, order=order,
        optional=ignore_missing,
    )
    config: Configue = Configue(target_dir)
    # [path] 默认值先注入再 load()：文件里写了的段由 load() 的 overwrite 赢。
    _inject_path_defaults(
        config, path_config, path_data, path_cache, path_temp,
    )
    # 快捷键：转换器 + 加载钩子必须早于 load()（类型自检要用），只读标记与
    # 段级写回落点可以紧随其后（它们只影响写回，不影响读）。
    _register_shortcut_files(shortcut_file_name)
    _wire_shortcut_config(config)
    config.load()
    _mark_shortcut_defaults_readonly(config)
    # 用 ``target_dir`` 而不是 ``base_dir``：``base_dir`` 可能是 ``None``
    # （回退用户配置目录）或 ``str``（调用方直接传字符串），两者做 ``/`` 都会
    # 失败 —— 前者 ``TypeError``，后者拼出错误的 ``str / str``。
    logger.info("Conf loaded: %s", target_dir / file_name)

    if register_aliases:
        container.reg("config", lambda: config, returns=Configue)
        container.reg("configue", lambda: config, returns=Configue)

    return config
