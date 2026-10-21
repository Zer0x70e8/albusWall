#
"""跨层常量：按域分文件，禁止集中式 ``common/constants.py`` 垃圾桶。"""

# 本包**不做**汇总 re-export：消费方显式 import 域模块
# （例如 ``from albuswall.common.constants.thumbnail import ALL_SPECS``），
# 避免又长成一个"常量总入口"。
