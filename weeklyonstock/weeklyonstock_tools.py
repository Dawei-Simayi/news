# -*- coding: utf-8 -*-
"""
证券市场周刊 (weeklyonstock.com) 爬虫工具模块。

按照 README 需求提供三个核心函数:
1. fetch_all_magazine_links()  — 获取所有周刊链接
2. fetch_articles_by_section() — 获取指定栏目下所有文章内容
3. parse_article_html()       — 解析文章 HTML（标题/时间/正文）
以及周边辅助函数:
   get_latest_magazine_link() — 获取最新一期周刊链接
   scrape_all()               — 批量爬取入口
   crawl_weeklyonstock()      — 一站式爬取（集体调用三个核心函数）
   fetch_and_summarize()      — 完整流程（爬取 → LLM总结 → 数据持久化）
"""

import json
import os
import re
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, parse_qs

import pandas as pd
from playwright.sync_api import sync_playwright
from logger import get_logger
from common.paths import data_file

from llm.summarizer import call_llm, MARKET_PROMPT_PATH
from llm.provider import LLMProvider

logger = get_logger(__name__)

BASE_URL = "https://www.weeklyonstock.com"
CHROME_PROXY_URL = "http://127.0.0.1:9222"
EID = "17"  # 周刊分类 ID（证券市场周刊）


# ──────────────────────────────────────────────
# 函数 1: 获取所有周刊链接
# ──────────────────────────────────────────────

def fetch_all_magazine_links(start_year: int = 2014,
                             end_year: Optional[int] = None,
                             existing_df: Optional[pd.DataFrame] = None) -> list[dict]:
    """
    获取所有周刊链接。

    URL 格式: eMag_list.php?eid=17&page_year=2014&page_id=3
    page_id 取值 1 ~ 5，每年最多 5 页。
    周刊链接在 `<a class="fl item_a" href=".../weekly_news.php?bid=...">` 中。

    优化：对 2014 ~ 2025 年（含）的链接直接从现有 parquet 数据中提取，
    避免重复爬取。2026 年及以后的链接仍通过 Playwright 爬取。

    :param start_year:  起始年份（默认 2014）
    :param end_year:    结束年份（默认当前年份）
    :param existing_df: 现有 parquet DataFrame（由上层传递，避免重复读取）
    :return: [{"url": str, "year": int, "page_id": int, "bid": str}, ...]
    """
    if end_year is None:
        end_year = datetime.now().year

    all_links: list[dict] = []

    # ── 历史年份: 从 parquet 提取（2014 ~ 2025） ──
    parquet_end = min(2025, end_year)
    if start_year <= parquet_end:
        # 如果上层未传 existing_df，内部自动读取
        if existing_df is None:
            existing_df = _load_existing_parquet()

        if not existing_df.empty and "url" in existing_df.columns and "timestamp" in existing_df.columns:
            # 从 parquet 中提取每个 magazine URL 及对应的年份
            parquet_links: dict[str, dict] = {}  # url -> link dict
            for _, row in existing_df.iterrows():
                url = row.get("url", "")
                ts = row.get("timestamp", "")
                if not url or not ts:
                    continue
                try:
                    year = int(str(ts).strip()[:4])
                except (ValueError, IndexError):
                    continue
                if year < start_year or year > parquet_end:
                    continue
                if url not in parquet_links:
                    bid = ""
                    parsed = urlparse(url)
                    qs = parse_qs(parsed.query)
                    if "bid" in qs:
                        bid = qs["bid"][0]
                    parquet_links[url] = {
                        "url": url,
                        "year": year,
                        "page_id": 0,
                        "bid": bid,
                    }

            all_links.extend(parquet_links.values())
            logger.info("从 parquet 读取 %d 个周刊链接（%d ~ %d）",
                        len(parquet_links), start_year, parquet_end)
        else:
            logger.info("parquet 无历史数据，跳过 parquet 读取")

    # ── 最新年份: 通过 Playwright 爬取（2026 ~ end_year） ──
    scrape_start = max(start_year, 2026)
    if scrape_start <= end_year:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CHROME_PROXY_URL)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            for year in range(scrape_start, end_year + 1):
                page_has_content = False
                for page_id in range(1, 6):
                    url = (f"{BASE_URL}/eMag_list.php"
                           f"?eid={EID}&page_year={year}&page_id={page_id}")
                    logger.info("正在获取 %d 年第 %d 页周刊列表: %s", year, page_id, url)

                    try:
                        page.goto(url, wait_until="networkidle", timeout=60000)
                        time.sleep(2)

                        hrefs = page.evaluate("""
                            () => {
                                const anchors = document.querySelectorAll('a.fl.item_a');
                                return Array.from(anchors)
                                    .map(a => a.href)
                                    .filter(h => h && h.includes('weekly_news.php'));
                            }
                        """)

                        if not hrefs:
                            logger.info("第 %d 年第 %d 页无更多周刊，跳过剩余页", year, page_id)
                            break

                        page_has_content = True
                        for href in hrefs:
                            bid = ""
                            parsed = urlparse(href)
                            qs = parse_qs(parsed.query)
                            if "bid" in qs:
                                bid = qs["bid"][0]
                            all_links.append({
                                "url": href,
                                "year": year,
                                "page_id": page_id,
                                "bid": bid,
                            })

                        logger.info("第 %d 年第 %d 页 → %d 个周刊", year, page_id, len(hrefs))
                        time.sleep(1)

                    except Exception as e:
                        logger.error("获取第 %d 年第 %d 页失败: %s", year, page_id, e)

                if not page_has_content:
                    logger.info("%d 年没有找到任何周刊，停止", year)

            page.close()

    # 按 URL 去重（确保 parquet 来源与爬取来源不重复）
    seen: set[str] = set()
    unique: list[dict] = []
    for link in all_links:
        u = link.get("url", "")
        if u and u not in seen:
            seen.add(u)
            unique.append(link)

    logger.info("共获取 %d 个独立周刊链接", len(unique))
    return unique


