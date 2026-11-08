# -*- coding: utf-8 -*-
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

import common
from logger import get_logger
from llm.dedup import dedup_by_title_llm

load_dotenv()

logger = get_logger(__name__)

# FT PressReader edition IDs
FT_EDITION_IDS = ["v99c", "1389", "v99e", "6324", "v99f"]

PRESSREADER_DOMAIN = "https://ft.pressreader.com"
FT_CONTENT_DOMAIN = "https://www.ft.com/content"
FT_CHROM_PROXY_URL = "http://127.0.0.1:9224"

# FT.com 板块列表（含分页参数）
FT_SECTION_URLS = [
    "https://www.ft.com/world?page={pageIndex}",
    "https://www.ft.com/us?page={pageIndex}",
    "https://www.ft.com/companies?page={pageIndex}",
    "https://www.ft.com/technology?page={pageIndex}",
    "https://www.ft.com/markets?page={pageIndex}",
    "https://www.ft.com/opinion?page={pageIndex}",
]


def fetch_print_edition_article_ids_by_date(date: str) -> list[dict]:
    """
    根据日期获取 FT 日报所有文章的 ArticleId 和 Title。
    访问所有 edition URL，监听 /services/pagesMetadata 响应提取数据，最后去重。

    :param date: 日期字符串，格式如 20260707
    :return: 去重后的文章列表，每项包含 {"articleId": str, "title": str, "url": str}
    """
    all_articles: list[dict] = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(FT_CHROM_PROXY_URL)
            context = browser.contexts[0] if browser.contexts else browser.new_context()

            for edition_id in FT_EDITION_IDS:
                url = f"{PRESSREADER_DOMAIN}/{edition_id}/{date}/textview"
                # 使用 mutable 容器避免闭包循环引用问题
                state = {"articles": [], "received": False}

                page = context.new_page()

                def handle_response(response, state=state):
                    if "/services/pagesMetadata" in response.url:
                        try:
                            resp_data = response.json()
                            if isinstance(resp_data, list):
                                for issue in resp_data:
                                    articles = issue.get("Articles", [])
                                    for article in articles:
                                        article_id = article.get("ArticleId")
                                        title = article.get("Title", "")
                                        if article_id:
                                            state["articles"].append({
                                                "title": title.strip() if title else "",
                                                "timestamp": datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                "url": f"{PRESSREADER_DOMAIN}/article/{article_id}",
                                            })
                            state["received"] = True
                            logger.info(f"从 {edition_id}/{date} 成功截获 pagesMetadata，"
                                        f"提取到 {len(state['articles'])} 篇文章")
                        except Exception as e:
                            logger.error(f"解析 pagesMetadata 响应失败: {e}")

                page.on("response", handle_response)

                logger.info(f"正在加载: {url}")
                try:
                    page.goto(url, wait_until="load", timeout=60000)
                except Exception as e:
                    logger.warning(f"页面加载失败: {url}, {e}")
                    page.close()
                    continue

                # 轮询等待网络监听器捕获到响应
                for _ in range(50):
                    if state["received"]:
                        break
                    page.wait_for_timeout(100)

                all_articles.extend(state["articles"])
                logger.info(f"Edition {edition_id} 提取到 {len(state['articles'])} 篇文章")
                page.close()

        except Exception as e:
            logger.error(f"Playwright 发生致命错误: {e}")

    # 按 articleId 去重（保留第一个出现的）
    seen = set()
    unique_articles = []
    for article in all_articles:
        if article["articleId"] not in seen:
            seen.add(article["articleId"])
            unique_articles.append(article)

    logger.info(f"所有 edition 提取完成，去重前: {len(all_articles)}，去重后: {len(unique_articles)} 篇文章")
    return unique_articles


def fetch_article_url_by_count(count: int) -> list[dict]:
    """
    从今天开始向前倒推 count 天（排除周日），收集每天的 FT 日报文章信息。

    :param count: 需要往前提取的天数
    :return: 所有日期的去重文章列表
    """
    all_articles: list[dict] = []
    today = datetime.now()
    current = today
    days_collected = 0

    while days_collected < count:
        # 跳过周日 (weekday() == 6 表示周日)
        if current.weekday() == 6:
            current -= timedelta(days=1)
            continue

        date_str = current.strftime("%Y%m%d")
        logger.info(f"--- 正在处理日期: {date_str} (第 {days_collected + 1}/{count} 天) ---")
        all_articles.extend(fetch_print_edition_article_ids_by_date(date_str))
        days_collected += 1
        current -= timedelta(days=1)

    # 先去重 articleId
    seen_ids = set()
    unique_by_id = []
    for article in all_articles:
        if article["articleId"] not in seen_ids:
            seen_ids.add(article["articleId"])
            unique_by_id.append(article)

    # LLM 按标题去重
    if unique_by_id:
        logger.info("正在调用 LLM 进行标题去重...")
        final_articles = dedup_by_title_llm(unique_by_id)
    else:
        final_articles = []

    logger.info(f"全部日期提取完成，articleId去重后: {len(unique_by_id)}，标题去重后: {len(final_articles)} 篇")
    return final_articles




