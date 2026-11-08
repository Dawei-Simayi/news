# -*- coding: utf-8 -*-
"""
OpenRouter 服务端 Batch API 封装。

Batch API 允许一次性提交大量推理请求，OpenRouter 异步处理（24 小时完成窗口），
完成后再统一取回结果。比逐篇同步调用 /chat/completions 更便宜、更快。

流程：
1. 提交批次 POST https://openrouter.ai/api/v1/batches
   -> 返回 202 + batch id（status=validating）
2. 轮询 GET https://openrouter.ai/api/v1/batches/:id 直到 terminal
3. status=completed 时 results 内联在响应里，按 custom_id 映射回原请求
"""
import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from logger import get_logger
from common.paths import data_file
from llm.provider import LLMProvider

logger = get_logger(__name__)

TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}

BATCH_ENDPOINT = "/api/v1/batches"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BATCH_REGISTRY_PATH = os.path.join(BASE_DIR, "summary", "batch_id.json")
PROMPT_PATH = os.path.join(BASE_DIR, "prompt", "prompt.md")

# 批次最多多少篇拆一个 batch（OpenRouter/性价比上限，超出拆多批全提交）
BATCH_CHUNK_SIZE = 50

# 不同业务（来源）区分：data 文件 与 summary 目录键。后台 job 靠它回写 llmDone 与落库。
# data 路径统一由 common.paths 生成绝对路径，实际文件位于 <root>/.data/{业务键}.parquet
BUSINESS = {
    "bloomberg":          {"data": data_file("bloomberg"),        "summary": "bloomberg"},
    "wsj":                {"data": data_file("wsj"),              "summary": "wall_street"},
    "wsj_print":          {"data": data_file("wsj_print"),        "summary": "wall_street_print_edition"},
    "economist":          {"data": data_file("economist"),        "summary": "economist"},
    "economist_weekly":   {"data": data_file("economist_weekly"), "summary": "economist_weekly"},
    "barrons_weekly":     {"data": data_file("barrons_weekly"),   "summary": "barrons_weekly"},
    "ft":                 {"data": data_file("ft"),               "summary": "ft"},
}


class BatchError(Exception):
    """Batch 提交或轮询失败"""


def _headers(provider) -> dict[str, str]:
    import os
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.environ.get(provider.api_key_env)}",
    }


def custom_id_from_url(url: str, fallback: str) -> str:
    """生成合规的 custom_id：包含序号前缀保证批次内唯一，只保留字母数字下划线减号，长度合规。"""
    import re
    path = urlparse(str(url or "")).path.rstrip("/")
    raw = path.rsplit("/", 1)[-1] if path else fallback
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", raw).strip("_")
    cleaned = (cleaned or fallback)[:40]
    return f"req_{fallback}_{cleaned}"


def build_requests(items: list[dict], prompt_template: str, provider, model: Optional[str] = None) -> list[dict]:
    """
    将多篇待分析文章构造成 Batch API 的 requests 数组。

    :param items: 每项含 source/url/timestamp/content 的载荷 dict
    :param prompt_template: system prompt 文本（读取自 prompt.md）
    :param provider: LLMProvider 实例（提供 max_tokens/temperature）
    :param model: 可选指定的模型名称
    :return: requests 数组，每项为 {"custom_id", "body"}
    """
    batch_model = (model or provider.batch_model or provider.model or "").removesuffix(":batch")
    requests: list[dict] = []
    for idx, item in enumerate(items):
        body = {
            "model": batch_model,
            "messages": [
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": json.dumps(item, ensure_ascii=False)},
            ],
            "max_tokens": provider.max_tokens,
            "temperature": provider.temperature,
        }
        custom_id = custom_id_from_url(item.get("url") or "", str(idx))
        requests.append({"custom_id": custom_id, "body": body})
    return requests


def _batch_url(provider, path: str = "") -> str:
    base = provider.base_url.rstrip("/")
    if base.endswith("/api/v1"):
        url = f"{base}/batches"
    else:
        url = f"{base}{BATCH_ENDPOINT}"
    if path:
        url = f"{url}/{path.lstrip('/')}"
    return url