# ──────────────────────────────────────────────
# 函数 2: 从杂志页面提取指定栏目的文章 ID
# ──────────────────────────────────────────────

def _get_article_ids_by_section(page, section_name: str) -> list[dict]:
    """
    从已加载的杂志页面 DOM 中提取指定栏目的文章列表。

    栏目 DOM 结构:
        <div class="item">
          <div class="title_item"><h5>民间智慧</h5></div>
          <div class="cont_item">
            <ul>
              <li><a id="560166" class="item_a" onclick="Get_News(this,560166)">标题</a></li>
            </ul>
          </div>
        </div>

    :param page:         Playwright Page 对象（已导航至杂志页面）
    :param section_name: 栏目名称，如 "民间智慧"
    :return: [{"id": str, "title": str}, ...]
    """
    return page.evaluate(
        """(sectionName) => {
            const items = document.querySelectorAll('.item');
            for (const item of items) {
                const titleEl = item.querySelector('.title_item h5');
                if (titleEl && titleEl.textContent.trim() === sectionName) {
                    const links = item.querySelectorAll('.cont_item ul li a');
                    return Array.from(links).map(a => ({
                        id: a.id,
                        title: a.textContent.trim().replace(/^·\\s*/, '')
                    }));
                }
            }
            return [];
        }""",
        section_name,
    )


# ──────────────────────────────────────────────
# 函数 3: 解析文章 HTML → 结构化字典
# ──────────────────────────────────────────────

def _parse_article_html(html_content: str) -> dict:
    """
    解析 get_news_info.php 返回的文章 HTML，提取标题 / 时间 / 来源 / 作者 / 正文。

    HTML 结构:
        <h4 id="title" class="article_title">市场进入混沌期</h4>
        <div class="article_time clearfix">
          <span id="onlinedata">2026-07-10</span>
          <span id="onlinetime">00:00</span>
          <span id="comfrom">来源：证券市场周刊</span>
          <span id="author">作者：主持人 尹星</span>
        </div>
        <div id="article_cont">…正文…</div>

    :param html_content: get_news_info.php 返回的 HTML 字符串
    :return: {"title": str, "timestamp": str, "content": str}
    """
    result = {"title": "", "timestamp": "", "content": ""}
    if not html_content:
        return result

    # 标题
    m = re.search(r'<h4[^>]*id="title"[^>]*>(.*?)</h4>', html_content, re.DOTALL)
    if m:
        result["title"] = m.group(1).strip()

    # 日期
    m = re.search(r'<span[^>]*id="onlinedata"[^>]*>(.*?)</span>', html_content)
    if m:
        result["timestamp"] = m.group(1).strip()

    # 时间（与日期合并）
    m = re.search(r'<span[^>]*id="onlinetime"[^>]*>(.*?)</span>', html_content)
    if m and result["timestamp"]:
        t = m.group(1).strip()
        if t:
            result["timestamp"] = f"{result['timestamp']} {t}"

    # 正文内容: <div id="article_cont">…</div>
    m = re.search(r'<div[^>]*id="article_cont"[^>]*>(.*?)</div>', html_content, re.DOTALL)
    if m:
        text = m.group(1)
        # <br> → 换行
        text = re.sub(r'<br\s*/?>', '\n', text)
        # 移除剩余 HTML 标签
        text = re.sub(r'<[^>]+>', '', text)
        # 压缩连续空行
        text = re.sub(r'\n\s*\n', '\n\n', text)
        result["content"] = text.strip()

    return result


