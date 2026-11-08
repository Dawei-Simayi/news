# -*- coding: utf-8 -*-
import json
import os
import re
import time
from datetime import datetime
from typing import Any

import pandas as pd
from playwright.sync_api import sync_playwright

import common.common_tools
from logger import get_logger

logger = get_logger(__name__)


def get_next_data(html_content: str, json_target_key: str, tag_id: str = "__NEXT_DATA__") -> Any:
    """
    从 HTML 响应中提取指定 id 的 script 标签内的 JSON 数据，
    并从中安全地提取出 'moreInArticles' 字段的值。

    :param json_target_key: 文章链接的字段ke
    :param html_content: 爬虫获取到的 HTML 字符串 (response.text)
    :param tag_id: 目标 script 标签的 id，默认是 '__NEXT_DATA__'
    :return: moreInArticles 对应解析后的 Python 数据（通常是 list 或 dict），未找到则返回 None
    """
    if not html_content:
        return None

    # 1. 动态构建正则，精准匹配指定 id 的 <script> 标签内容
    pattern = rf'<script[^>]*id="{tag_id}"[^>]*>(.*?)</script>'
    match = re.search(pattern, html_content, re.DOTALL)

    if not match:
        logger.warning(f"未找到 id 为 {tag_id} 的 script 标签。")
        return None

    json_str = match.group(1).strip()

    try:
        # 2. 将标签内的整段字符串解析为标准的 Python 字典
        full_data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}")
        return None

    # 3. 编写一个递归深度查找函数，在复杂的嵌套字典/列表中搜索 "moreInArticles"
    def find_key_recursive(data_obj: Any, target_key: str) -> Any:
        if isinstance(data_obj, dict):
            if target_key in data_obj:
                return data_obj[target_key]
            for value in data_obj.values():
                result = find_key_recursive(value, target_key)
                if result is not None:
                    return result
        elif isinstance(data_obj, list):
            for item in data_obj:
                result = find_key_recursive(item, target_key)
                if result is not None:
                    return result
        return None

    # 4. 调用递归查找
    more_in_articles_value = find_key_recursive(full_data, json_target_key)

    if more_in_articles_value is not None:
        logger.info(f'成功提取到 {json_target_key} 字段数据！')
        return more_in_articles_value
    else:
        logger.warning("在 JSON 数据结构中未找到 moreInArticles 字段。")
        return None


def get_wsj_news_list(list_url: str, json_target_key: str) -> list:
    articles = common.common_tools.get_article_list(list_url, json_target_key=json_target_key, chrom_proxy_url="http://127.0.0.1:9222")
    extracted_data = [
        {
            "url": item.get("articleUrl"),
            "timestamp": item.get("timestamp")
        }
        for item in (articles or []) if isinstance(item, dict)  # 确保元素是字典，防止报错
    ]

    return extracted_data


WSJ_SECTIONS = [
    ("economy", "https://www.wsj.com/economy?page=1"),
    ("business", "https://www.wsj.com/business?page=1"),
    ("tech", "https://www.wsj.com/tech?page=1"),
    ("china", "https://www.wsj.com/world/china?page=1"),
]


def fetch_all_news_url(existing_urls: set[str]) -> list[dict]:
    """
    获取 WSJ 各板块文章列表，跨板块去重后过滤已爬链接，返回待爬取的新文章列表。

    :param existing_urls: 已爬取的 URL 集合（用于过滤）
    :return: 本次待爬取的新文章列表 [{"url": ..., "timestamp": ...}]
    """
    # 1. 抓取各板块文章列表
    all_targets: list[dict] = []
    for section_name, section_url in WSJ_SECTIONS:
        logger.info(f"--- 正在抓取板块: {section_name} ---")
        result = get_wsj_news_list(section_url, json_target_key="moreInArticles")
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    all_targets.append({
                        "url": item.get("url"),
                        "timestamp": item.get("timestamp"),
                    })

    logger.info(f"本次各板块共扫描到 {len(all_targets)} 条在线链接。")

    # 2. 板块间去重（同一文章可能出现在多个板块）
    seen = set()
    unique_targets = []
    for t in all_targets:
        if t["url"] not in seen:
            seen.add(t["url"])
            unique_targets.append(t)
    logger.info(f"跨板块去重后剩余 {len(unique_targets)} 条独立链接。")

    # 3. 过滤已爬过的链接，只保留新链接
    new_targets = [t for t in unique_targets if t["url"] not in existing_urls]
    logger.info(f"排除已有链接后，待爬取新链接: {len(new_targets)} 条。")

    return new_targets


def fetch_daily_news_url(date: str) -> list[dict]:
    # 格式化日期为标准的 YYYY-MM-DDT00:00:00Z（每日报纸统一使用 00:00:00 时间戳）
    try:
        if len(date) == 8 and date.isdigit():
            formatted_ts = datetime.strptime(date, "%Y%m%d").strftime("%Y-%m-%dT00:00:00Z")
        else:
            formatted_ts = datetime.fromisoformat(date).strftime("%Y-%m-%dT00:00:00Z")
    except Exception:
        formatted_ts = f"{date}T00:00:00Z"

    urls = [
        f'https://www.wsj.com/print-edition/{date}/frontpage',
        f'https://www.wsj.com/print-edition/{date}/us',
        f'https://www.wsj.com/print-edition/{date}/world',
        f'https://www.wsj.com/print-edition/{date}/opinion',
        f'https://www.wsj.com/print-edition/{date}/business-and-finance',
    ]

    all_article_urls: list[dict] = []
    seen_urls: set[str] = set()

    for url in urls:
        result = get_wsj_news_list(url, json_target_key="articles")
        if not result:
            continue  # 避免因单版面为空中断后续版面
        for item in result:
            article_url = item.get("url")
            if article_url and article_url not in seen_urls:
                seen_urls.add(article_url)
                all_article_urls.append({
                    "url": article_url,
                    "timestamp": formatted_ts
                })
    return all_article_urls


def scrape_all(targets: list[dict]) -> list[dict]:
    """
    爬取 WSJ 文章正文。不负责保存，仅返回新爬取的文章列表。

    :param targets: 待爬取的文章列表 [{"url": ..., "timestamp": ...}]
    :return: 本次新爬取的文章列表
    """
    logger.info(f'--- 开始增量爬取{len(targets)}篇正文段落 ---')
    new_articles = []

    for target in targets:
        url = target["url"]
        timestamp = target.get("timestamp")

        result = common.common_tools.get_news_paragraphs(url, '[data-type="paragraph"]', "http://127.0.0.1:9222", "time[datetime]", "datetime")
        if len(result["content"]) == 0:
            continue
        new_articles.append({
            "url": url,
            "timestamp": timestamp,
            "content": result['content'],
            "llmDone": False,
        })
        time.sleep(1)

    logger.info(f"增量爬取结束！本次实际新爬取了 {len(new_articles)} 篇页面。")
    return new_articles
