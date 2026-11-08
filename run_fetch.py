# -*- coding: utf-8 -*-
import ast
import json
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional
import traceback
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import uvicorn
import Wall_Street
import barrons
import bloomberg
import common
from common.common_tools import (
    PARQUET_LOCK,
    read_existing_urls,
    send_feishu_message,
)
from common.lock_manager import fetch_scope

import economist
import ft
from logger import get_logger

import numpy as np
import pandas as pd

from llm.batch import BUSINESS, submit_analysis_batches
from llm.batch_job import batch_job_loop
from common.event_bus import event_publisher
from common.events import BusinessFetchFinishedEvent
import llm.fetch_listener  # 注册业务抓取完成事件监听器

logger = get_logger(__name__)


def merge_and_save(new_articles: list[dict], save_path: str, set_llm_done: bool = True) -> None:
    """
    将新文章与本地 parquet 数据合并、去重、排序后保存。

    :param new_articles: 新文章列表
    :param save_path: parquet 文件保存路径
    :param set_llm_done: 是否将新文章标记为 llmDone=True。为 False 时保留文章自身的 llmDone 值（通常为 False）。
    """
    if len(new_articles) == 0:
        return

    # 与后台批次收尾（batch_job._finalize_completed）共享同一把锁，串行读改写 parquet，
    # 避免 Windows 下并发写同一文件触发 [Errno 22] Invalid argument / 丢更新。
    with PARQUET_LOCK:
        content_list: list[dict] = []

        if os.path.exists(save_path):
            try:
                df_existing = pd.read_parquet(save_path)
                content_list = df_existing.to_dict(orient="records")
                logger.info(f"成功读取本地 Parquet 历史数据，共 {len(df_existing)} 条。")
            except Exception as e:
                logger.warning(f"读取本地 Parquet 失败，将重新创建。错误: {e}")

        content_list.extend(new_articles)

        if content_list:
            df_final = pd.DataFrame(content_list)
            df_final.drop_duplicates(subset=["url"], keep="first", inplace=True)
            new_urls = {a["url"] for a in new_articles}
            if set_llm_done:
                df_final.loc[df_final["url"].isin(new_urls), "llmDone"] = True
            df_final.sort_values("timestamp", ascending=False, inplace=True)
            # 数据目录可能不存在（首次运行或手动清理过），先确保父目录存在
            os.makedirs(os.path.dirname(os.path.abspath(save_path)) or ".", exist_ok=True)
            df_final.to_parquet(save_path, engine="pyarrow", index=False)
            logger.info(f"数据已合并、去重并保存至 {save_path} | 当前总存量: {len(df_final)} 条。")
        else:
            logger.info("没有数据，未对文件进行写入。")


# 抓取/入库与 LLM 批次汇总在 run_fetch 内已解耦：
# 各 fetch 函数只负责抓取、merge 入库（llmDone=False）、提交批次（非阻塞，登记到
# llm/summary/batch_id.json）后立即返回；后台 job（llm/batch_job.py）每 60s 扫描
# 注册表，等批次 completed 后提取汇总、按业务存 summary、更新 llmDone。


def retry_failed_articles(business: str = "wsj") -> int:
    """找出该业务 parquet 中 llmDone=False 且有正文的文章，重新提交批次（异步收尾）。

    :param business: 业务键（见 llm.batch.BUSINESS）
    :return: 提交的批次数
    """
    if business not in BUSINESS:
        raise KeyError(f"未知业务键: {business}，可用: {list(BUSINESS)}")
    data_path = BUSINESS[business]["data"]
    df = pd.read_parquet(data_path)
    if "llmDone" not in df.columns:
        df["llmDone"] = False
    has_content = df['content'].apply(lambda c: hasattr(c, '__len__') and len(c) > 0)
    failed = df[(df["llmDone"] != True) & has_content]
    if len(failed) == 0:
        logger.info("该业务没有未完成的有正文文章。")
        return 0
    articles = failed.to_dict(orient="records")
    logger.info("发现 %d 篇未分析文章", len(articles))
    # return len(submit_analysis_batches(articles, business))  # 已改用 Antigravity Agent 总结
    return len(articles)


