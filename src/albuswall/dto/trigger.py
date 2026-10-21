#
""""""

import datetime
import json
import re
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any

from albuswall.common.enums import UpdateMode

from .utils import dump_json, none_or


@dataclass
class DeviceTrigger:
    """设备触发器配置"""
    id: Optional[int] = None
    enabled: bool = False
    target: Optional[str] = None  # 目标设备标识，例如设备路径或设备 ID


@dataclass
class ScheduledTrigger:
    """计划触发器配置"""
    id: Optional[int] = None
    enabled: bool = False
    time: Optional[str] = None  # 当更新模式为 scheduled_time 时使用，格式 "HH:MM"
    interval: Optional[str] = None  # 当更新模式为 interval_time 时使用，格式如 "12h"、"30m"、"1d"

    def get_time(self) -> datetime.time:
        """解析 'HH:MM' 字符串为 time 对象"""
        if not self.time:
            raise ValueError("计划时间未设置")
        return datetime.datetime.strptime(self.time, "%H:%M").time()

    def get_interval(self) -> datetime.timedelta:
        """解析如 '12h'、'30m'、'1d' 的字符串为 timedelta"""
        if not self.interval:
            raise ValueError("间隔时间未设置")
        match = re.match(r"(\d+)([shmd])", self.interval)
        if not match:
            raise ValueError(f"无效的间隔格式: {self.interval}")
        value = int(match.group(1))
        unit = match.group(2)
        if unit == 'h':
            return datetime.timedelta(hours=value)
        elif unit == 'm':
            return datetime.timedelta(minutes=value)
        elif unit == 'd':
            return datetime.timedelta(days=value)
        elif unit == 's':
            return datetime.timedelta(seconds=value)
        raise ValueError(f"不支持的时间单位: {unit}")

    def to_cron_trigger_kwargs(self) -> Dict[str, Any]:
        """返回 APScheduler CronTrigger 所需的 hour/minute 参数"""
        t = self.get_time()
        return {"hour": t.hour, "minute": t.minute}

    def to_interval_trigger_kwargs(self) -> Dict[str, Any]:
        """返回 APScheduler IntervalTrigger 所需的 seconds 参数"""
        delta = self.get_interval()
        return {"seconds": delta.total_seconds()}


@dataclass
class TriggerConfig:
    """总触发配置，对应 trigger_config 字段（嵌套结构）。

    字段类型刻意写非 ``Optional``：这些字段在语义上"永远有值"，
    由 ``default_factory`` 提供默认实例；``__post_init__`` 里的
    ``is None`` 分支只是防御动态构造（``TriggerConfig(update_mode=None)``）
    绕过默认工厂，不参与类型收窄，故不写进注解。
    """
    id: Optional[int] = None
    update_mode: List[UpdateMode] = field(default_factory=lambda: [UpdateMode.MANUAL])
    device_trigger: DeviceTrigger = field(default_factory=DeviceTrigger)
    scheduled: ScheduledTrigger = field(default_factory=ScheduledTrigger)

    # noinspection unreachable-code
    def __post_init__(self):
        # 防御性兜底：外部显式传 None 时（绕过 default_factory）回落到默认实例。
        # 对类型检查器而言这些分支"不可能命中"（字段非 Optional），保留是为了
        # 接住 ``TriggerConfig(update_mode=None)`` 这类动态调用。
        if self.update_mode is None:
            self.update_mode = [UpdateMode.MANUAL]
        if self.device_trigger is None:
            self.device_trigger = DeviceTrigger()
        if self.scheduled is None:
            self.scheduled = ScheduledTrigger()

    def is_void(self) -> bool:
        """
        判断当前配置是否不包含任何可用的自动触发器。
        返回 True 表示配置无效，应被忽略。
        """
        has_device_trigger = (
                UpdateMode.DEVICE_TRIGGER in self.update_mode
                and self.device_trigger.enabled
                and self.device_trigger.target is not None
        )
        has_scheduled_trigger = (
                self.scheduled.enabled
                and (
                        UpdateMode.SCHEDULED_TIME in self.update_mode
                        or UpdateMode.INTERVAL_TIME in self.update_mode
                )
        )
        return not (has_device_trigger or has_scheduled_trigger)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，保留 ``id`` 与 ``update_mode`` 的 ``Enum`` 原样。

        契约（见 ``CONVENTIONS.md`` · TriggerConfig.to_dict）：
        * ``id`` 保留；
        * ``update_mode`` 保持 ``Enum`` 原样，不转 ``.value``；
        * 存储形态**不**走本方法，走 ``IngestSourceFormData.to_trigger_config()``。
          运行时视图与存储视图职责分离，运行时字段不污染存储。
        """
        return {
            "id": self.id,
            "update_mode": list(none_or(self.update_mode, list)),
            "device_trigger": asdict(none_or(self.device_trigger, DeviceTrigger)),
            "scheduled": asdict(none_or(self.scheduled, ScheduledTrigger)),
        }

    def to_json(self) -> str:
        """转换为 JSON 字符串；``Enum`` 成员由 ``to_json`` 兜底转 ``value``。"""
        return dump_json(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TriggerConfig":
        # 提取 id（若不存在则为 None）
        config_id = data.get("id")

        # 处理 update_mode
        raw_modes = data.get("update_mode", ["manual"])
        if isinstance(raw_modes, str):
            raw_modes = [raw_modes]
        if not isinstance(raw_modes, list):
            raw_modes = ["manual"]
        try:
            update_modes = [UpdateMode(mode) for mode in raw_modes]
        except ValueError:
            update_modes = [UpdateMode.MANUAL]

        # 处理 device_trigger
        device_data = data.get("device_trigger") or {}
        # 确保只传递 DeviceTrigger 定义的字段，忽略多余键
        device_trigger = DeviceTrigger(
            id=device_data.get("id"),
            enabled=device_data.get("enabled", False),
            target=device_data.get("target"),
        )

        # 处理 scheduled
        scheduled_data = data.get("scheduled") or {}
        scheduled = ScheduledTrigger(
            id=scheduled_data.get("id"),
            enabled=scheduled_data.get("enabled", False),
            time=scheduled_data.get("time"),
            interval=scheduled_data.get("interval"),
        )

        return cls(
            id=config_id,
            update_mode=update_modes,
            device_trigger=device_trigger,
            scheduled=scheduled,
        )

    @classmethod
    def from_json(cls, json_str: Optional[str]) -> Optional["TriggerConfig"]:
        """从 JSON 字符串构建实例，兼容 NULL 值"""
        if json_str is None or json_str == "":
            return None  # 或者返回一个默认实例，取决于业务需求
        try:
            data = json.loads(json_str)
            return cls.from_dict(data)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            # 可记录日志或抛出更有意义的异常
            raise ValueError(f"无效的 trigger_config JSON: {e}") from e
