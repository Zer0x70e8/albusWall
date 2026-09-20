#
"""
设备服务：监听设备连接事件，根据 TriggerConfig 中的设备触发器（device_trigger）
在匹配的设备插入时触发更新。
使用 pyudev 进行 Linux 设备监听，若不可用可替换为其他实现。
"""
import logging
from typing import Dict, Optional, Callable

# import pyudev

# from albuswall.common.enums import UpdateMode
from albuswall.dto.trigger import TriggerConfig


class DeviceService:
    """基于 pyudev 的设备触发器服务"""

    def __init__(self, update_callback: Callable[[int], None], logger: Optional[logging.Logger] = None):
        """
        :param update_callback: 当设备触发时调用的函数，参数为 source_id
        :param logger: 可选日志记录器
        """
        self.update_callback = update_callback
        self.logger = logger or logging.getLogger(__name__)

        # self._context = pyudev.Context()
        # self._monitor = pyudev.Monitor.from_netlink(self._context)
        # self._monitor.filter_by(subsystem='block')  # 监听块设备，可根据需要调整

        self._observer = None  # pyudev.MonitorObserver
        self._configs: Dict[int, TriggerConfig] = {}
        self._device_targets: Dict[str, int] = {}  # target -> source_id 映射（一个 target 可对应多个源，这里简化为一个）

    def start(self, configs: Dict[int, TriggerConfig]) -> None:
        """
        启动设备监听并加载已有配置。
        :param configs: 字典 {source_id: TriggerConfig}
        """
        # self._configs = configs.copy()
        # self._rebuild_target_map()
        #
        # self._observer = pyudev.MonitorObserver(self._monitor, self._device_event)
        # self._observer.start()
        # self.logger.info("设备监听服务已启动")

    def stop(self) -> None:
        """停止设备监听"""
        # if self._observer:
        #     self._observer.stop()
        #     self._observer = None
        # self._configs.clear()
        # self._device_targets.clear()
        # self.logger.info("设备监听服务已停止")

    def add_source(self, source_id: int, config: TriggerConfig) -> None:
        """添加或更新源配置"""
        # self._configs[source_id] = config
        # self._rebuild_target_map()

    def remove_source(self, source_id: int) -> None:
        """移除源配置"""
        # self._configs.pop(source_id, None)
        # self._rebuild_target_map()

    def update_source(self, source_id: int, config: TriggerConfig) -> None:
        """更新源配置"""
        # self.add_source(source_id, config)

    def _rebuild_target_map(self) -> None:
        """重新构建设备 target 到 source_id 的映射"""
        self._device_targets.clear()
        # for source_id, config in self._configs.items():
        #     if self._should_listen(config):
        #         target = config.device_trigger.target
        #         if target:
        #             # 如果一个 target 对应多个源，这里仅保留最后一个，实际可扩展为列表
        #             self._device_targets[target] = source_id
        #             self.logger.debug("注册设备 target '%s' -> 源 %d", target, source_id)

    def _should_listen(self, config: TriggerConfig) -> bool:
        """判断配置中是否启用了设备触发器"""
        # return (
        #         config.device_trigger.enabled
        #         and UpdateMode.DEVICE_TRIGGER in config.update_mode
        #         and config.device_trigger.target is not None
        # )

    # def _device_event(self, action: str, device: pyudev.Device) -> None:
    #     """设备事件回调"""
    #     if action != 'add':
    #         return
    #
    #     # 获取设备路径或标识（例如 /dev/sdb1）
    #     device_node = device.device_node
    #     if not device_node:
    #         return
    #
    #     # 检查是否匹配任何 target
    #     for target, source_id in self._device_targets.items():
    #         if target in device_node or target in device.get('ID_FS_UUID', ''):
    #             self.logger.info("检测到匹配设备 '%s'，触发源 %d 更新", device_node, source_id)
    #             try:
    #                 self.update_callback(source_id)
    #             except Exception as e:
    #                 self.logger.exception("更新源 %d 时出错: %s", source_id, e)
    #             # 若一个设备可能匹配多个 target，可继续循环，但这里假设唯一
    #             break