# ──────────────────────────────────────────────
# 函数 2 + 3: 获取指定栏目的所有文章
# ──────────────────────────────────────────────

def fetch_articles_by_section(magazine_url: str,
                              section_name: str = "民间智慧",
                              existing_article_ids: Optional[set] = None) -> list[dict]:
    """
    从指定周刊页面获取 **指定栏目** 下所有文章的完整内容。

    工作流程:
      1. 用 Playwright 加载杂志页面
      2. 从 DOM 提取目标栏目的文章 ID 列表
      3. 过滤掉已在 parquet 中存在的 article_id（避免重复爬取）
      4. 对每篇新文章，直接点击对应的 a 标签（触发 Get_News JS 函数加载文章）
      5. 等待文章内容渲染到 DOM 后读取
      6. 解析 HTML 并结构化存储

    :param magazine_url:          周刊 URL，如 https://www.weeklyonstock.com/member/weekly_news.php?bid=1770
    :param section_name:          栏目名称（默认 "民间智慧"）
    :param existing_article_ids:  已爬取的 article_id 集合，用于跳过已有数据
    :return: [{"url": str, "article_id": str, "title": str, "timestamp": str,
               "author": str, "source": str, "content": str}, ...]
    """
    articles: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CHROME_PROXY_URL)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        logger.info("正在加载杂志页面: %s", magazine_url)
        try:
            page.goto(magazine_url, wait_until="networkidle", timeout=60000)
            time.sleep(3)  # 等待 JS 渲染完成
        except Exception as e:
            logger.error("加载杂志页面失败 %s: %s", magazine_url, e)
            page.close()
            return []

        # ── 收集页面所有 h5 标签文案，保存到 h5.txt ──
        try:
            all_h5 = page.evaluate("""
                () => Array.from(document.querySelectorAll('h5')).map(h => h.textContent.trim())
            """)
            h5_path = os.path.join(os.path.dirname(__file__), "h5.txt")
            existing_h5: set[str] = set()
            if os.path.exists(h5_path):
                with open(h5_path, "r", encoding="utf-8") as f:
                    existing_h5 = set(line.strip() for line in f if line.strip())
            new_h5 = [h for h in all_h5 if h not in existing_h5]
            if new_h5:
                with open(h5_path, "a", encoding="utf-8") as f:
                    for h in new_h5:
                        f.write(h + "\n")
                logger.info("h5.txt 新增 %d 个栏目: %s", len(new_h5), new_h5)
        except Exception as e:
            logger.warning("收集 h5 标签失败（不影响主流程）: %s", e)

        # 获取目标栏目的文章列表
        article_links = _get_article_ids_by_section(page, section_name)
        if not article_links:
            logger.warning("在 %s 中未找到栏目「%s」", magazine_url, section_name)
            page.close()
            return []

        # 过滤已爬取的文章
        if existing_article_ids:
            before = len(article_links)
            article_links = [a for a in article_links
                             if a.get("id") not in existing_article_ids]
            skipped = before - len(article_links)
            if skipped:
                logger.info("过滤 %d 篇已爬取文章，剩余 %d 篇待爬取",
                            skipped, len(article_links))
            if not article_links:
                logger.info("该杂志「%s」栏目所有文章均已爬取，跳过", section_name)
                page.close()
                return []

        logger.info("找到 %d 篇「%s」文章", len(article_links), section_name)

        # 顺序点击每篇文章的 a 标签，从 DOM 读取内容
        for idx, link in enumerate(article_links):
            article_id = link.get("id", "")
            article_title = link.get("title", "")
            if not article_id:
                continue

            logger.info("获取文章 [%d/%d]: %s (ID: %s)",
                        idx + 1, len(article_links), article_title, article_id)

            try:
                # 点击 a 标签，触发 Get_News JS 函数加载文章内容到 DOM
                logger.info("点击文章链接: %s", article_title)
                page.click(f'[id="{article_id}"]')
                time.sleep(1)

                # 等待文章内容渲染完成
                page.wait_for_selector("#article_cont", timeout=15000)

                # 读取标题
                title_el = page.query_selector('#title')
                title_text = title_el.inner_text().strip() if title_el else ""

                # 读取时间
                ts_el = page.query_selector('#onlinedata')
                time_el = page.query_selector('#onlinetime')
                timestamp = ""
                if ts_el:
                    timestamp = ts_el.inner_text().strip()
                    if time_el:
                        t = time_el.inner_text().strip()
                        if t:
                            timestamp = f"{timestamp} {t}"

                # 读取正文（类似 get_news_paragraphs 方式，用 inner_text）
                cont_el = page.query_selector('#article_cont')
                content_text = ""
                if cont_el:
                    text = cont_el.inner_text().strip()
                    # 按连续空行拆分为段落，再重新拼接
                    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
                    content_text = "\n\n".join(paragraphs)

                if title_text or content_text:
                    data = {
                        "title": title_text,
                        "timestamp": timestamp,
                        "content": content_text,
                        "article_id": article_id,
                        "url": magazine_url,
                    }
                    articles.append(data)
                    logger.info("✓ %s", title_text or article_title)
                else:
                    logger.warning("文章内容为空: %s", article_title)

            except Exception as e:
                logger.error("获取文章失败 [%s]: %s", article_title, e)

        page.close()

    logger.info("从 %s 的「%s」栏目共获取 %d 篇文章",
                magazine_url, section_name, len(articles))
    return articles