def fetch_wall_street_news_list() -> None:
    data_path = BUSINESS["wsj"]["data"]
    existing_urls = read_existing_urls(data_path)
    targets = Wall_Street.wsj_tools.fetch_all_news_url(existing_urls)
    new_articles = Wall_Street.wsj_tools.scrape_all(targets)
    if not new_articles:
        logger.info("没有新的 WSJ 文章需要处理。")
        return
    merge_and_save(new_articles, data_path, False)
    # submit_analysis_batches(new_articles, 'wsj')  # 已改用 Antigravity Agent 总结


def fetch_wall_street_print_edition_news_list(date: str) -> None:
    data_path = BUSINESS["wsj_print"]["data"]
    news_url_list = Wall_Street.wsj_tools.fetch_daily_news_url(date)
    if not news_url_list:
        logger.info(f"WSJ Print Edition ({date}) 未获取到文章链接。")
        return

    # 过滤已爬取的链接，避免重复爬取正文
    existing_urls = read_existing_urls(data_path)
    new_targets = [item for item in news_url_list if item["url"] not in existing_urls]

    if not new_targets:
        logger.info(f"WSJ Print Edition ({date}) 所有文章已爬取，无需重复处理。")
        return

    logger.info(f"WSJ Print Edition ({date}) 待爬取新文章: {len(new_targets)} 篇")
    new_articles = Wall_Street.wsj_tools.scrape_all(new_targets)
    merge_and_save(new_articles, data_path, False)
    # submit_analysis_batches(new_articles, 'wsj_print')  # 已改用 Antigravity Agent 总结


def fetch_bloomberg() -> None:
    data_path = BUSINESS["bloomberg"]["data"]
    text_file_path = "./bloomberg/url.txt"
    all_urls = []
    if os.path.exists(text_file_path):
        with open(text_file_path, 'r', encoding='utf-8-sig') as f:
            all_urls.extend(json.load(f))
    else:
        all_urls = bloomberg.bloomberg_tools.fetch_all_news_url()
        with open(text_file_path, "w", encoding="utf-8") as f:
            json.dump(all_urls, f, ensure_ascii=False, indent=2)
    new_urls = common.filter_unique_urls(all_urls, data_path)

    if len(new_urls) > 0:
        # 1. 统一爬取所有新文章，全部入库并保持 llmDone=False
        for url in new_urls:
            new_articles = bloomberg.bloomberg_tools.scrape_all([url])
            merge_and_save(new_articles, data_path, set_llm_done=False)

    logger.info(f'需要处理的 Bloomberg 文章数量：{len(new_urls)}')

    # 爬取结束，清理临时 url.txt 文件
    if os.path.exists(text_file_path):
        try:
            os.remove(text_file_path)
            logger.info(f"爬取结束，已删除临时文件 {text_file_path}")
        except Exception as e:
            logger.warning(f"删除临时文件 {text_file_path} 失败: {e}")

    # 2. 读取 parquet，捞出所有 llmDone=False 且有正文的文章（含历史失败/未完成数据），重新提交批次（异步收尾）
    # df = pd.read_parquet(data_path)
    # has_content = df['content'].apply(lambda c: hasattr(c, '__len__') and len(c) > 0)
    # filtered = df[(df['llmDone'] != True) & has_content].to_dict(orient='records')
    # submit_analysis_batches(filtered, 'bloomberg')  # 已改用 Antigravity Agent 总结


def fetch_economist() -> None:
    fetched_new_urls = economist.economist_tools.fetch_all_news_url()
    economist_common_action('economist', fetched_new_urls)


def fetch_economist_weekly() -> None:
    fetched_new_urls = economist.economist_tools.fetch_all_weekly_news_url(1)
    economist_common_action('economist_weekly', fetched_new_urls)


