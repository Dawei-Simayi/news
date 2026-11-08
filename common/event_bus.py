# -*- coding: utf-8 -*-
"""
事件驱动总线机制 (对标 Spring 的 ApplicationEvent / ApplicationListener / @EventListener)
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, TypeVar
from logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound="ApplicationEvent")


@dataclass
class ApplicationEvent:
    """事件基类 (对标 Spring ApplicationEvent)"""
    timestamp: datetime = field(default_factory=datetime.now, init=False)


class ApplicationListener:
    """监听器抽象基类 (对标 Spring ApplicationListener<E>)"""
    def on_application_event(self, event: ApplicationEvent) -> None:
        raise NotImplementedError


class EventPublisher:
    """
    事件发布与调度器 (对标 Spring ApplicationEventPublisher)
    支持同步消费与线程池异步消费 (对标 @Async)
    """
    def __init__(self, max_workers: int = 4):
        # 存储映射: event_cls -> [(listener_callable, is_async)]
        self._listeners: Dict[Type[ApplicationEvent], List[Tuple[Callable, bool]]] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="event-consumer")

    def register(
        self,
        event_type: Type[T],
        listener: Callable[[T], None],
        is_async: bool = True
    ) -> None:
        """
        注册监听器。
        :param event_type: 监听的事件类型
        :param listener: 回调函数或包含 on_application_event 的对象
        :param is_async: 是否异步执行（默认 True，不阻塞发布方）
        """
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append((listener, is_async))
        logger.info(f"[EventBus] 已注册监听器 {getattr(listener, '__name__', str(listener))} 监听 {event_type.__name__} (async={is_async})")

    def publish_event(self, event: ApplicationEvent) -> None:
        """
        发布事件，通知所有订阅了该事件类型（或其父类型）的监听器
        :param event: 事件实例
        """
        event_cls = type(event)
        targets: List[Tuple[Callable, bool]] = []

        for registered_type, listeners in self._listeners.items():
            if issubclass(event_cls, registered_type):
                targets.extend(listeners)

        if not targets:
            logger.debug(f"[EventBus] 事件 {event_cls.__name__} 无注册监听器。")
            return

        for callback, is_async in targets:
            if is_async:
                self._executor.submit(self._dispatch, callback, event)
            else:
                self._dispatch(callback, event)

    def _dispatch(self, callback: Callable, event: ApplicationEvent) -> None:
        try:
            if isinstance(callback, ApplicationListener):
                callback.on_application_event(event)
            else:
                callback(event)
        except Exception as e:
            cb_name = getattr(callback, "__name__", str(callback))
            logger.error(f"[EventBus] 监听器 {cb_name} 处理事件 {type(event).__name__} 异常: {e}", exc_info=True)


# 全局事件发布器单例
event_publisher = EventPublisher()


def event_listener(event_type: Type[T], is_async: bool = True):
    """
    函数装饰器 (对标 Spring @EventListener)
    用法：
        @event_listener(BusinessFetchFinishedEvent, is_async=True)
        def handle_fetch(event: BusinessFetchFinishedEvent):
            ...
    """
    def decorator(fn: Callable[[T], None]):
        event_publisher.register(event_type, fn, is_async=is_async)
        return fn
    return decorator