# ──────────────────────────────────────────────
# 最新一期周刊
# ──────────────────────────────────────────────

def get_latest_magazine_link() -> Optional[str]:
    """
    获取最新一期周刊的 URL。

    访问 https://www.weeklyonstock.com/eMag_list.php（无参数），
    从列表中提取第一个周刊链接。

    :return: 最新周刊 URL，未找到则返回 None
    """
    latest: Optional[str] = None

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CHROME_PROXY_URL)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        try:
            page.goto(f"{BASE_URL}/eMag_list.php", wait_until="networkidle", timeout=60000)
            time.sleep(2)

            links = page.evaluate("""
                () => {
                    const anchors = document.querySelectorAll('a.fl.item_a');
                    return Array.from(anchors)
                        .map(a => a.href)
                        .filter(h => h && h.includes('weekly_news.php'));
                }
            """)
            if links:
                latest = links[0]
                logger.info("最新周刊: %s", latest)
            else:
                logger.warning("未在首页找到任何周刊链接")
        except Exception as e:
            logger.error("获取最新周刊链接失败: %s", e)

        page.close()

    return latest


# ──────────────────────────────────────────────
# 批量爬取入口（供 run_fetch.py 调用）
# ──────────────────────────────────────────────

def scrape_all(targets: list[dict],
               section_name: str = "民间智慧",
               existing_df: Optional[pd.DataFrame] = None) -> list[dict]:
    """
    批量爬取多个杂志指定栏目的所有文章。

    这是 run_fetch.py 调用的标准入口，与项目内其他模块的 scrape_all
    签名保持一致。

    :param targets:      杂志列表 [{"url": str, "bid": str, ...}, ...]
    :param section_name: 栏目名称（默认 "民间智慧"）
    :param existing_df:  现有 parquet DataFrame（用于过滤已爬取文章）
    :return: 所有文章列表
    """
    all_articles: list[dict] = []

    # 预构建 article_id 查找表: magazine_url -> set[article_id]
    existing_ids_by_url: dict[str, set] = {}
    if existing_df is not None and not existing_df.empty:
        for _, row in existing_df.iterrows():
            url = row.get("url", "")
            aid = str(row.get("article_id", ""))
            if url and aid:
                existing_ids_by_url.setdefault(url, set()).add(aid)

    for target in targets:
        magazine_url = target.get("url", "")
        if not magazine_url:
            continue
        if not magazine_url.startswith("http"):
            magazine_url = f"{BASE_URL}{magazine_url}"

        logger.info("正在爬取杂志: %s", magazine_url)
        try:
            existing_ids = existing_ids_by_url.get(magazine_url, set())
            articles = fetch_articles_by_section(magazine_url, section_name, existing_ids)
            all_articles.extend(articles)
        except Exception as e:
            logger.error("爬取杂志失败 [%s]: %s", magazine_url, e)

        time.sleep(1)

    logger.info("批量爬取结束，共获取 %d 篇文章", len(all_articles))
    return all_articles