def economist_common_action(business: str, fetched_targets: list) -> None:
    data_path = BUSINESS[business]["data"]
    new_targets = common.filter_unique_urls(fetched_targets, data_path)
    if not new_targets:
        logger.info("没有新的 economist 文章需要处理。")
        return

    # 1. 先爬完所有文章（llmDone=False 入库），统一保存
    all_new_articles: list[dict] = []
    for target in new_targets:
        new_articles = economist.scrape_all([target])
        if len(new_articles) == 0 or len(new_articles[0].get('content')) == 0:
            logger.info('economist get blocked')
            new_articles = economist.economist_tools.scrape_all([target])
        all_new_articles.extend(new_articles)
        merge_and_save(new_articles, data_path, set_llm_done=False)

    logger.info(f"economist 全部爬取完成，共 {len(all_new_articles)} 篇，待 Antigravity 总结...")
    # submit_analysis_batches(all_new_articles, business)  # 已改用 Antigravity Agent 总结


def fetch_barrons_weekly():
    fetched_targets = barrons.barrons_tools.fetch_all_weekly_news_url(4)
    barrons_common_action('barrons_weekly', fetched_targets)


def fetch_ft() -> None:
    """抓取 FT.com 各板块文章并提交批次（异步收尾）"""
    data_path = BUSINESS["ft"]["data"]
    existing_urls = read_existing_urls(data_path)

    targets = ft.ft_tools.fetch_all_news_url(page_count=1)
    new_urls = [u for u in targets if u not in existing_urls]
    if not new_urls:
        logger.info("没有新的 FT 文章需要处理。")
        return

    logger.info(f"需要处理的 FT 文章数量: {len(new_urls)}")
    new_articles = ft.ft_tools.scrape_all(new_urls)
    merge_and_save(new_articles, data_path, False)
    # submit_analysis_batches(new_articles, 'ft')  # 已改用 Antigravity Agent 总结


def barrons_common_action(business: str, fetched_targets: list) -> None:
    data_path = BUSINESS[business]["data"]
    new_targets = common.filter_unique_urls(fetched_targets, data_path)
    if not new_targets:
        logger.info("没有新的 barrons 文章需要处理。")
        return

    # 1. 先爬完所有文章（llmDone=False 入库），统一保存
    all_new_articles: list[dict] = []
    for target in new_targets:
        new_articles = barrons.scrape_all([target])
        if len(new_articles) == 0 or len(new_articles[0].get('content')) == 0:
            logger.info('barrons get blocked')
            new_articles = barrons.barrons_tools.scrape_all([target])
        all_new_articles.extend(new_articles)
        merge_and_save(new_articles, data_path, set_llm_done=False)

    logger.info(f"barrons 全部爬取完成，共 {len(all_new_articles)} 篇，待 Antigravity 总结...")
    # submit_analysis_batches(all_new_articles, business)  # 已改用 Antigravity Agent 总结



# ============================================================
# FastAPI 常驻后台服务
# ============================================================

# 各来源抓取命中的 CDP 端口。同一 Chrome（同一端口）可开多个窗口并发爬取，
# 故不同来源可并行；仅同一来源需串行（避免重复触发时冲突）。
_SOURCE_CDP_PORT = {
    "bloomberg": "9223",
    "ft": "9224",
    "wsj": "9222",
    "wsj-print": "9222",
    "economist": "9222",
    "economist-weekly": "9222",
    "barrons": "9224",
}
SOURCE_TO_BUSINESS = {
    "bloomberg": "bloomberg",
    "wsj": "wsj",
    "wsj-print": "wsj_print",
    "wsj_print": "wsj_print",
    "economist": "economist",
    "economist-weekly": "economist_weekly",
    "economist_weekly": "economist_weekly",
    "barrons": "barrons_weekly",
    "barrons_weekly": "barrons_weekly",
    "ft": "ft",
}
_fetch_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


