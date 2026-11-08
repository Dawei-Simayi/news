# -*- coding: utf-8 -*-
"""
业务事件定义
"""
from dataclasses import dataclass
from common.event_bus import ApplicationEvent


@dataclass
class BusinessFetchFinishedEvent(ApplicationEvent):
    """
    单个业务源抓取完成事件
    """
    business: str            # 对应的 BUSINESS 字典键 (如 bloomberg, wsj, ft 等)
    source: str = ""         # 触发抓取的原始来源标识 (如 wsj-print, barrons 等)
    success: bool = True     # 抓取是否成功
    error_msg: str = ""      # 失败时的错误信息
