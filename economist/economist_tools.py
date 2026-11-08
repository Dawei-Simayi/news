# -*- coding: utf-8 -*-
import time
from datetime import datetime, timedelta

import common
from common.common_tools import get_article_list

from logger import get_logger

logger = get_logger(__name__)

ECONOMIST_SECTIONS = [
    ("business", "https://www.economist.com/topics/business"),
    ("financeAndEconomic", "https://www.economist.com/topics/finance-and-economics")
]


def fetch_article(url: str) -> dict:
    """
    从 Economist 文章页面的 __NEXT_DATA__ 中提取 body，
    遍历获取所有 type=PARAGRAPH 的 dist.text，拼接后返回。

    :param url: 目标文章 URL
    :return: 拼接后的正文字符串
    """
    content = get_article_list(url, json_target_key="content", chrom_proxy_url="http://127.0.0.1:9222")
    if not content:
        logger.warning(f"未能获取到 body 数据: {url}，重复提取")
        return fetch_article(url)

    timestamp = content.get('datePublished')
    body_list = content.get('body')
    article = []
    for item in body_list:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "PARAGRAPH":
            text = item.get("text", {})
            if text:
                article.append(text)


    result = {url: url, 'timestamp': timestamp, 'content': article, 'llmDone': False}
    logger.info(f"成功提取 {len(article)} 个段落")
    return result


def fetch_weekly_article_url_list(date: str) -> list[dict]:
    magazine_date = date if "-" in date else f"{date[:4]}-{date[4:6]}-{date[6:]}"
    weekly_url = f'https://www.economist.com/weeklyedition/{date}'
    components = get_article_list(weekly_url, json_target_key="components", chrom_proxy_url="http://127.0.0.1:9222")
    if components is None or len(components) == 0:
        logger.error(f'没有获取到内容，{weekly_url}')
        return []
    all_targets = []
    seen_urls = set()
    for component in components:
        articles = component.get('articles')
        if articles is None or len(articles) == 0:
            continue
        for article in articles:
            article_url = article.get("url", "")
            if article_url:
                full_url = f"https://www.economist.com{article_url}"
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    all_targets.append({
                        "url": full_url,
                        "magazine_date": magazine_date,
                    })

    return all_targets

def fetch_all_news_url() -> list[str]:
    """
    遍历 ECONOMIST_SECTIONS，从每个板块页面的 __NEXT_DATA__ 中提取 content.articles，
    获取文章 URL 并拼上 https://www.economist.com/ 前缀。

    :return: 完整文章 URL 列表
    """
    all_urls: list[str] = []
    for section_name, section_url in ECONOMIST_SECTIONS:
        logger.info(f"正在爬取板块 [{section_name}]: {section_url}")
        content = get_article_list(section_url, json_target_key="content", chrom_proxy_url="http://127.0.0.1:9222")
        if not content or not isinstance(content, dict):
            logger.warning(f"板块 [{section_name}] 未获取到 content 数据")
            content = get_article_list(section_url, json_target_key="content", chrom_proxy_url="http://127.0.0.1:9222")

        if content is None:
            continue
        articles = content.get("articles", [])
        if not isinstance(articles, list) or len(articles) == 0:
            logger.warning(f"板块 [{section_name}] articles 字段格式异常")
            continue

        for article in articles:
            article_url = article.get("url", "")
            if article_url:
                full_url = f"https://www.economist.com{article_url}"
                all_urls.append(full_url)

        logger.info(f"板块 [{section_name}] 提取到 {len(articles)} 篇文章")

    logger.info(f"所有板块爬取完成，共提取 {len(all_urls)} 篇文章链接。")
    return all_urls


def scrape_all(targets: list) -> list[dict]:
    """
    爬取 economist 文章列表 → 增量爬取正文。不负责保存，仅返回新爬取的文章列表。

    :param targets: 待爬取的文章列表 [{"url": ..., "magazine_date": ...}] 或 URL 列表
    :return: 本次新爬取的文章列表
    """
    new_articles = []

    for target in targets:
        if isinstance(target, dict):
            url = target.get("url")
            mag_date = target.get("magazine_date")
        else:
            url = str(target)
            mag_date = None

        if not url:
            continue

        logger.info(f"--- 正在爬取: {url} ---")
        result = common.get_news_paragraphs(url, '[data-component="paragraph"]', "http://127.0.0.1:9222", "time[datetime]", "datetime")
        if len(result["content"]) == 0:
            logger.info(f"主 selector 未提取到内容，尝试 body-text 标签兜底: {url}")
            result = common.get_news_paragraphs(url, "body-text", "http://127.0.0.1:9222", "time", None)
        if len(result["content"]) == 0:
            logger.warning(f"body-text 兜底亦无内容，跳过: {url}")
            continue
        new_articles.append({
            "url": url,
            "content": result["content"],
            "timestamp": result.get("timestamp"),
            "magazine_date": mag_date,
            "llmDone": False,
        })
        time.sleep(1)

    logger.info(f"economist 爬取结束！本次共爬取 {len(new_articles)} 篇文章。")
    return new_articles


def fetch_all_weekly_news_url(weekly_count: int) -> list[dict]:
    today = datetime.now()
    days_since_saturday = (today.weekday() - 5) % 7
    saturday = today - timedelta(days=days_since_saturday)
    sat_list = []
    for i in range(weekly_count):
        saturday_delta = saturday - timedelta(days=i * 7)
        date_str = saturday_delta.strftime("%Y-%m-%d")
        sat_list.append(date_str)
    targets = []
    seen = set()
    for date in sat_list:
        for item in fetch_weekly_article_url_list(date):
            if item["url"] not in seen:
                seen.add(item["url"])
                targets.append(item)
    return targets