_job_stop = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _job_stop.clear()
    job_thread = threading.Thread(target=batch_job_loop, args=(_job_stop,), daemon=True, name="batch-job")
    job_thread.start()
    logger.info("News Fetch API 已启动（爬取完成后发布 ApplicationEvent 异步触发 API 总结），监听 http://0.0.0.0:8000 （文档: /docs）")
    try:
        yield
    finally:
        _job_stop.set()
        job_thread.join(timeout=10)
        logger.info("News Fetch API 已关闭。")


app = FastAPI(title="News Fetch API", version="1.0.0", lifespan=lifespan)


class RetryRequest(BaseModel):
    business: str = "wsj"


def _run_with_lock(source: str, func, *args, **kwargs):
    """在来源对应的锁下执行抓取函数，返回结果 JSON。锁按来源隔离，不同来源可并行。
    抓取成功后，自动发布针对该业务源的 BusinessFetchFinishedEvent，由后台监听器异步消费总结。
    """
    lock = _fetch_locks[source]
    with lock:
        with fetch_scope(source):
            logger.info("开始抓取来源: %s (已标记 FETCHING)", source)
            try:
                result = func(*args, **kwargs)
                success = True
                err_msg = ""
            except Exception as e:
                logger.error("抓取来源 %s 失败: %s", source, e)
                traceback.print_exc()
                success = False
                err_msg = str(e)
                raise HTTPException(status_code=500, detail=f"{source} 抓取失败: {e}")
            logger.info("抓取来源 %s 完成 (已清除 FETCHING 锁状态)。", source)

        # 锁已释放且 FETCHING 状态已清空，立即发布该业务抓取完成事件
        biz = SOURCE_TO_BUSINESS.get(source)
        if biz and success:
            logger.info("来源 %s 抓取结束，发布业务 %s 完成通知 (ApplicationEvent)...", source, biz)
            event_publisher.publish_event(
                BusinessFetchFinishedEvent(business=biz, source=source, success=True)
            )

        return {"source": source, "status": "ok", "result": result}




@app.post("/fetch/all")
def fetch_all():
    """并发抓取全部来源：每个来源独立线程并行。

    同一 Chrome（同一 CDP 端口）可开多个窗口并发，故 wsj/economist（9222）、
    ft/barrons（9224）等可同时爬取；仅同一来源重复触发时由 per-source 锁串行。
    """
    jobs = [
        ("bloomberg", fetch_bloomberg),
        ("wsj", fetch_wall_street_news_list),
        ("wsj-print", lambda: fetch_wall_street_print_edition_news_list(time.strftime("%Y%m%d"))),
        ("economist", fetch_economist),
        ("economist-weekly", fetch_economist_weekly),
        ("barrons", fetch_barrons_weekly),
        ("ft", fetch_ft),
    ]

    def run_one(item):
        source, func = item
        try:
            return _run_with_lock(source, func)
        except HTTPException as e:
            return {"source": source, "status": "error", "detail": e.detail}
        except Exception as e:
            return {"source": source, "status": "error", "detail": str(e)}

    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        results = list(ex.map(run_one, jobs))

    return {"status": "ok", "results": results}


@app.post("/fetch/{source}")
def fetch_source(source: str, date: str = Query(default=None),):
    """按需抓取单个来源。"""
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    handlers = {
        "bloomberg": fetch_bloomberg,
        "wsj": fetch_wall_street_news_list,
        "wsj-print": lambda: fetch_wall_street_print_edition_news_list(date),
        "economist": fetch_economist,
        "economist-weekly": fetch_economist_weekly,
        "barrons": fetch_barrons_weekly,
        "ft": fetch_ft,
    }
    if source not in handlers:
        raise HTTPException(status_code=404, detail=f"unknown source: {source}")
    return _run_with_lock(source, handlers[source])


@app.post("/fetch/retry")
def fetch_retry(req: RetryRequest):
    """重试该业务 llmDone=False 且有正文的文章（异步提交批次）。"""
    return _run_with_lock("retry", retry_failed_articles, req.business)


if __name__ == "__main__":

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")