# ──────────────────────────────────────────────
# 集体调用: 一站式爬取（整合三个核心函数）
# ──────────────────────────────────────────────

def crawl_weeklyonstock(section_name: str = "民间智慧",
                         start_year: int = 2014,
                         end_year: Optional[int] = None,
                         max_magazines: int = 0) -> list[dict]:
    """
    一站式爬取：集体调用三个核心函数。

    工作流程:
      1. fetch_all_magazine_links()   — 获取所有周刊链接
      2. fetch_articles_by_section()  — 从每个杂志页面提取指定栏目文章
      3. _parse_article_html()        — 解析文章 HTML（工具函数，独立可用）

    :param section_name:  栏目名称（默认 "民间智慧"）
    :param start_year:    起始年份（默认 2014）
    :param end_year:      结束年份（默认今年）
    :param max_magazines: 最多处理多少期（0 = 全部处理）
    :return: 所有文章列表
    """
    # 读取现有 parquet 数据（只需读取一次，向下传递）
    existing_df = _load_existing_parquet()

    # 第一步：获取所有周刊链接
    logger.info("=" * 50)
    logger.info("第一步：获取所有周刊链接 (%d ~ %s)",
                start_year, end_year or datetime.now().year)
    all_links = fetch_all_magazine_links(start_year, end_year, existing_df)
    logger.info("共获取 %d 个独立周刊", len(all_links))

    if not all_links:
        logger.warning("未获取到任何周刊链接，流程终止")
        return []

    if max_magazines > 0:
        logger.info("限制处理前 %d 期周刊", max_magazines)
        all_links = all_links[:max_magazines]

    # 第二步 + 第三步：批量获取并解析文章
    logger.info("第二步+第三步：逐期提取「%s」栏目文章并解析", section_name)
    articles = scrape_all(all_links, section_name, existing_df)

    logger.info("=" * 50)
    logger.info("一站式爬取完成！共从 %d 期周刊中获取 %d 篇「%s」文章",
                len(all_links), len(articles), section_name)
    return articles


# ──────────────────────────────────────────────
# 文件路径常量
# ──────────────────────────────────────────────

# parquet 数据文件保存路径（统一放在项目根目录 .data/ 下，文件名 = 业务键）
PARQUET_PATH = data_file("weeklyonstock")
# 季度报告 markdown 文件存放目录
REPORT_DIR = os.path.join(os.path.dirname(__file__), "reports")


# ──────────────────────────────────────────────
# 辅助函数: 读取本地 parquet 数据
# ──────────────────────────────────────────────

def _load_existing_parquet(save_path: str = PARQUET_PATH) -> pd.DataFrame:
    """
    读取本地 parquet 数据，不存在则返回空 DataFrame。
    :param save_path: parquet 保存路径
    :return: DataFrame（空表示无历史数据）
    """
    if not os.path.exists(save_path):
        return pd.DataFrame()
    try:
        df = pd.read_parquet(save_path)
        logger.info("读取本地 Parquet 历史数据，共 %d 条。", len(df))
        return df
    except Exception as e:
        logger.warning("读取本地 Parquet 失败: %s", e)
        return pd.DataFrame()


# ──────────────────────────────────────────────
# 辅助函数: 从 timestamp 提取季度键值
# ──────────────────────────────────────────────

