# -*- coding: utf-8 -*-
"""
后台批次收尾 job。

提交侧只负责把 batch 提交并登记到 llm/summary/batch_id.json（非阻塞即返回），
本模块的 job 每 BATCH_SCAN_INTERVAL 秒扫描一次注册表里未终态的批次：
  - completed       -> 提取摘要、按业务存回 {summary_dir}/{日期}.md、更新对应 parquet 的 llmDone
  - failed/expired  -> 仅记日志
批次达到终态后，对应记录从注册表中删除（不再积压 history）。
失败批次的文章 llm_processing 仍为 False，可经 /fetch/retry 重新提交。

run_fetch.py 的 FastAPI lifespan 启动 batch_job_loop 作为守护线程；
也可单独调用 finalize_due_batches() 手动触发一轮扫描。
"""
import os
import threading
import time

import pandas as pd

from llm import batch as batch_mod
from common import common_tools

logger = batch_mod.logger

BATCH_SCAN_INTERVAL = 60  # 每 180s 扫描一次
_JOB_LOCK = threading.Lock()

# summary 输出根目录。默认指向 llm/summary；测试可覆盖。
SUMMARY_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "summary")


def _is_terminal(status) -> bool:
    return (status or "pending") in batch_mod.TERMINAL_STATUSES


def _process_record(record: dict) -> dict:
    """查询单个批次终态；completed 则落库摘要 + 更新 llmDone；更新并返回记录。"""
    if _is_terminal(record.get("status")):
        return record

    try:
        batched = batch_mod.get_batch(batch_mod.LLMProvider.OPEN_ROUTER, record["batch_id"])
    except Exception as e:  # 网络/瞬时错误：保留 pending，下次再查
        logger.warning("[BatchJob] 查询 batch_id=%s 失败: %s", record["batch_id"], e)
        return record

    status = batched.get("status") or "pending"
    record["status"] = status

    if not _is_terminal(status):
        return record  # 非终态，这次不写回，避免每 60s 重写

    if status == "completed":
        _finalize_completed(record, batched)
    else:
        logger.warning("[Batch] 批次 %s 终态=%s (business=%s) 未完成",
                       record["batch_id"], status, record.get("business"))
    return record


def _finalize_completed(record: dict, batched: dict) -> None:
    """completed 批次收尾：提取摘要 -> 存 summary 文件 -> 更新 parquet llmDone。"""
    business = record.get("business") or ""
    biz = batch_mod.BUSINESS.get(business)
    if not biz:
        logger.warning("[Batch] 注册表未知 business=%s，跳过收尾", business)
        return

    summaries_map = batch_mod.extract_summaries(batched)
    if not summaries_map:
        logger.warning("[Batch] batch_id=%s completed 但无摘要可取", record["batch_id"])
        return

    # 抽出成功条目：只保留能映射到摘要的 url
    ok_items = []
    for art in record.get("articles") or []:
        summary = summaries_map.get(art.get("custom_id"))
        if summary:
            ok_items.append({
                "url": art["url"],
                "summary": summary,
                "date": art.get("date") or batch_mod._date_str(None),
            })
    if not ok_items:
        return

    # 1. 写 summary：按日期分组合并，沿用 save_summary（append_mode=False 插顶部）
    from llm.summarizer import save_summary
    by_date: dict[str, list[str]] = {}
    for item in ok_items:
        by_date.setdefault(item["date"], []).append(item["summary"])

    summary_dir = biz["summary"]  # summary 目录键（如 wsj -> wall_street）
    for date_str, summaries in by_date.items():
        combined = "\n\n---\n\n".join(summaries)
        month_str = date_str[:7]
        save_path = os.path.join(SUMMARY_BASE, summary_dir, month_str, f"{date_str}.md")
        save_summary(combined, append_mode=False, save_path=save_path)
        logger.info("[Batch] business=%s month=%s date=%s 摘要已写 %d 篇", business, month_str, date_str, len(summaries))

    # 2. 更新对应来源 parquet：llmDone=True
    # 与 merge_and_save 共享 PARQUET_LOCK，串行读改写，避免并发写同一 parquet
    # 触发 Windows [Errno 22] Invalid argument 及丢更新。
    data_path = biz["data"]  # 相对进程 CWD
    if not os.path.exists(data_path):
        logger.warning("[Batch] parquet %s 不存在，无法更新 llmDone", data_path)
    else:
        with common_tools.PARQUET_LOCK:
            try:
                df = pd.read_parquet(data_path)
                if "url" in df.columns:
                    success_urls = {item["url"] for item in ok_items}
                    df.loc[df["url"].isin(success_urls), "llmDone"] = True
                    df.to_parquet(data_path, index=False)
                    logger.info("[Batch] 已更新 %s，%d 篇标记 llmDone=True", data_path, len(success_urls))
            except Exception as e:
                logger.warning("[Batch] 更新 parquet %s 失败: %s", data_path, e)


def _remove_record(batch_id: str) -> None:
    """从注册表删除一条记录（批次达到终态后不再保留）。"""
    with batch_mod._REGISTRY_LOCK:
        records = batch_mod.read_registry()
        records = [r for r in records if r.get("batch_id") != batch_id]
        batch_mod.write_registry(records)


def finalize_due_batches() -> int:
    """扫描注册表，收尾所有未终态批次。返回本轮处理（达终态后已删除）的批次数。"""
    records = batch_mod.read_registry()
    pending = [r for r in records if not _is_terminal(r.get("status"))]
    if not pending:
        return 0

    processed = 0
    with _JOB_LOCK:
        for record in pending:
            new = _process_record(record)
            if _is_terminal(new.get("status")):
                _remove_record(record.get("batch_id"))
                processed += 1
        return processed


def batch_job_loop(stop_event: threading.Event) -> None:
    """后台守护线程主体：每 BATCH_SCAN_INTERVAL 秒扫描一次；收到停止信号退出。"""
    logger.info("[BatchJob] 启动批次收尾 job，每 %ds 扫描一次", BATCH_SCAN_INTERVAL)
    while not stop_event.wait(BATCH_SCAN_INTERVAL):
        try:
            n = finalize_due_batches()
            if n:
                logger.info("[BatchJob] 本轮收尾 %d 个批次", n)
        except Exception as e:
            logger.error("[BatchJob] 扫描异常: %s", e, exc_info=True)
    logger.info("[BatchJob] 已停止。")