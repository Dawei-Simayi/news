#https://www.barrons.com/topics/economy-and-policy
from datetime import datetime, timedelta
import time

import common
from common import get_article_list
from logger import get_logger
from weeklyonstock.weeklyonstock_tools import CHROME_PROXY_URL

logger = get_logger(__name__)

CHROME_PROXY_URL="http://127.0.0.1:9224"

#日期一般是周一
def fetch_barrons_weekly_article_url_list(date: str) -> list[dict]:
    magazine_date = f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 and "-" not in date else date
    url_date = date.replace("-", "")
    weekly_url = f'https://www.barrons.com/magazine/{url_date}'
    magazine_data = get_article_list(weekly_url, json_target_key="magazineData", chrom_proxy_url=CHROME_PROXY_URL)
    if magazine_data is None:
        logger.error(f'没有获取到内容，{weekly_url}')
        return []
    all_targets = []

    sections = [
        magazine_data.get("coverArticle", []),
        magazine_data.get("featureArticles", []),
        magazine_data.get("restArticles", []),
        magazine_data.get("columnsArticles", []),
        magazine_data.get("departmentsArticles", []),
    ]
    seen_urls = set()
    for section in sections:
        for article in section:
            if isinstance(article, dict):
                url = article.get("url")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_targets.append({
                        "url": url,
                        "magazine_date": magazine_date,
                    })

    return all_targets


def fetch_all_weekly_news_url(weekly_count: int) -> list[dict]:
    now = datetime.now()
    days_since_monday = (now.weekday() - 0) % 7
    monday = now - timedelta(days=days_since_monday)
    date_list = []

    days_to_saturday = 5 - now.weekday()
    # 构造本周六北京时间 19:00（美东早上 7 点）的时间点
    saturday_7pm = (now + timedelta(days=days_to_saturday)).replace(
        hour=19, minute=0, second=0, microsecond=0
    )
    if now > saturday_7pm:
        next_monday_delta = monday + timedelta(days=7)
        date_str = next_monday_delta.strftime("%Y%m%d")
        date_list.append(date_str)

    for i in range(weekly_count):
        monday_delta = monday - timedelta(days=i * 7)
        date_str = monday_delta.strftime("%Y%m%d")
        date_list.append(date_str)

    targets = []
    seen = set()
    for date in date_list:
        for item in fetch_barrons_weekly_article_url_list(date):
            if item["url"] not in seen:
                seen.add(item["url"])
                targets.append(item)
    return targets


def scrape_all(targets: list) -> list[dict]:
    """
    爬取 barrons 文章列表 → 增量爬取正文。不负责保存，仅返回新爬取的文章列表。

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
        result = common.get_news_paragraphs(url, '[data-type="paragraph"]', CHROME_PROXY_URL, 'meta[name="article.published"]', "content")
        if len(result["content"]) == 0:
            continue
        new_articles.append({
            "url": url,
            "content": result["content"],
            "timestamp": result.get("timestamp"),
            "magazine_date": mag_date,
            "llmDone": False,
        })
        time.sleep(1)

    logger.info(f"barrons 爬取结束！本次共爬取 {len(new_articles)} 篇文章。")
    return new_articles