def _get_quarter_key(timestamp: str) -> str:
    """
    从时间戳提取季度标识，如 "2014Q1"。

    支持格式: "2026-07-10" 或 "2026-07-10 00:00"
    :param timestamp: 文章时间戳
    :return: 季度标识，无法解析则返回 "unknown"
    """
    if not timestamp:
        return "unknown"
    try:
        dt = datetime.strptime(timestamp.strip()[:10], "%Y-%m-%d")
        quarter = (dt.month - 1) // 3 + 1
        return f"{dt.year}Q{quarter}"
    except (ValueError, IndexError):
        return "unknown"

# ──────────────────────────────────────────────
# 辅助函数: 合并并保存 parquet
# ──────────────────────────────────────────────

def _merge_and_save(new_articles: list[dict],
                    save_path: str = PARQUET_PATH) -> None:
    """
    将新文章合并到本地 parquet 文件，去重并排序。

    保留文章自身的 llmDone 值（不做强制覆盖）:
      - llmDone = False → 待总结（尚未调用 LLM）
      - llmDone = True  → 已总结（LLM 已完成）

    :param new_articles: 新文章列表（需包含 llmDone 字段）
    :param save_path:    parquet 保存路径
    """
    if not new_articles:
        return

    content_list: list[dict] = []

    if os.path.exists(save_path):
        try:
            df_existing = pd.read_parquet(save_path)
            content_list = df_existing.to_dict(orient="records")
            logger.info("成功读取本地 Parquet 历史数据，共 %d 条。", len(df_existing))
        except Exception as e:
            logger.warning("读取本地 Parquet 失败，将重新创建。错误: %s", e)

    content_list.extend(new_articles)

    if content_list:
        df_final = pd.DataFrame(content_list)
        # 注意: keep="last" 确保新数据（可能已更新 llmDone）覆盖旧数据
        df_final.drop_duplicates(subset=["url"], keep="last", inplace=True)
        df_final.sort_values("timestamp", ascending=False, inplace=True)
        # 数据目录可能不存在（首次运行或手动清理过），先确保父目录存在
        os.makedirs(os.path.dirname(os.path.abspath(save_path)) or ".", exist_ok=True)
        df_final.to_parquet(save_path, engine="pyarrow", index=False)
        logger.info("数据已合并、去重并保存至 %s | 当前总存量: %d 条。",
                    save_path, len(df_final))
    else:
        logger.info("没有数据，未对文件进行写入。")


def _update_llm_done_in_parquet(urls_completed: set[str],
                                 save_path: str = PARQUET_PATH) -> None:
    """
    将 parquet 中指定 URL 的 llmDone 标记更新为 True（已总结）。

    :param urls_completed: 已完成 LLM 总结的文章 URL 集合
    :param save_path:      parquet 保存路径
    """
    if not urls_completed or not os.path.exists(save_path):
        return

    try:
        df = pd.read_parquet(save_path)
        matched = df["url"].isin(urls_completed)
        count = matched.sum()
        if count > 0:
            df.loc[matched, "llmDone"] = True
            df.to_parquet(save_path, engine="pyarrow", index=False)
            logger.info("已更新 %d 篇文章的 llmDone=True（总结完成）", count)
    except Exception as e:
        logger.warning("更新 llmDone 失败: %s", e)


# ──────────────────────────────────────────────
# 辅助函数: 按季度保存 markdown 报告
# ──────────────────────────────────────────────