def submit_batch(provider, requests: list[dict], model: Optional[str] = None) -> dict:
    """
    提交一个批次的请求。

    :param provider: openLLM Provider
    :param requests: 由 build_requests 生成的 requests 数组
    :param model: 批次模型 slug，默认取 provider.batch_model
    :return: OpenRouter 返回的 batch 对象（含 id 与 status）
    """
    raw_model = model or provider.batch_model or provider.model
    if not raw_model:
        raise ValueError("provider 未配置 batch_model")
    batch_model = raw_model.removesuffix(":batch")

    # 确保每个 request 的 body 内也带上匹配的 model
    for req in requests:
        if "body" in req and "model" not in req["body"]:
            req["body"]["model"] = batch_model

    # ⚠️ 文档强调：endpoint / model 必须序列化在 requests 之前，否则 API 返回 400/422
    payload = {
        "endpoint": "/v1/chat/completions",
        "model": batch_model,
        "requests": requests,
    }

    headers = _headers(provider)
    url = _batch_url(provider)
    with provider.create_httpx_client() as client:
        response = client.post(url, headers=headers, json=payload)
        if response.is_error:
            logger.error("[Batch] OpenRouter 提交失败: HTTP %s, 响应内容: %s", response.status_code, response.text)
            print(f"\n[Batch Error] OpenRouter API 返回 HTTP {response.status_code}: {response.text}\n", file=sys.stderr)
            raise BatchError(f"OpenRouter 批次提交失败 [{response.status_code}]: {response.text}")
        result = response.json()

    logger.info("[Batch] 提交成功 id=%s status=%s 请求数=%d",
                result.get("id"), result.get("status"), len(requests))
    return result


def get_batch(provider, batch_id: str) -> dict:
    """获取一次批次的当前状态/结果（GET /api/v1/batches/:id）。"""
    headers = _headers(provider)
    url = _batch_url(provider, batch_id)
    with provider.create_httpx_client() as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


def poll_batch(
    provider,
    batch_id: str,
    timeout_seconds: float = 600.0,
    poll_interval: float = 10.0,
) -> dict:
    """
    一轮批直到 terminal 状态（或超时）。

    :return: 最终 batch 对象；status=completed 且包含 results 数组
    """
    deadline = time.time() + timeout_seconds
    while True:
        batch = get_batch(provider, batch_id)
        status = batch.get("status")
        logger.info("[Batch] id=%s status=%s completed=%s failed=%s",
                    batch_id, status,
                    (batch.get("request_counts") or {}).get("completed"),
                    (batch.get("request_counts") or {}).get("failed"))

        if status in TERMINAL_STATUSES:
            logger.info("[Batch] id=%s 达到终态：%s", batch_id, status)
            return batch

        if time.time() >= deadline:
            logger.error("[Batch] id=%s 轮询超时（>%.0fs），尚未达到终态", batch_id, timeout_seconds)
            return batch

        time.sleep(poll_interval)


def extract_summaries(batch: dict) -> dict[str, str]:
    """
    从 completed 的 batch 结果中提取摘要，按 custom_id 映射。

    :return: {custom_id: summary}，仅含成功（200）且取到 content 的结果
    """
    summaries: dict[str, str] = {}
    for result in batch.get("results") or []:
        custom_id = result.get("custom_id")
        if not custom_id:
            continue
        # 每个结果恰好一个 response 或 error
        resp = result.get("response") or {}
        if resp.get("status_code") != 200:
            logger.warning("[Batch] custom_id=%s 失败 status=%s error=%s",
                           custom_id, resp.get("status_code"), result.get("error"))
            continue
        body = resp.get("body") or {}
        choices = body.get("choices") or []
        if not choices:
            logger.warning("[Batch] custom_id=%s 响应缺少 choices", custom_id)
            continue
        content = choices[0].get("message", {}).get("content")
        if content:
            summaries[custom_id] = content
    return summaries


# ============================================================
# 批次注册表（llm/summary/batch_id.json）与非阻塞提交
# 提交侧：只提交 + 登记，把"等完成→存 summary→更新 llmDone"交给后台 job（llm/batch_job.py）
# ============================================================

_REGISTRY_LOCK = threading.Lock()


def _safe_str(val, default=""):
    """安全地将任意值转为干净字符串（兼容 parquet 取出的 numpy 标量/数组）。"""
    if val is None:
        return default
    if isinstance(val, (list, tuple)):
        return "".join(str(x) for x in val)
    try:
        import numpy as np
        if isinstance(val, np.ndarray):
            if val.dtype.kind in ('U', 'S', 'O'):
                return "".join(str(x) for x in val)
            return default
    except ImportError:
        pass
    return str(val)