def fetch_article_content_by_url(url: str) -> dict:
    """
    通过 Playwright 打开 FT 文章页面，提取 article-text 标签内的正文内容。

    :param url: 文章 URL (https://ft.pressreader.com/article/{ArticleId})
    :return: {"content": str, "timestamp": str | None}
    """
    result: dict = {"content": "", "timestamp": None}

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(FT_CHROM_PROXY_URL)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            # 拦截图片/字体等资源加速加载
            page.route("**/*", lambda route: route.abort()
                       if route.request.resource_type in ["image", "media", "font"]
                       else route.continue_())

            logger.info(f"正在加载文章: {url}")
            page.goto(url, wait_until="load", timeout=60000)

            # 模拟滚动触发懒加载
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            page.wait_for_timeout(1500)

            # 提取 article-text 标签内容
            logger.info("正在提取正文段落...")
            content_parts: list[str] = []
            article_el = page.query_selector("article-text")
            if article_el:
                # 尝试获取所有子段落
                paragraphs = article_el.query_selector_all("p")
                if paragraphs:
                    for p_el in paragraphs:
                        text = p_el.inner_text().strip()
                        if text:
                            content_parts.append(text)
                else:
                    # 没有 p 标签则取全部文本
                    text = article_el.inner_text().strip()
                    if text:
                        content_parts.append(text)

            # 尝试提取时间
            time_el = page.query_selector("time[datetime]")
            if time_el:
                dt = time_el.get_attribute("datetime")
                if dt:
                    result["timestamp"] = dt

            logger.info(f"成功提取到 {len(content_parts)} 个段落")
            # 段落列表直接合并成单个 str，爬取阶段即为字符串，入库时保持 str
            result["content"] = "\n\n".join(content_parts)
            page.close()

        except Exception as e:
            logger.error(f"提取文章内容时发生错误 [{url}]: {e}")

    return result


def fetch_print_edition_paragraph(count: int) -> list[dict]:
    article_info_list = fetch_article_url_by_count(count)
    for article_info in article_info_list:
        content = common.get_news_paragraphs(article_info["url"], '[class="article-text"]', FT_CHROM_PROXY_URL, None, None)
        if content and len(content["content"]) > 0:
            article_info["content"] = content["content"]
    return article_info_list


# =============================================================================
# FT.com 板块文章爬取（需求文档函数 1/2/3）
# =============================================================================


def get_article_ids_from_section(url: str) -> list[str]:
    """
    函数2：从 FT 板块页面获取所有文章的 data-id。

    找到 data-ref="lev" 的标签，在其子标签中递归查找 data-id 属性值，去重后返回。

    :param url: FT 板块页面 URL（如 https://www.ft.com/world?page=1）
    :return: 去重后的 data-id 列表
    """
    article_ids: list[str] = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(FT_CHROM_PROXY_URL)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            # 拦截图片/字体等资源加速加载
            page.route("**/*", lambda route: route.abort()
                       if route.request.resource_type in ["image", "media", "font"]
                       else route.continue_())

            logger.info(f"正在加载板块页面: {url}")
            page.goto(url, wait_until="load", timeout=60000)

            # 查找所有 data-ref="lev" 的元素
            lev_elements = page.query_selector_all('[data-ref="lev"]')

            for lev_el in lev_elements:
                # 在该元素下查找所有带有 data-id 的子元素
                id_elements = lev_el.query_selector_all("[data-id]")
                for el in id_elements:
                    did = el.get_attribute("data-id")
                    if did:
                        article_ids.append(did)

            page.close()

        except Exception as e:
            logger.error(f"获取板块文章列表时发生错误 [{url}]: {e}")

    # 去重
    unique_ids = list(set(article_ids))
    logger.info(f"从 {url} 提取到 {len(unique_ids)} 个唯一 data-id (原始: {len(article_ids)})")
    return unique_ids


def fetch_all_news_url(page_count: int) -> list[str]:
    """
    函数1：遍历所有 FT 板块和页码，获取所有文章 URL。

    :param page_count: 需要获取的页数（pageIndex 从 1 到 page_count）
    :return: 完整的文章 URL 列表（已去重）
    """
    all_urls: list[str] = []

    for section_url_template in FT_SECTION_URLS:
        for page_index in range(1, page_count + 1):
            url = section_url_template.format(pageIndex=page_index)
            logger.info(f"=== 正在处理: {url} ===")

            data_ids = get_article_ids_from_section(url)

            for data_id in data_ids:
                full_url = f"{FT_CONTENT_DOMAIN}/{data_id}"
                all_urls.append(full_url)

    # 全局去重
    unique_urls = list(set(all_urls))
    logger.info(f"所有板块爬取完成，共获取 {len(unique_urls)} 篇独立文章 (原始: {len(all_urls)})")
    return unique_urls


def scrape_all(urls: list[str]) -> list[dict]:
    """
    函数3：爬取 FT 文章正文内容。

    使用 common.get_news_paragraphs 提取正文段落和时间。

    :param urls: 文章 URL 列表
    :return: 文章内容列表，每项包含 url / content / timestamp / llmDone
    """
    new_articles: list[dict] = []

    for url in urls:
        logger.info(f"--- 正在爬取: {url} ---")
        result = common.get_news_paragraphs(
            url,
            '[data-attribute="article-content-body"] p',
            FT_CHROM_PROXY_URL,
            "time[datetime]",
            "datetime",
        )
        if result and len(result["content"]) > 0:
            new_articles.append({
                "url": url,
                "content": result["content"],
                "timestamp": result.get("timestamp"),
                "llmDone": False,
            })
        else:
            logger.warning(f"文章内容为空或提取失败: {url}")

        time.sleep(1)

    logger.info(f"FT 爬取结束！本次共爬取 {len(new_articles)} 篇文章。")
    return new_articles