def _save_quarterly_reports(summary_list: list[dict]) -> None:
    """
    将 LLM 总结结果按季度分组，保存为 markdown 文件。

    文件命名: {年}Q{季}.md，存入 REPORT_DIR 目录。
    每篇总结之间用 --- 分隔，新总结追加到对应季度文件末尾。

    :param summary_list: [{"article": dict, "summary": str}, ...]
    """
    if not summary_list:
        logger.info("没有总结内容需要保存。")
        return

    os.makedirs(REPORT_DIR, exist_ok=True)

    # 按季度分组
    quarterly: dict[str, list[str]] = {}
    for item in summary_list:
        article = item["article"]
        summary = item["summary"]
        ts = article.get("timestamp", "")
        title = article.get("title", "无标题")
        qkey = _get_quarter_key(ts)
        if qkey not in quarterly:
            quarterly[qkey] = []
        # 每篇总结加上标题和分隔线
        entry = f"## {title}\n\n{summary}\n\n---\n"
        quarterly[qkey].append(entry)

    # 写入季度文件（追加模式）
    for qkey, entries in sorted(quarterly.items()):
        file_path = os.path.join(REPORT_DIR, f"{qkey}.md")
        with open(file_path, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(entry + "\n")
        logger.info("季度报告已更新: %s (%d 篇)", file_path, len(entries))

    logger.info("季度报告保存完成，共涉及 %d 个季度。", len(quarterly))


# ──────────────────────────────────────────────
# 辅助函数: 使用本地 LLM 总结文章
# ──────────────────────────────────────────────

def _summarize_articles_with_local_llm(
    articles: list[dict],
) -> tuple[list[dict], set[str]]:
    """
    使用本地 LLM 模型逐篇总结文章，并在每篇总结完成后立即：
      1. 更新 parquet 文件中对应文章的 llmDone=True
      2. 将总结内容追加到对应的季度 markdown 报告

    会就地更新每篇文章的 llmDone 字段：
      - llmDone = True  → 总结成功
      - llmDone = False → 总结失败（保持原状）

    :param articles: 文章列表（需包含 url, content, timestamp 字段）
    :return: (summary_list, completed_urls)
        - summary_list: [{"article": dict, "summary": str}, ...]
        - completed_urls: 成功完成总结的文章 URL 集合
    """
    summary_list: list[dict] = []
    completed_urls: set[str] = set()

    # 按时间升序排序（老文章在前），保证 md 文件里时间顺序正确
    articles.sort(key=lambda a: str(a.get("timestamp", "")))
    total = len(articles)

    for idx, article in enumerate(articles, 1):
        content = article.get("content", "")
        if not content:
            continue

        payload = {
            "source": "weeklyonstock",
            "title": article["title"],
            "url": article.get("url", ""),
            "timestamp": article.get("timestamp", ""),
            "content": content,
        }

        title = article.get("title", "无标题")
        logger.info("[%d/%d] 正在分析: %s", idx, total, title)
        try:
            summary = call_llm(json.dumps(payload, ensure_ascii=False),
                               LLMProvider.LOCAL, MARKET_PROMPT_PATH)
            summary_list.append({"article": article, "summary": summary})
            article["llmDone"] = True
            completed_urls.add(article.get("url", ""))

            # ★ 立即更新 parquet 和季度报告
            url = article.get("url", "")
            if url:
                _update_llm_done_in_parquet({url})
            _save_quarterly_reports([{"article": article, "summary": summary}])

            logger.info("✓ %s 分析完成，已更新 parquet 和季度报告", title)
        except Exception as e:
            logger.error("✗ %s 分析失败: %s", title, e)
            # 保持 llmDone = False（待重试）

    return summary_list, completed_urls


# ──────────────────────────────────────────────
# 完整流程: 爬取 → LLM 总结（本地模型）→ 数据持久化
# ──────────────────────────────────────────────

def fetch_and_summarize(section_name: str = "民间智慧",
                         start_year: int = 2014,
                         end_year: Optional[int] = None,
                         max_magazines: int = 0) -> None:
    """
    完整流程：集体调用三个核心函数获取文章 → 先保存 parquet → 再用本地 LLM 总结。

    工作流程:
      1. fetch_all_magazine_links()     — 获取所有周刊链接
      2. fetch_articles_by_section()     — 提取指定栏目文章
      3. _merge_and_save()              — ★ 先保存 parquet（llmDone=False 待总结）
      4. 合并 parquet 中 llmDone=False 的历史文章 — 自动重试之前失败的文章
      5. _summarize_articles_with_local_llm() — 使用本地模型逐篇总结
      6. _update_llm_done_in_parquet()  — 更新已总结文章的 llmDone=True
      7. _save_quarterly_reports()      — 总结按季度保存为 markdown

    字段说明:
      - llmDone = False → 待总结（初始状态）
      - llmDone = True  → 已总结（LLM 完成）

    :param section_name:  栏目名称（默认 "民间智慧"）
    :param start_year:    起始年份（默认 2014）
    :param end_year:      结束年份（默认今年）
    :param max_magazines: 最多处理多少期（0 = 全部处理）
    """
    logger.info("证券市场周刊 · 完整流程开始")
    logger.info("栏目: %s | 年份: %d ~ %s | 最大期数: %s",
                section_name, start_year, end_year or datetime.now().year,
                "全部" if max_magazines == 0 else str(max_magazines))

    # 读取现有 parquet 数据（只需读取一次，向下传递）
    existing_df = _load_existing_parquet()

    # ── 第一步：获取所有周刊链接 ──
    logger.info("▶ 第一步：获取所有周刊链接")
    all_links = fetch_all_magazine_links(start_year, end_year, existing_df)
    if not all_links:
        logger.warning("未获取到任何周刊链接，流程终止。")
        return
    logger.info("共获取 %d 个独立周刊", len(all_links))

    if max_magazines > 0:
        all_links = all_links[:max_magazines]
        logger.info("限制处理前 %d 期周刊", max_magazines)

    # ── 第二步：爬取文章 ──
    logger.info("▶ 第二步：逐期提取「%s」栏目文章并解析", section_name)
    articles = scrape_all(all_links, section_name, existing_df)
    if not articles:
        logger.warning("未获取到任何文章，流程终止。")
        return
    logger.info("共获取 %d 篇文章", len(articles))

    # 初始化 llmDone = False（待总结）
    for article in articles:
        article["llmDone"] = False

    # ── 第三步：先保存 parquet（llmDone=False，待总结状态） ──
    logger.info("▶ 第三步：保存文章数据到 parquet（llmDone=False = 待总结）")
    _merge_and_save(articles)

    # ── 第四步：合并 parquet 中 llmDone=false 的历史文章 ──
    logger.info("▶ 第四步：检查 parquet 中是否有 llmDone=false 的历史遗留文章")
    pending_df = _load_existing_parquet()
    if pending_df is not None and not pending_df.empty and "llmDone" in pending_df.columns:
        pending_df = pending_df[pending_df["llmDone"] != True]
        pending_articles = pending_df.to_dict(orient="records")
        # 按 url 去重，新爬取的文章优先（已在 articles 中的不再重复添加）
        existing_urls = {a.get("url", "") for a in articles}
        merged = list(articles)
        for pa in pending_articles:
            if pa.get("url", "") not in existing_urls:
                merged.append(pa)
        logger.info("  · 新增 %d 篇历史待重试文章，合计 %d 篇待总结",
                    len(merged) - len(articles), len(merged))
        articles = merged
    else:
        logger.info("  · 无非历史遗留文章")

    # ── 第五步：LLM 总结（使用本地模型） ──
    logger.info("▶ 第五步：调用本地 LLM 逐篇总结（含历史遗留）")
    summary_list, completed_urls = _summarize_articles_with_local_llm(articles)

    # ── 更新 parquet 中已总结文章的 llmDone ──
    if completed_urls:
        _update_llm_done_in_parquet(completed_urls)

    if not summary_list:
        logger.warning("没有成功生成任何总结，可后续重试。")
        return

    # ── 第五步：保存季度 markdown 报告 ──
    # （_summarize_articles_with_local_llm 内部已实时追加写入，此处无需重复保存）

    # ── 完成 ──
    logger.info("  · Parquet 数据: %s（llmDone=True 已总结 / False 待处理）",
                PARQUET_PATH)
    logger.info("  · 季度报告目录: %s/", REPORT_DIR)

def retry_pending_summaries() -> None:
    """
    重试 parquet 中 llmDone=False 的文章总结。

    用于在 LLM 总结失败后单独重试，无需重新爬取文章。
    """
    df = _load_existing_parquet()
    if df is None or df.empty:
        logger.warning("无数据，请先运行爬取流程。")
        return

    # 过滤掉已总结的文章
    if "llmDone" in df.columns:
        df = df[df["llmDone"] != True]
    articles = df.to_dict(orient="records")
    if not articles:
        logger.info("所有文章均已总结完成，无需重试。")
        return

    logger.info("共 %d 篇文章待处理（llmDone=False，尚未总结或之前失败）。", len(articles))

    _summarize_articles_with_local_llm(articles)
    # 注意: _summarize_articles_with_local_llm 内部已实时更新 parquet
    logger.info("重试完成。")

#
if __name__ == '__main__':
    fetch_and_summarize(start_year=2013)