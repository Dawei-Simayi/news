# -*- coding: utf-8 -*-
"""
Lock 状态管理器 (common/lock_manager.py)

极简单一状态机制：
- 爬取中 : lock 文件内容为 "FETCHING" (Agent 遇到自动跳过避让)
- 非爬取/空闲 : lock 文件内容为空 ""
"""
import os
from contextlib import contextmanager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_DIR = os.path.join(PROJECT_ROOT, "lock")

# 唯一的活跃状态符号：爬取中
STATUS_FETCHING = "FETCHING"
STATUS_IDLE = ""

# 业务源名称映射与归一化
SOURCE_ALIAS_MAP = {
    "wsj-print": "wsj_print",
    "economist-weekly": "economist_weekly",
    "barrons": "barrons_weekly",
}


def normalize_source_name(source: str) -> str:
    """归一化业务源标识，使其与 BUSINESS 配置及锁文件名保持一致。"""
    source_clean = source.strip().lower()
    return SOURCE_ALIAS_MAP.get(source_clean, source_clean)


def _ensure_lock_dir():
    """确保 lock 目录存在。"""
    if not os.path.exists(LOCK_DIR):
        os.makedirs(LOCK_DIR, exist_ok=True)


def get_lock_file_path(source: str) -> str:
    """获取指定业务源对应的 lock 文件绝对路径。"""
    _ensure_lock_dir()
    normalized = normalize_source_name(source)
    return os.path.join(LOCK_DIR, f"{normalized}.lock")


def set_lock_status(source: str, status: str) -> None:
    """
    更新指定业务源的状态符号。
    :param source: 业务源（如 'bloomberg', 'wsj' 等）
    :param status: 'FETCHING' 或 ''
    """
    lock_file = get_lock_file_path(source)
    try:
        with open(lock_file, "w", encoding="utf-8") as f:
            f.write(status.strip())
    except Exception as e:
        print(f"[Warning] 写入锁文件 {lock_file} 失败: {e}")


def get_lock_status(source: str) -> str:
    """获取指定业务源当前状态，若在爬取中返回 'FETCHING'，否则返回 ''。"""
    lock_file = get_lock_file_path(source)
    if not os.path.exists(lock_file):
        return STATUS_IDLE
    try:
        with open(lock_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return STATUS_IDLE


def is_fetching(source: str) -> bool:
    """判断指定业务源当前是否正处于爬取中 (FETCHING)。"""
    return get_lock_status(source) == STATUS_FETCHING


def clear_lock(source: str) -> None:
    """清空指定业务源的锁文件。"""
    set_lock_status(source, STATUS_IDLE)


@contextmanager
def fetch_scope(source: str):
    """
    爬虫抓取执行上下文管理器：
    进入时写入 FETCHING，退出时（无论正常结束还是抛出异常）清空文件。
    """
    set_lock_status(source, STATUS_FETCHING)
    try:
        yield
    finally:
        clear_lock(source)
