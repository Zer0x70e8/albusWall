#
""""""

import datetime
import json
import re
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

from albuswall.common.enums import UpdateMode


@dataclass
class DeviceTrigger:
    """设备触发器配置"""
    id: Optional[int] = None  # 添加默认值
    enabled: bool = False
    target: Optional[str] = None  # 目标设备标识，例如设备路径或设备 ID


@dataclass
class ScheduledTrigger:
    """计划触发器配置"""
    id: Optional[int] = None  # 添加默认值
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
        match = re.match(r"(\d+)([hmd])", self.interval)
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
    """总触发配置，对应 trigger_config 字段（嵌套结构）"""
    id: Optional[int] = None  # 添加默认值
    update_mode: List[UpdateMode] = None  # 可同时选择多种模式，默认为空列表
    device_trigger: DeviceTrigger = None
    scheduled: ScheduledTrigger = None

    def __post_init__(self):
        # 确保复杂字段有默认实例，避免 None 引发错误
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
        """转换为字典，枚举值转为字符串以便 JSON 序列化"""
        # asdict 会递归包含所有字段，包括 id
        return {
            "id": self.id,
            "update_mode": [mode.value for mode in self.update_mode],
            "device_trigger": asdict(self.device_trigger),
            "scheduled": asdict(self.scheduled),
        }

    def to_json(self) -> str:
        """转换为 JSON 字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

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
