#
"""源域命令：``source.*``（读观测 + 写命令 + 手动挂载 / 刷新）。

线程语义（本层**不做任何线程处理**）
-----------------------------------
handler 跑在 IPC 工作线程（``ipc-conn``）；需要主线程属主状态的写操作由服务
的写入口自己搬回主线程（``service/main_loop_service.py``）。所以：

* ``create`` / ``update`` / ``delete`` / ``enable`` / ``disable`` 走
  ``SourceService`` —— 已带线程边界；
* ``trash`` / ``restore`` 走 ``SourceTrashService`` —— 同样已带边界
  （软删 / 恢复都会改跟踪集）；``purge`` 例外，它不碰状态层，线程无关；
* ``refresh`` / ``mount`` 走 ``SourceStateService`` —— 同样已带边界；
* ``tasks`` / ``scan`` / ``stop`` 走 ``SourceRuntime`` —— 它自己也带边界
  （任务表只允许主线程改）；
* ``list`` / ``get`` 是纯仓储读，**不平移**（读命令不该陪写命令排队）。

异常同样不在这里包：服务抛的 ``SourceError`` 家族由
``service/command/dispatch_errors.py`` 统一翻译成带 ``code`` / ``exit_code``
的 ``DispatchError``。参数级的错用 ``_params.as_*`` 抛 ``BadRequest``。

依赖纪律：只 ``container.require`` 四个 **service**，不碰 repo / DTO 之外的
基础设施（见 ``handlers/_protocol.py``）。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from enum import Enum
from typing import Any, Optional

from albuswall.command import CommandRegistry
from albuswall.command.exceptions import BadRequest
from albuswall.common.enums import FileTypeCheckMode
from albuswall.core import Container
from albuswall.dto.source.command import IngestSourceCreate, IngestSourceUpdate
from albuswall.dto.source.query import IngestSource
from albuswall.service.source.errors import SourceNotFound

from ._namespace import register_namespace
from ._params import as_bool, as_int, flag, opt, pos

__all__: list[str] = []

#: 允许通过命令设置的文件类型检查方式（``NONE`` 是 Python 侧哨兵，不开放）。
_FILE_TYPE_CHECK_CHOICES = ("suffix", "magic", "off", "magic_lax")


# ==================== 序列化 / 入参收口 ====================

def _row(source: IngestSource) -> dict[str, Any]:
    """``IngestSource`` → JSON-able dict。

    只做两件事：``dataclasses.asdict`` 摊平字段；把 ``FileTypeCheckMode``
    枚举转成它的值（枚举对象过不了 ``json.dumps``）。``trigger_config`` 在
    DTO 里已经是存储形态（JSON-able dict），不需要再处理。
    """
    data = dataclasses.asdict(source)
    mode = source.file_type_check
    data["file_type_check"] = mode.value if isinstance(mode, Enum) else None
    return data


def _as_str_list(value: Any, *, field: str) -> list[str]:
    """``["png", "jpg"]`` / ``"png,jpg"`` → ``["png", "jpg"]``。

    CLI 传的是逗号分隔字符串，IPC / GUI 传的是数组；两种都接受，空值给空表
    （"不限制扩展名"是合法意图）。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, Iterable):
        items: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise BadRequest(f"{field}: expected strings, got {item!r}")
            items.append(item)
        return items
    raise BadRequest(f"{field}: expected a list or comma-separated string")


def _as_optional_bool(value: Any, *, field: str) -> Optional[bool]:
    """PATCH 语义的布尔：``None`` = 不修改，其余按 ``as_bool`` 归一。"""
    if value is None:
        return None
    return as_bool(value, field=field)


def _as_file_type_check(value: Any) -> Optional[FileTypeCheckMode]:
    """``"suffix"`` / ``"magic"`` … → 枚举；``None`` = 不修改。"""
    if value is None:
        return None
    if isinstance(value, FileTypeCheckMode):
        return value
    text = str(value).strip().lower()
    if text not in _FILE_TYPE_CHECK_CHOICES:
        raise BadRequest(
            f"file_type_check: {value!r} not in {_FILE_TYPE_CHECK_CHOICES!r}"
        )
    return FileTypeCheckMode(text)


# ==================== 装配 ====================

