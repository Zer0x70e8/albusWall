#
"""快捷键配置文件：默认表（resources）+ 用户覆盖（用户配置目录）。

**单独一个文件**，不和 ``config.ini`` 混在一起。原因有三：

1. 绑定表的 key 是命令路径，属于「运行期才知道的 key」——命令由插件在导入期
   注册，声明式字段写不出来，硬塞进主配置只会让声明表变脏；
2. 整表重载（热改键位）只碰一个文件，不惊动数据库 / 主题 / 路径那些配置；
3. 默认表要随包分发（``resources``），用户表要可写（用户配置目录），两份
   文件天然就是两个来源。

文件格式（INI）::

    # 注释
    [shortcuts]
    slot_name     = ["Alt+1", [1], {"mode": "x"}]
    view.fullscreen = [["F11", "Ctrl+Shift+F"], [], {}]
    content.search = ["Ctrl+F"]

每条记录是「快捷键 + 位置参数 + 关键字参数」三元组（JSON 数组）：

========================  ==================================================
位置                      含义
========================  ==================================================
``[0]`` **快捷键**         单个写字符串（``"Alt+1"``），多个写字符串数组
``[1]`` **位置参数**       JSON 数组，触发时按位置传给槽函数
``[2]`` **关键字参数**     JSON 对象，触发时按关键字传给槽函数
========================  ==================================================

两个简写也接受（默认表里用的就是简写，读起来干净）：

- 扁平字符串数组 ``["F11", "Ctrl+Shift+F"]`` = 多个键位 + 无参数（旧写法）；
- 单个字符串 ``"Ctrl+Q"`` = 一个键位 + 无参数；
- 空串 / ``[]`` = **显式解绑**（用户用它关掉一个默认快捷键）。

``save()`` 永远写完整的规范化三元组形式，读回时两种写法都认。

两份文件与优先级：

- 默认表：``resources/config/shortcuts.ini``（只读，随包分发）；
- 用户表：``<user_config_dir>/shortcuts.ini``（只写**与默认表不同的条目**）。

用户表优先，用户文件缺失 = 全部走默认。``save()`` 只写差异，默认表升级后
用户不会永远卡在旧键位上；想恢复默认删掉用户文件即可（:meth:`reset`）。
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Iterable, Mapping

import albuswall
from albuswall.common.keymap import (
    FILE_NAME as FILE_NAME_IN_COMMON,
    SECTION as SECTION_IN_COMMON,
    ShortcutRecord,
)
from albuswall.configue.utils.platform_dir import get_user_config_dir

from .errors import InvalidKeySequence, ShortcutConfigError
from .model import ShortcutBinding
from .sequence import KeySequence, parse_sequences

__all__ = [
    "DEFAULT_FILE_NAME",
    "DEFAULT_SECTION",
    "ShortcutConfig",
    "default_config_path",
    "user_config_path",
]

logger = logging.getLogger(__package__)

#: 默认 / 用户快捷键文件名（两个目录下同名，但互不影响）。
#:
#: 与 :mod:`albuswall.common.keymap`（配置器那股线用的同一份常量）同源：
#: 两份实现读的是同一批文件，文件名不允许出现第二种写法。
DEFAULT_FILE_NAME = FILE_NAME_IN_COMMON

#: 绑定表所在段名。与 ``configue`` 文档里预留的 ``[shortcuts]`` 保持一致。
DEFAULT_SECTION = SECTION_IN_COMMON

#: 「按默认规则解析路径」的哨兵；``None`` 表示「没有这份文件」。
_DEFAULT = object()

#: 用户表文件头。用户手工改坏了也能对照这里恢复。
_FILE_HEADER = """\
# albuswall shortcuts -- user overrides
#
# 每行是一条绑定记录，写法：命令路径 = [快捷键, 位置参数, 关键字参数]
#   快捷键     单个写字符串 "Alt+1"，多个写数组 ["F11", "Ctrl+Shift+F"]
#   位置参数   JSON 数组，如 [1, "x"]；没有就写 []
#   关键字参数 JSON 对象，如 {"mode": "fit"}；没有就写 {}
# 键位用 Qt 便携写法：F5 / Ctrl+N / G, G（逗号分隔和弦步）。
# 快捷键写 [] 表示显式解绑。只保存与默认表
# （resources/config/shortcuts.ini）不同的条目；删掉本文件即恢复默认。
"""


def default_config_path() -> Path:
    """随包分发的默认绑定表（``resources/config/shortcuts.ini``）。"""
    from albuswall import resources

    return resources.get("shortcuts")


def user_config_path(file_name: str = DEFAULT_FILE_NAME) -> Path:
    """用户可写的快捷键文件（``<user_config_dir>/shortcuts.ini``）。"""
    return get_user_config_dir(
        albuswall.__name__, albuswall.__author__,
    ) / file_name


def _parse_binding(value: str) -> ShortcutBinding:
    """把一条配置值解析成 :class:`ShortcutBinding`。

    解析规则**只有一份**，在 :meth:`albuswall.common.keymap.ShortcutRecord.
    from_ini` 里（配置器那股线读同一批文件，两份实现不允许漂移）。这里只把
    「键位文本」交给 Qt 的 ``parse_sequences`` 校验并规范化；结构问题由
    ``ShortcutRecord`` 抛 ``ValueError``。

    Raises:
        ValueError: 记录结构不合法（``ShortcutRecord.from_ini``）。
        InvalidKeySequence: 键位文本无法解析（``parse_sequences``）。
    """
    record = ShortcutRecord.from_ini(value)
    return ShortcutBinding(
        keys=parse_sequences(record.keys),
        args=record.args,
        kwargs=dict(record.kwargs),
    )


def _format_binding(command: str, binding: ShortcutBinding) -> str:
    """把绑定记录写成配置值（强制使用显式三元组形式）。

    与读取侧同源：写的是 ``ShortcutRecord.to_ini()`` 的规范形态，因此
    ``from_ini(to_ini(r))`` 恒等（见 ``tests/test_common_keymap.py``）。
    """
    try:
        return ShortcutRecord(
            keys=tuple(str(key) for key in binding.keys),
            args=tuple(binding.args),
            kwargs=dict(binding.kwargs),
        ).to_ini()
    except (TypeError, ValueError) as exc:
        raise ShortcutConfigError(
            f"shortcut binding for {command!r} is not writable: {exc}"
        ) from exc


def _coerce_binding(
        value: ShortcutBinding | str | KeySequence | Iterable[str | KeySequence],
) -> ShortcutBinding:
    """把「只是键位」的入参补成完整绑定记录。"""
    if isinstance(value, ShortcutBinding):
        return value
    return ShortcutBinding(keys=parse_sequences(value))


class ShortcutConfig:
    """读 / 写一份快捷键绑定表（默认表 + 用户覆盖）。

    每次 :meth:`load` 都**重新读盘**，不做缓存——「每次都从一个单独的配置
    文件加载」是这一层的明确契约，热改键位只要再 ``load`` 一次。

    Args:
        defaults_path: 默认表路径；传 ``None`` 表示没有默认表，不传则用
            :func:`default_config_path`。
        user_path: 用户表路径；传 ``None`` 表示不落盘（只读场景），不传则用
            :func:`user_config_path`。
        file_name: 用户文件名（``user_path`` 未显式给出时才参与计算）。
        section: 段名，默认 ``[shortcuts]``。
    """

    def __init__(
            self,
            *,
            defaults_path: Path | str | None | object = _DEFAULT,
            user_path: Path | str | None | object = _DEFAULT,
            file_name: str = DEFAULT_FILE_NAME,
            section: str = DEFAULT_SECTION,
    ) -> None:
        if not section or not section.strip():
            raise ValueError("ShortcutConfig: section must be non-empty")
        self.section = section
        self.file_name = file_name
        self._defaults_path = self._resolve_defaults(defaults_path)
        self._user_path = (
            user_config_path(file_name)
            if user_path is _DEFAULT
            else (None if user_path is None else Path(user_path))
        )

    @staticmethod
    def _resolve_defaults(
            value: Path | str | None | object,
    ) -> Path | None:
        """解析默认表路径；随包资源缺失时转成本层异常。"""
        if value is not _DEFAULT:
            return None if value is None else Path(value)
        try:
            return default_config_path()
        except (KeyError, FileNotFoundError, ValueError) as exc:
            raise ShortcutConfigError(
                f"default shortcut file is unavailable: {exc}"
            ) from exc

    # -- 路径 ---------------------------------------------------------

    @property
    def defaults_path(self) -> Path | None:
        return self._defaults_path

    @property
    def user_path(self) -> Path | None:
        return self._user_path

    # -- 读 -----------------------------------------------------------

    def load_defaults(
            self, *, strict: bool = True,
    ) -> dict[str, ShortcutBinding]:
        """只读默认表。没有默认表（``defaults_path=None``）时返回空表。"""
        path = self.defaults_path
        if path is None:
            return {}
        if not path.is_file():
            if strict:
                raise ShortcutConfigError(
                    f"default shortcut file not found: {path}"
                )
            logger.warning("default shortcut file not found: %s", path)
            return {}
        return self._read(path, strict=strict)

    def load_user(
            self, *, strict: bool = True,
    ) -> dict[str, ShortcutBinding]:
        """只读用户表；文件不存在时返回空表（等同于「全走默认」）。"""
        path = self.user_path
        if path is None or not path.is_file():
            return {}
        return self._read(path, strict=strict)

    def load(self, *, strict: bool = True) -> dict[str, ShortcutBinding]:
        """合并后的绑定表：默认表被用户表逐条覆盖。

        覆盖粒度是**命令**：用户写了 ``view.fullscreen = [[], [], {}]``
        就把该命令的默认键位整体清空；没写的命令继续用默认值。
        """
        merged = dict(self.load_defaults(strict=strict))
        merged.update(self.load_user(strict=strict))
        return merged

    # -- 写 -----------------------------------------------------------

    def save(
            self,
            entries: Mapping[
                str,
                ShortcutBinding | str | KeySequence
                | Iterable[str | KeySequence],
            ],
    ) -> Path:
        """把 ``entries``（命令 → 绑定记录）写进用户表，返回文件路径。

        值可以是完整 :class:`ShortcutBinding`，也可以只给键位（自动补空参数）。
        只写调用方给出的条目——惯例是传「与默认表不同的那些命令」
        （``ShortcutRegistry.persist`` 就是这么算的）。写盘是原子的
        （同目录临时文件 + ``os.replace``），中途失败不会留下半个文件。
        用户表里的注释不会保留：这是**状态文件**，格式由本类负责。
        """
        path = self.user_path
        if path is None:
            raise ShortcutConfigError(
                "no user path configured; cannot persist shortcuts"
            )

        lines = [_FILE_HEADER, "", f"[{self.section}]"]
        for command in sorted(entries):
            binding = _coerce_binding(entries[command])
            lines.append(
                f"{command} = {_format_binding(command, binding)}"
            )
        text = "\n".join(lines) + "\n"

        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, text)
        logger.debug("shortcuts persisted: %s (%d entries)", path, len(entries))
        return path

    def reset(self) -> None:
        """删除用户表，恢复「全部走默认」。文件不存在时静默返回。"""
        path = self.user_path
        if path is None:
            return
        with suppress(FileNotFoundError):
            path.unlink()
            logger.debug("shortcuts user file removed: %s", path)

    # -- 内部 ---------------------------------------------------------

    def _read(
            self,
            path: Path,
            *,
            strict: bool,
    ) -> dict[str, ShortcutBinding]:
        parser = configparser.ConfigParser(
            interpolation=None,
            allow_no_value=True,
            delimiters=("=",),
            strict=False,
        )
        # 命令路径大小写有意义：不要 configparser 默认的 lower()。
        parser.optionxform = str  # type: ignore[assignment]

        try:
            with path.open("r", encoding="utf-8") as handle:
                parser.read_file(handle, source=str(path))
        except (OSError, configparser.Error) as exc:
            raise ShortcutConfigError(
                f"failed to read shortcut config {path}: {exc}"
            ) from exc

        if not parser.has_section(self.section):
            if parser.sections():
                raise ShortcutConfigError(
                    f"{path}: missing [{self.section}] section"
                )
            return {}

        table: dict[str, ShortcutBinding] = {}
        for command, raw in parser.items(self.section):
            value = "" if raw is None else raw.strip()
            try:
                table[command] = _parse_binding(value)
            except (InvalidKeySequence, TypeError, ValueError) as exc:
                message = f"{path}: [{self.section}] {command}: {exc}"
                if strict:
                    raise ShortcutConfigError(message) from exc
                logger.warning("skipping bad shortcut entry: %s", message)
        return table


def _atomic_write(path: Path, text: str) -> None:
    """同目录临时文件 + ``os.replace``，保证目标文件要么旧要么新。"""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise
