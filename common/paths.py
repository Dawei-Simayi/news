# -*- coding: utf-8 -*-
"""
项目统一路径常量。

所有 parquet 数据文件（爬虫落库的原始文章、LLM 总结回写的 llmDone 状态等
读写的都是同一份文件）统一放在项目根目录下的 `.data/` 中，文件名即业务键，
与 llm.batch.BUSINESS 的 key 一一对应：

    .data/bloomberg.parquet
    .data/wsj.parquet
    .data/wsj_print.parquet          （原 Wall_Street/print-edition.parquet）
    .data/economist.parquet
    .data/economist_weekly.parquet
    .data/barrons_weekly.parquet
    .data/ft.parquet
    .data/weeklyonstock.parquet

这样做的原因：
1. 数据与代码分离，各来源包目录里不再混放 parquet 二进制文件；
2. 原先 economist/data.parquet 与 economist/weekly.parquet、
   Wall_Street/data.parquet 与 print-edition.parquet 命名不统一，
   改为「业务键.parquet」后与 BUSINESS / 事件流里的来源标识完全对齐；
3. 路径统一走绝对路径，不再依赖启动时的当前工作目录（原先的
   "./bloomberg/data.parquet" 只能从项目根目录启动才正确）。
"""
import os

# 项目根目录（common/ 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 数据目录
DATA_DIR = os.path.join(PROJECT_ROOT, ".data")


def data_file(business: str) -> str:
    """返回某业务键对应的 parquet 绝对路径。

    :param business: 业务键，如 "wsj" / "economist_weekly"
    :return: 绝对路径，如 <root>/.data/wsj.parquet
    """
    return os.path.join(DATA_DIR, f"{business}.parquet")


def ensure_data_dir() -> str:
    """确保数据目录存在（首次运行/目录被误删时自动重建），返回目录路径。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR
