# -*- coding: utf-8 -*-
import re
import time
from enum import Enum

import requests

from playwright.sync_api import sync_playwright

import common
from common.common_tools import send_feishu_message
from logger import get_logger

logger = get_logger(__name__)

BLOOMBERG_DOMAIN = "https://www.bloomberg.com"

# Bloomberg 正文段落选择器：
# 1. 传统模板：[data-component="paragraph"]
# 2. 新版 Design System 模板：<p class="_paragraph_8db259a6db ds--col-main ds--paragraph">
BLOOMBERG_PARAGRAPH_SELECTOR = (
    '[data-component="paragraph"], '
    'p._paragraph_8db259a6db, '
    'p.ds--paragraph, '
    'p[class*="_paragraph_"]'
)


class BloombergPage(Enum):
    """Bloomberg 新闻板块枚举"""
    PHX_FINANCE = "phx-finance"


def get_bloomberg_news_list(list_url: str, max_pages: int = 10) -> list[str]:
    """
    通过 Playwright 加载页面，翻页爬取 Bloomberg 新闻列表。
    每页需要先滚动到底部，点击 aria-label="more stories" 按钮加载更多。

    :param list_url:  目标列表页 URL
    :param max_pages: 最大翻页次数，防止无限滚动
    """
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9223")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            logger.info(f"正在加载页面: {list_url}")
            page.goto(list_url, wait_until="load", timeout=60000)

            # 等待 archive_story_list 出现
            page.wait_for_selector('#archive_story_list', timeout=300000)

            # 先翻页加载所有内容
            for _ in range(max_pages):
                more_btn = page.query_selector('button[aria-label="more stories"]')
                if not more_btn:
                    logger.info("没有更多翻页按钮，翻页结束。")
                    break

                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(500)
                more_btn.click()
                page.wait_for_timeout(2000)

            # 一次性捞取所有 href
            hrefs = page.eval_on_selector_all(
                '#archive_story_list a[href]',
                'els => [...new Set(els.map(el => el.getAttribute("href")).filter(Boolean))]'
            )
            logger.info(f'翻页完成，数量：{len(hrefs)}，开始统一提取所有链接...')

            page.close()

            urls = [
                BLOOMBERG_DOMAIN + href if href.startswith("/") else href
                for href in hrefs
            ]
            logger.info(f"Bloomberg 列表页翻页爬取完成，共提取 {len(urls)} 条链接。")

        except Exception as e:
            logger.error(f"Bloomberg 列表页翻页爬取发生致命错误: {e}")
            return []

    return urls

def fetch_all_news_url() -> list[str]:
    page_list = [
        {"finance": "https://www.bloomberg.com/finance"},
        {"economics": "https://www.bloomberg.com/economics"},
        {"industries": "https://www.bloomberg.com/industries"},
        {"technology": "https://www.bloomberg.com/technology"},
        {"politics": "https://www.bloomberg.com/politics"},
        {"opinion": "https://www.bloomberg.com/opinion"},
        {"businessweek": "https://www.bloomberg.com/businessweek"},
    ]

    all_news: list[str] = []
    for page in page_list:
        url = next(iter(page.values()))
        news = get_bloomberg_news_list(url)
        if len(news) == 0:
            send_feishu_message()
            logger.error(f'获取文章列表失败：{url}')
            news = get_bloomberg_news_list(url)
        all_news.extend(news)

    return all_news


def scrape_all(urls: list[str]) -> list[dict]:
    """
    爬取 Bloomberg 文章列表 → 增量爬取正文。不负责保存，仅返回新爬取的文章列表。

    :param urls: 待爬取的文章 URL 列表
    :return: 本次新爬取的文章列表
    """
    new_articles = []

    for url in urls:
        logger.info(f"--- 正在爬取: {url} ---")
        result = common.get_news_paragraphs(
            url,
            BLOOMBERG_PARAGRAPH_SELECTOR,
            "http://127.0.0.1:9223",
            "time[datetime]",
            "datetime",
        )
        if len(result["content"]) == 0:
            continue
        new_articles.append({
            "url": url,
            "content": result["content"],
            "timestamp": result.get("timestamp"),
            "llmDone": False,
        })
        time.sleep(1)

    logger.info(f"Bloomberg 爬取结束！本次共爬取 {len(new_articles)} 篇文章。")
    return new_articles


if __name__ == '__main__':
    news_url_list = fetch_all_news_url()
    news_content_list = scrape_all(news_url_list)
