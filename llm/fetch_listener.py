# -*- coding: utf-8 -*-
"""
业务爬取完成监听器 (消费端)
仿造 script.py 实现：监听到某一个 business 爬取结束后，仅针对该 business 的待处理文章调用 call_llm 进行总结并落盘。
"""
import os
import sys
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from common.event_bus import event_listener
from common.events import BusinessFetchFinishedEvent
from common.common_tools import PARQUET_LOCK
from llm.batch import BUSINESS, submit_analysis_batches
from llm.provider import LLMProvider
from llm.summarizer import call_llm
from llm.agent_helper import save_summary_item
from logger import get_logger

logger = get_logger(__name__)

DEFAULT_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt", "prompt.md")


def summarize_business(
    business: str,
    provider: LLMProvider = LLMProvider.OPEN_ROUTER,
    prompt_path: str = DEFAULT_PROMPT_PATH,
) -> dict:
    """
    针对单个业务源进行总结与落盘，逻辑仿造 script.py。

    :param business: 业务源标识，如 'wsj', 'bloomberg', 'ft', 等
    :param provider: LLM 提供者，默认 LLMProvider.AG_TOOL
    :param prompt_path: 提示词模板路径
    :return: 统计字典，如 {"total": 5, "success": 5, "failed": 0}
    """
    if business not in BUSINESS:
        logger.warning(f"[{business}] 未知业务标识，跳过总结。可用业务: {list(BUSINESS.keys())}")
        return {"total": 0, "success": 0, "failed": 0}

    parquet_path = BUSINESS[business]["data"]
    if not os.path.exists(parquet_path):
        logger.warning(f"[{business}] Parquet 数据文件不存在: {parquet_path}")
        return {"total": 0, "success": 0, "failed": 0}

    with PARQUET_LOCK:
        try:
            df = pd.read_parquet(parquet_path)
        except Exception as e:
            logger.error(f"[{business}] 读取 Parquet 失败: {e}")
            return {"total": 0, "success": 0, "failed": 0}

    if "llmDone" not in df.columns:
        df["llmDone"] = False

    has_content = df["content"].apply(lambda c: isinstance(c, str) and len(c.strip()) > 0)
    pending = df[(df["llmDone"] != True) & has_content].copy()

    total = len(pending)
    logger.info(f"[{business}] 监听到完成事件，发现 {total} 篇待总结文章")

    if total == 0:
        logger.info(f"[{business}] 无待处理文章，总结流程结束。")
        return {"total": 0, "success": 0, "failed": 0}

    # 若模型提供商为 OpenRouter，切换为异步批量提交
    if provider == LLMProvider.OPEN_ROUTER:
        articles = pending.to_dict(orient="records")
        submitted = submit_analysis_batches(articles, business)
        logger.info(f"[{business}] OpenRouter 批处理已提交 {len(submitted)} 个批次（共 {total} 篇待分析文章）。后续由后台 batch_job 处理并回写。")
        return {"total": total, "submitted_batches": len(submitted), "success": len(submitted), "failed": 0}

    success = 0
    failed = 0

    for idx, (row_idx, row) in enumerate(pending.iterrows(), start=1):
        url = str(row.get("url", ""))
        timestamp = str(row.get("timestamp", ""))
        content = str(row.get("content", ""))

        article_input = (
            f"source: {business}\n"
            f"timestamp: {timestamp}\n"
            f"url: {url}\n\n"
            f"content:\n{content}"
        )

        logger.info(f"[{business}] [{idx}/{total}] 正在调用 API 总结: {url[:80]} ...")

        try:
            summary = call_llm(article_input, provider=provider, save_path=prompt_path)
            ok = save_summary_item(
                business=business,
                url=url,
                timestamp=timestamp,
                summary=summary,
            )
            if ok:
                success += 1
                logger.info(f"[{business}] [{idx}/{total}] ✅ 已保存并标记 llmDone=True")
            else:
                failed += 1
                logger.warning(f"[{business}] [{idx}/{total}] ⚠️ save_summary_item 返回 False")
        except Exception as e:
            failed += 1
            logger.error(f"[{business}] [{idx}/{total}] ❌ 总结失败 {url}: {e}")

    logger.info(f"[{business}] 总结消费完成 | 总共: {total} 篇 | 成功: {success} 篇 | 失败: {failed} 篇")
    return {"total": total, "success": success, "failed": failed}


@event_listener(BusinessFetchFinishedEvent, is_async=True)
def on_business_fetch_finished(event: BusinessFetchFinishedEvent) -> None:
    """
    对标 Spring @EventListener(BusinessFetchFinishedEvent) + @Async
    当某个业务抓取结束时触发，只消费该业务。
    """
    if not event.success:
        logger.warning(f"[EventConsumer] 业务源 '{event.business}' 抓取未成功，跳过总结。原因: {event.error_msg}")
        return

    logger.info(f"[EventConsumer] 监听到 '{event.business}' (源: {event.source}) 抓取完成事件，开始异步触发总结...")
    summarize_business(business=event.business, provider=LLMProvider.OPEN_ROUTER)
