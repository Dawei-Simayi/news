# -*- coding: utf-8 -*-
import json
import os
import re
import threading
import requests
from typing import Any

import pandas as pd
from playwright.sync_api import sync_playwright

from logger import get_logger
from urllib.parse import urlparse
logger = get_logger(__name__)

# 全局 parquet 读写锁：合并写入（run_fetch.merge_and_save）与后台批次收尾（batch_job）
# 都会对同一 data.parquet 做"读→改→写"。Windows 下并发读写同一文件会触发
# [Errno 22] Invalid argument（pyarrow 临时文件/rename 被占用）。此锁保证对 parquet
# 的读改写串行化，避免丢更新与文件冲突。
PARQUET_LOCK = threading.Lock()


def get_next_data(html_content: str, json_target_key: str, tag_id: str = "__NEXT_DATA__") -> list:
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
        return []

def get_article_list(url: str, json_target_key: str, chrom_proxy_url: str) -> any:
    """
    【全新重构：纯网络响应监听模式】
    通过监听网络流直接截获最原始的 HTML 响应，从中离线抽取 __NEXT_DATA__
    """
    # 局部容器，用于在网络监听器中暂存数据
    scraped_payload = {"html_text": "", "status": None}

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(chrom_proxy_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            # 🌟 【核心逻辑】定义网络响应监听器
            def handle_response(response):
                # 只要响应的 URL 和我们请求的列表页 URL 完全一致，说明这就是最核心的原始 HTML 数据包
                if response.url == url:
                    scraped_payload["status"] = response.status
                    # 状态不是 200：大概率是人机验证页。这时不提取 html_text，
                    # 等验证通过、页面真正渲染完成返回 200 后再截获，否则拿到的是验证页的 HTML。
                    if response.status != 200:
                        logger.warning(f"列表页状态不对 ({response.status})，等待自动跳转到目标页面: {url}")
                        page.wait_for_timeout(3000)
                        return
                    try:
                        # 只有确认 200 才截获最原始的、未经 JS 修改的 HTML 文本
                        scraped_payload["html_text"] = response.text()
                        logger.info(f"网络层成功截获原始数据包! [Status: {response.status}]")
                    except Exception as e:
                        logger.error(e)

            # 绑定网络响应监听器
            page.on("response", handle_response)

            logger.info(f"正在开启网络监听加载: {url}")

            try:
                page.goto(url, wait_until="load", timeout=300000)
            except Exception as e:
                logger.warning(f"首次加载可能被人机验证页拦截，等待自动跳转到目标页面: {e}")

            # 🌟 人机验证处理：目标页常常先出现人机验证页，停留数秒后会自动跳转到目标页面。
            # 因此这里必须轮询等待，直到从 HTML 中真正解析出数据，而不是拿到验证页就立即返回空。
            for _ in range(20):  # 最长等约 60 秒，足够验证页自动跳转

                # 每次 HTML 更新后都重新尝试解析，直到拿到真实数据
                if scraped_payload["html_text"]:
                    articles = get_next_data(scraped_payload["html_text"], json_target_key=json_target_key)
                    if articles:
                        page.close()
                        return articles

                page.wait_for_timeout(3000)

            # 超时仍未解析到数据
            page.close()
            logger.error(f"等待超时，人机验证后仍未拿到 {json_target_key} 数据: {url}")
            return []

        except Exception as e:
            logger.error(f"列表页网络监听发生致命错误: {e}")
            return []


def skipp_url(url):
    """判断 URL 是否同时满足：
    1) 域名为 economist.com（含 www 等子域）
    2) 路径结尾为 economic-data-commodities-and-markets
    """
    p = urlparse(url)

    # --- 域名判定 ---
    host = (p.netloc or '').lower().split(':')[0]           # 去掉端口号
    domain_ok = host == 'economist.com' or host.endswith('.economist.com')
    # 用 endswith('.economist.com') 而非 'economist.com'，可挡住
    # evil.com/economist.com（子域判断）和 noteconomist.com（后缀混淆）这类伪域名

    # --- 结尾判定 ---
    tail = (p.path or '').rstrip('/')                        # 去掉可能存在的尾部斜杠
    end_ok = tail.endswith('/economic-data-commodities-and-markets') or \
             tail == 'economic-data-commodities-and-markets'

    return domain_ok and end_ok


def get_news_paragraphs(url: str, selector: str, chrome_proxy_url: str, time_selector: str=None, time_attribute: str=None) -> dict:
    """
    通过 Playwright 加载 Bloomberg, economist 文章页面，监听网络响应提取 datetime标签，
    同时从 DOM 中提取所有 data-component="paragraph" 的段落内容。

    :param url: 目标文章 URL
    :return: {"content": str, "timestamp": str | None}
    """
    result = {"content": "", "timestamp": None}

    if skipp_url(url=url):
        return result
    
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(chrome_proxy_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        logger.info(f"正在加载文章: {url}")
        try:
            page.goto(url, wait_until="load", timeout=300000)
        except Exception as e:
            logger.warning(f"首次加载可能被人机验证页拦截或跳转: {e}")

        def get_paragraphs():
            """获取段落 DOM。若页面因跳转/刷新导致执行上下文销毁，等待加载完成后重新获取 DOM"""
            try:
                return page.query_selector_all(selector)
            except Exception as e:
                if "destroyed" in str(e) or "navigation" in str(e):
                    logger.info("检测到页面正在跳转/刷新，等待加载完成后重新获取 DOM...")
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    try:
                        return page.query_selector_all(selector)
                    except Exception:
                        return []
                return []

        # 🌟 人机验证处理：目标页常常先出现人机验证页，停留数秒后会自动跳转到目标页面。
        paragraphs = []
        for i in range(20):  # 最长等约 60 秒，足够验证页自动跳转
            paragraphs = get_paragraphs()
            if paragraphs:
                break
            page.wait_for_timeout(3000)

        if not paragraphs:
            logger.error(f"等待超时，人机验证后仍未提取到文章段落 [{url}]")
            send_feishu_message()
            # 刷新/跳转后重新等待页面加载完成，再获取一次 DOM
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            paragraphs = get_paragraphs()
            if not paragraphs:
                page.close()
                return result

        # 模拟滚动触发懒加载
        try:
            page.evaluate("window.scrollBy(0, window.innerHeight)")
        except Exception as e:
            logger.warning(f"模拟滚动失败 [{url}]: {e}")

        # 优先从 DOM 中 time[datetime] 提取时间
        if time_selector:
            try:
                time_el = page.query_selector(time_selector)
                if time_el:
                    dt = time_el.get_attribute(time_attribute)
                    if dt:
                        result["timestamp"] = dt
            except Exception as e:
                logger.warning(f"提取时间标签失败 [{url}]: {e}")

        logger.info("正在提取正文段落...")
        content_parts: list[str] = []
        for p_element in paragraphs:
            try:
                text = p_element.inner_text().strip()
                if text:
                    content_parts.append(text)
            except Exception as e:
                logger.warning(f"提取段落文本失败 [{url}]: {e}")
                continue

        logger.info(f"成功提取到 {len(content_parts)} 个段落。")
        # 段落列表直接合并成单个 str（两个换行连接），内容在爬取阶段即为字符串，
        # 后续追加入库时保持 str，避免 parquet 因存 numpy 数组触发字典冲突。
        result["content"] = "".join(content_parts)
        page.close()

    return result


def normalize_url(url: str) -> str:
    """
    将 URL 标准化：去除查询参数(?)和锚点(#)，统一转小写。
    用于 URL 的去重和比对。
    """
    parsed = urlparse(url.strip())
    # 将 scheme 和 netloc 统一转小写，防止因大小写导致的误判
    base_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}"
    return base_url


def normalize_content(val) -> str:
    """把 content 值统一成字符串。

    爬虫返回的 content 是段落列表（存库时可能变成 numpy 数组），会导致 parquet 出现
    "Column cannot have more than one dictionary" 的字典编码冲突。写入 parquet 前用本函数
    将 content 归一成单个 str（段落用两个换行连接），None/空转成空串。

    :param val: 任意 content 值（str / list / tuple / np.ndarray / None）
    :return: 归一化后的 str
    """
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        return "\n\n".join(str(x) for x in val)
    try:
        import numpy as np
        if isinstance(val, np.ndarray):
            if val.dtype.kind in ("U", "S", "O"):
                return "\n\n".join(str(x) for x in val)
    except ImportError:
        pass
    return str(val)


def read_existing_urls(save_path: str) -> set[str]:
    """从 parquet 文件读取已有 URL 集合。

    Args:
        save_path: Parquet 文件路径。
        llm_done: 可选布尔值。若不为 None，则仅返回 llm_done 列匹配该值的数据；
                  若为 None，则返回全部数据。
    """
    if os.path.exists(save_path):
        try:
            df = pd.read_parquet(save_path)
            if "url" not in df.columns:
                return set()
            # has_content = df['content'].apply(lambda c: hasattr(c, '__len__') and len(c) > 0)
            # df = df[has_content]
            urls = set(df["url"].tolist())
            logger.info(f"读取本地 Parquet 历史数据，共 {len(urls)} 条独立链接")
            return urls
        except Exception as e:
            logger.warning(f"读取本地 Parquet 失败: {e}")
    return set()


def filter_unique_urls(new_urls: list, data_path: str) -> list:
    """
    智能去重过滤：
    1. 支持 new_urls 中的元素为字符串或字典（包含 'url' 键）。
    2. 优先在 new_urls 内部去重，且保留【最后一次】出现的版本（通常参数最全）。
    3. 然后再与 existing_urls 进行历史去重。
    """
    if not new_urls:
        return []

    # 1. 内部去重：倒序遍历，遇到重复的保留后面的（即最新的）
    internal_unique_map = {}
    existing_urls = read_existing_urls(data_path)
    for item in reversed(new_urls):
        raw_url = item["url"] if isinstance(item, dict) else str(item)
        base_url = normalize_url(raw_url)
        internal_unique_map[base_url] = item  # 后面的覆盖前面的

    # 2. 历史去重：过滤掉已经存在于 existing_urls 中的链接
    normalized_existing = {normalize_url(url) for url in existing_urls}
    final_urls = []
    # 因为刚才倒序了，这里再倒序一次，恢复原本的抓取时间顺序
    for base_url in reversed(list(internal_unique_map.keys())):
        if base_url not in normalized_existing:
            final_urls.append(internal_unique_map[base_url])

    return final_urls


def send_feishu_message():
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/d15153fb-e6f8-42f2-a97e-c13d354528f6"
    payload = {
    "msg_type": "text",
    "content": { "text": "<at user_id = \"ou_8e4e66d7f04d45a8eb465175da3b23f8\">Naruto</at> 爬虫卡住了，请检查" }
    }
    headers = {"content-type": "application/json"}

    requests.post(url, json=payload, headers=headers)