def setup(registry: CommandRegistry, container: Container) -> None:
    source_service = container.require("source_service")
    state_service = container.require("source_state_service")
    # ``source_runtime`` 同样是 service 层对象（service/source/runtime/service.py）：
    # 它持有扫描任务表，是运行控制命令的唯一入口。
    runtime = container.require("source_runtime")
    # 回收站三件套（trash / restore / purge）只认它：软删与恢复的线程边界、
    # 异步 purge 的调度都在服务内部，handler 只做参数收口。
    source_trash_service = container.require("source_trash_service")

    # ---------------- 读取 ----------------

    @registry.command(
        "source.list",
        help_="列出所有导入源（含虚拟根 / 手动导入源，按 id 升序）",
        params=(),
    )
    def source_list() -> list[dict]:
        return [_row(source) for source in source_service.list_sources()]

    @registry.command(
        "source.get",
        help_="查看单个导入源（不存在时返回 null）",
        params=(pos("source_id", type=int, help="导入源 id"),),
    )
    def source_get(source_id: int) -> Optional[dict]:
        source_id = as_int(source_id, field="source_id", minimum=0)
        source = source_service.get_source(source_id)
        return None if source is None else _row(source)

    # ---------------- 写入（服务侧已回主线程） ----------------

    @registry.command(
        "source.create",
        help_="新建导入源，返回新 id",
        params=(
            pos("title", help="显示名"),
            pos("source_path", help="源目录（空串会被拒绝）"),
            opt("description", default=None, help="备注"),
            opt("target_path", default=None, help="目标路径（auto_mount 用）"),
            opt("mount_point", default=None, help="挂载点（auto_mount 用）"),
            opt("file_types", default=None,
                help="文件后缀白名单，逗号分隔（如 png,jpg）；空 = 不限制"),
            flag("auto_mount", help="需要时自动挂载"),
            flag("disabled", help="建好后先禁用"),
        ),
    )
    def source_create(
            title: str,
            source_path: str,
            description: Optional[str] = None,
            target_path: Optional[str] = None,
            mount_point: Optional[str] = None,
            file_types: Any = None,
            auto_mount: bool = False,
            disabled: bool = False,
    ) -> dict:
        new_id = source_service.create(IngestSourceCreate(
            title=title,
            source_path=source_path,
            description=description,
            target_path=target_path,
            mount_point=mount_point,
            auto_mount=bool(auto_mount),
            file_types=_as_str_list(file_types, field="file_types"),
            disabled=bool(disabled),
        ))
        return {"id": new_id}

    @registry.command(
        "source.update",
        help_="局部更新导入源（只改传入的字段）",
        params=(
            pos("source_id", type=int, help="导入源 id"),
            opt("title", default=None, help="显示名"),
            opt("description", default=None, help="备注"),
            opt("source_path", default=None, help="源目录"),
            opt("target_path", default=None, help="目标路径"),
            opt("mount_point", default=None, help="挂载点"),
            opt("file_types", default=None,
                help="文件后缀白名单，逗号分隔；空串 = 清空白名单"),
            opt("file_type_check", default=None,
                help="文件类型检查方式：" + " / ".join(_FILE_TYPE_CHECK_CHOICES)),
            opt("auto_mount", default=None, help="是否自动挂载（true / false）"),
        ),
    )
    def source_update(
            source_id: int,
            title: Optional[str] = None,
            description: Optional[str] = None,
            source_path: Optional[str] = None,
            target_path: Optional[str] = None,
            mount_point: Optional[str] = None,
            file_types: Any = None,
            file_type_check: Any = None,
            auto_mount: Any = None,
    ) -> dict:
        source_id = as_int(source_id, field="source_id", minimum=0)

        # PATCH 语义：没传的字段保持 ``UNSET``（= 不改）；显式传 None 才是置空。
        # ``opt`` 的默认值就是 None，所以这里"None = 不传"是唯一可行的映射。
        patch: dict[str, Any] = {}
        for name, value in (
            ("title", title),
            ("description", description),
            ("source_path", source_path),
            ("target_path", target_path),
            ("mount_point", mount_point),
        ):
            if value is not None:
                patch[name] = value
        if file_types is not None:
            patch["file_types"] = _as_str_list(file_types, field="file_types")
        mode = _as_file_type_check(file_type_check)
        if mode is not None:
            patch["file_type_check"] = mode
        auto = _as_optional_bool(auto_mount, field="auto_mount")
        if auto is not None:
            patch["auto_mount"] = auto

        if not patch:
            raise BadRequest("source.update: nothing to update")

        source_service.update(source_id, IngestSourceUpdate(**patch))
        return {"id": source_id, "changed": sorted(patch)}

    @registry.command(
        "source.enable",
        help_="启用导入源（幂等）",
        params=(pos("source_id", type=int, help="导入源 id"),),
    )
    def source_enable(source_id: int) -> dict:
        source_id = as_int(source_id, field="source_id", minimum=0)
        source_service.enable(source_id)
        return {"id": source_id, "enabled": True}

    @registry.command(
        "source.disable",
        help_="禁用导入源（幂等；DB 行保留）",
        params=(pos("source_id", type=int, help="导入源 id"),),
    )
    def source_disable(source_id: int) -> dict:
        source_id = as_int(source_id, field="source_id", minimum=0)
        source_service.disable(source_id)
        return {"id": source_id, "enabled": False}

    @registry.command(
        "source.delete",
        help_="立即永久删除导入源（仅限不含资产的源；进回收站请用 source.trash）",
        params=(pos("source_id", type=int, help="导入源 id"),),
    )
    def source_delete(source_id: int) -> dict:
        source_id = as_int(source_id, field="source_id", minimum=0)
        source_service.delete(source_id)
        return {"id": source_id, "deleted": True}

    # ---------------- 回收站（软删 / 恢复 / 立即 purge） ----------------
    # 与 ``source.delete`` 的分工是本域唯一容易搞混的地方，两者语义不同：
    #
    #   source.delete  → 立刻物理删（要求源里没有资产，否则 SourceDeleteFailed）
    #   source.trash   → 软删；源立刻从列表 / 跟踪集消失，DB 行留到 debounce
    #                    之后由 Reconciler 异步 purge；期间可 source.restore 救回
    #
    # UI 的「删除」按钮走 source.trash（可撤销）；source.delete 留给「确定这个
    # 空源不要了」的场景（脚本 / 初始化）。

    @registry.command(
        "source.trash",
        help_="把导入源移进回收站（软删，可 source.restore 撤销）",
        params=(pos("source_id", type=int, help="导入源 id"),),
    )
    def source_trash(source_id: int) -> dict:
        source_id = as_int(source_id, field="source_id", minimum=0)
        # ``False`` = 已经是删除态 / 行不存在 —— 幂等，不报错。
        return {
            "id": source_id,
            "trashed": source_trash_service.delete_source(source_id),
        }

    @registry.command(
        "source.restore",
        help_="从回收站恢复导入源（已被 purge 的源救不回来）",
        params=(pos("source_id", type=int, help="导入源 id"),),
    )
    def source_restore(source_id: int) -> dict:
        source_id = as_int(source_id, field="source_id", minimum=0)
        return {
            "id": source_id,
            "restored": source_trash_service.restore_source(source_id),
        }

    @registry.command(
        "source.purge",
        help_="立即物理清除回收站里的源（跳过 debounce，不等待）",
        hidden=True,
        params=(pos("source_id", type=int, help="导入源 id"),),
    )
    def source_purge(source_id: int) -> dict:
        source_id = as_int(source_id, field="source_id", minimum=0)
        purge = source_trash_service.purge_source_now(source_id)
        return {
            "id": source_id,
            "purged": purge.purged,
            "asset_ids": list(purge.asset_ids),
            "asset_uuids": [str(u) for u in purge.asset_uuids],
        }

    # ---------------- 运行时动作 ----------------

    @registry.command(
        "source.refresh",
        help_="重评估并重扫单个导入源（跟踪集与 DB 对齐后换 seq 重扫）",
        params=(pos("source_id", type=int, help="导入源 id"),),
    )
    def source_refresh(source_id: int) -> dict:
        source_id = as_int(source_id, field="source_id", minimum=0)
        state_service.refresh_source(source_id)
        return {"id": source_id, "refreshed": True}

    @registry.command(
        "source.mount",
        help_="显式执行一次自动挂载（失败会报错，不是静默返回）",
        params=(pos("source_id", type=int, help="导入源 id"),),
    )
    def source_mount(source_id: int) -> dict:
        source_id = as_int(source_id, field="source_id", minimum=0)
        state_service.mount_source(source_id)
        return {"id": source_id, "mounted": True}

    # ---------------- 运行时：观测 + 控制 ----------------

    @registry.command(
        "source.tasks",
        help_="查看扫描任务状态（不给 source_id 则列出全部）",
        params=(
            opt("source_id", type=int, default=None,
                help="只看某个源（省略 = 全部）"),
        ),
    )
    def source_tasks(source_id: Optional[int] = None) -> list[dict]:
        if source_id is not None:
            source_id = as_int(source_id, field="source_id", minimum=0)
        return [
            dataclasses.asdict(snapshot)
            for snapshot in runtime.status(source_id)
        ]

    @registry.command(
        "source.scan",
        help_="强制启动一次扫描（不看跟踪集，立即换 seq 提交）",
        params=(pos("source_id", type=int, help="导入源 id"),),
    )
    def source_scan(source_id: int) -> dict:
        source_id = as_int(source_id, field="source_id", minimum=0)
        source = source_service.get_source(source_id)
        if source is None:
            raise SourceNotFound(source_id)
        seq = runtime.force_scan(source)
        # seq=None：runtime 未启动（装配早期 / 已 shutdown），如实回报。
        return {"id": source_id, "accepted": seq is not None, "seq": seq}

    @registry.command(
        "source.stop",
        help_="停止该源的扫描任务（不打断已在跑的扫描）",
        params=(pos("source_id", type=int, help="导入源 id"),),
    )
    def source_stop(source_id: int) -> dict:
        source_id = as_int(source_id, field="source_id", minimum=0)
        return {"id": source_id, "stopped": runtime.stop_scan(source_id)}

    # ---- 命名空间入口（与 ``task`` / 其余域同一形状）----
    # ``albuswall source create ...`` == ``albuswall source.create ...``。
    # 必须在叶子命令之后注册：用法清单与转发都读注册表。
    register_namespace(
        registry, "source",
        help_="导入源命令入口：源的读写与回收站（source <sub> ...）",
        hint="删除整条导入源用 source trash（进回收站）；"
             "删单张照片用 albuswall asset trash。",
    )