def _date_str(ts) -> str:
    """从 timestamp 推导归档日期 YYYY-MM-DD；缺失/异常回退当天。"""
    if isinstance(ts, str) and ts:
        ts = ts.strip()
        # 兼容 8 位纯数字日期，如 20260819
        if len(ts) == 8 and ts.isdigit():
            try:
                return datetime.strptime(ts, "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            pass
        if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
            return ts[:10]
    return time.strftime("%Y-%m-%d")


def read_registry() -> list[dict]:
    """读取批次注册表；文件不存在/损坏返回空列表。"""
    if not os.path.exists(BATCH_REGISTRY_PATH):
        return []
    try:
        with open(BATCH_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[BatchRegistry] 读取 %s 失败: %s", BATCH_REGISTRY_PATH, e)
        return []


def write_registry(records: list[dict]) -> None:
    """整表写回（临时文件 + os.replace 保证原子性）。"""
    os.makedirs(os.path.dirname(BATCH_REGISTRY_PATH), exist_ok=True)
    tmp = BATCH_REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BATCH_REGISTRY_PATH)


def register_batch(batch: dict, business: str, items: list[dict]) -> dict:
    """把刚提交的批次追加登记到注册表，供后台 job 扫描收尾。"""
    record = {
        "batch_id": batch.get("id"),
        "business": business,
        "model": batch.get("model"),
        "status": batch.get("status") or "pending",
        "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "articles": [
            {
                "url": item.get("url", ""),
                "custom_id": custom_id_from_url(item.get("url") or "", str(idx)),
                "date": _date_str(item.get("magazine_date") or item.get("timestamp")),
            }
            for idx, item in enumerate(items)
        ],
    }
    with _REGISTRY_LOCK:
        records = read_registry()
        records.append(record)
        write_registry(records)
    logger.info("[Batch] 已登记 business=%s batch_id=%s 状态=%s", business, record["batch_id"], record["status"])
    return record


def build_batch_items(articles: list[dict], source: str) -> tuple[list[dict], list[dict]]:
    """把文章列表转成 Batch 载荷 items，过滤空正文；返回 (items, skipped)。"""
    items: list[dict] = []
    skipped: list[dict] = []
    for article in articles:
        content = article.get("content")
        if content is None or len(content) == 0:
            skipped.append(article)
            continue
        payload = {
            "source": source,
            "url": article["url"],
            "timestamp": _safe_str(article.get("timestamp")),
            "content": _safe_str(content),
        }
        if "magazine_date" in article and article.get("magazine_date"):
            payload["magazine_date"] = _safe_str(article.get("magazine_date"))
        items.append(payload)
    return items, skipped


def _read_prompt() -> str:
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def pending_urls() -> set[str]:
    """注册表中所有未达终态批次的文章 URL 集合。

    异步方案下，已提交但批次未完成的文章 llmDone 仍是 False，下一轮抓取若不去重
    会被重复提交。抓取结束后先用本函数过滤掉这些仍在排队/处理中的 URL。
    """
    urls: set[str] = set()
    for rec in read_registry():
        if (rec.get("status") or "pending") in TERMINAL_STATUSES:
            continue  # 已终态（含 completed/failed），不再占用
        for art in rec.get("articles") or []:
            if art.get("url"):
                urls.add(art["url"])
    return urls


def submit_analysis_batches(articles: list[dict], business: str) -> list[dict]:
    """非阻塞提交待审批文章：按 BATCH_CHUNK_SIZE 拆多个 batch 全提交并登记，立即返回。

    收尾（等待批次完成→写 summary→更新 llmDone）由 llm/batch_job.py 的后台 job 负责。
    对已登记在 pending 批次中的 URL 自动过滤，避免同篇文章重复提交。
    :return: 已登记批次列表 [{batch_id, business}]
    """
    if business not in BUSINESS:
        raise KeyError(f"未知业务键: {business}，可用: {list(BUSINESS)}")
    if not articles:
        logger.info("[Batch] 没有待分析文章，跳过提交。")
        return []

    items, skipped = build_batch_items(articles, business)
    if not items:
        logger.warning("[Batch] 所有文章均无正文，跳过提交。")
        return []

    pending = pending_urls()
    if pending:
        before = len(items)
        items = [it for it in items if it["url"] not in pending]
        if len(items) != before:
            logger.info("[Batch] 过滤 %d 篇已处于 pending 批次的文章（避免重复提交）",
                        before - len(items))
    if len(items) == 0:
        logger.info("[Batch] 过滤后没有待提交文章。")
        return []

    logger.info("[Batch] business=%s 共 %d 篇文章，%d 篇参与批处理（跳过 %d 篇空正文）...",
                business, len(articles), len(items), len(skipped))

    prompt_template = _read_prompt()
    chunks = [items[i:i + BATCH_CHUNK_SIZE] for i in range(0, len(items), BATCH_CHUNK_SIZE)]

    submitted: list[dict] = []
    for chunk in chunks:
        requests = build_requests(chunk, prompt_template, LLMProvider.OPEN_ROUTER)
        batch = submit_batch(LLMProvider.OPEN_ROUTER, requests)
        register_batch(batch, business, chunk)
        submitted.append({"batch_id": batch.get("id"), "business": business})
        logger.info("[Batch] business=%s 批次已提交 id=%s 请求数=%d", business, batch.get("id"), len(chunk))
    return submitted