# -*- coding: utf-8 -*-
"""
Agent 核心数据辅助脚本 (llm/agent_helper.py)

功能：
1. 灵活读取待总结新闻：支持指定媒体来源 (business)、指定条数 (limit)、指定特定 URL。
2. 批量与单篇精准保存：
   - 自动推导文章时间，存入 `llm/summary/{来源}/{YYYY-MM}/{YYYY-MM-DD}.md`
   - 自动基于文章 url 查找并回写对应 Parquet，将 llmDone 标记为 True。
3. 状态统计：一键查看各新闻媒体当前的待处理（未总结）文章数量。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Optional, Union

# 确保能正确引入根目录下的 common 和 llm 模块
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if sys.platform == "win32":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import pandas as pd

from common.common_tools import PARQUET_LOCK
from common.lock_manager import (
    STATUS_FETCHING,
    clear_lock,
    get_lock_status,
    is_fetching,
)
from llm.batch import BUSINESS
from llm.summarizer import save_summary



BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY_BASE = os.path.join(BASE_DIR, "summary")


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
        # 兼容 YYYY-MM-DD 或 ISO 格式
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            pass
        if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
            return ts[:10]
    return datetime.now().strftime("%Y-%m-%d")


def _clean_content(raw: str, max_chars: int = 1100) -> str:
    """清理新闻正文中的 HTML 标签与冗余空白，截断至合理长度以保证管道通信不溢出。"""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def get_pending(
    business: Optional[str] = None,
    limit: int = 20,
    url: Optional[str] = None
) -> list[dict]:
    """
    获取待总结 (llmDone != True 且有正文) 的新闻文章。
    若某业务源处于爬取中 (FETCHING)，则主动避让跳过。

    :param business: 来源标识（如 'wsj', 'bloomberg', 'ft', 'economist', 'economist_weekly', 'barrons_weekly', 'wsj_print'）
                     若为 None 则扫描所有可用业务。
    :param limit: 获取的最大条数（硬约束：最少 5 篇）。
    :param url: 若指定，则精确查询该 URL 的未处理文章。
    :return: 文章列表，每项包含: business, source, url, timestamp, content
    """
    if limit <= 0:
        limit = 5

    targets = [business] if business else list(BUSINESS.keys())
    results = []

    for biz in targets:
        if biz not in BUSINESS:
            print(f"[Warning] 未知业务标识: {biz}", file=sys.stderr)
            continue

        # 检查该新闻源是否正在爬取中，若是则主动跳过避让
        if is_fetching(biz):
            print(f"[Info] 业务源 '{biz}' 当前正在爬取中 (状态: FETCHING)，已跳过本次总结调度。", file=sys.stderr)
            continue

        biz_config = BUSINESS[biz]
        data_path = biz_config["data"]
        if not os.path.exists(data_path):
            continue

        with PARQUET_LOCK:
            try:
                df = pd.read_parquet(data_path)
                if "llmDone" not in df.columns:
                    df["llmDone"] = False

                has_content = df["content"].apply(lambda c: hasattr(c, "__len__") and len(c) > 0)
                filtered = df[(df["llmDone"] != True) & has_content]

                if url:
                    filtered = filtered[filtered["url"] == url]

                if not filtered.empty:
                    needed = limit - len(results)
                    records = filtered.head(needed).to_dict(orient="records")
                    for r in records:
                        raw_content = str(r.get("content", ""))
                        item = {
                            "business": biz,
                            "source": biz,
                            "url": r.get("url", ""),
                            "timestamp": str(r.get("timestamp", "")),
                            "content": _clean_content(raw_content),
                        }
                        if "magazine_date" in r and pd.notna(r.get("magazine_date")) and r.get("magazine_date"):
                            item["magazine_date"] = str(r["magazine_date"])
                        results.append(item)
                    if len(results) >= limit:
                        break
            except Exception as e:
                print(f"[Warning] 读取 {data_path} 失败: {e}", file=sys.stderr)

    return results[:limit]


def get_status() -> dict:
    """
    统计并返回各个媒体来源当前未总结 (llmDone != True 且有正文) 的文章数量及 Lock 状态。
    """
    stats = {}
    locks = {}
    for biz, biz_config in BUSINESS.items():
        locks[biz] = get_lock_status(biz)
        data_path = biz_config["data"]
        if not os.path.exists(data_path):
            stats[biz] = 0
            continue

        with PARQUET_LOCK:
            try:
                df = pd.read_parquet(data_path)
                if "llmDone" not in df.columns:
                    df["llmDone"] = False
                has_content = df["content"].apply(lambda c: hasattr(c, "__len__") and len(c) > 0)
                count = int(((df["llmDone"] != True) & has_content).sum())
                stats[biz] = count
            except Exception as e:
                stats[biz] = 0
    return {"pending_counts": stats, "lock_statuses": locks}



def save_summary_item(business: str, url: str, timestamp: str, summary: str) -> bool:
    """
    保存单篇新闻的总结，并精准将对应 Parquet 中的该 url 标记为 llmDone=True。

    :param business: 业务来源（如 wsj, bloomberg 等）
    :param url: 文章链接
    :param timestamp: 文章发布时间戳
    :param summary: Markdown 格式的总结内容
    :return: 是否保存成功
    """
    return save_batch([{
        "business": business,
        "url": url,
        "timestamp": timestamp,
        "summary": summary
    }])["saved_count"] > 0


def _check_summary_garbled(summary: str) -> Optional[str]:
    """
    检测总结是否因编码错误（如通过 python - / python -c 中转）变成了问号乱码 (如 ?? **????**)
    """
    if not summary:
        return None
    import re
    has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', summary))
    # 若完全没有中文字符，且包含连续问号或大量问号
    if not has_chinese and ("??" in summary or summary.count("?") >= 10):
        return "内容不含任何有效中文字符，且包含大量问号 '?'（疑似 Windows 编码损坏乱码）"
    # 若问号数量异常多且中文字符比例极低
    if summary.count("?") > 20 and len(re.findall(r'[\u4e00-\u9fa5]', summary)) < 20:
        return "问号数量异常过多，且有效中文字数过少（疑似编码截断或损坏）"
    return None


def save_batch(items: list[dict]) -> dict:
    """
    批量保存总结内容：
    1. 校验内容完整性与编码有效性，拦截问号乱码。
    2. 根据每篇文章的 timestamp 计算日期和对应年月子文件夹 (YYYY-MM)。
    3. 将 Markdown 摘要以插入顶部模式存入 `llm/summary/{来源}/{YYYY-MM}/{YYYY-MM-DD}.md`。
    4. 根据 url 加锁原子性更新对应 Parquet 中的 llmDone = True。

    :param items: 列表，每项需包含: business, url, timestamp, summary
    :return: 处理结果字典，如 {"status": "ok", "saved_count": 5}
    """
    if not items:
        return {"status": "ok", "saved_count": 0}

    # 0. 乱码防御自检拦截
    for it in items:
        summary = it.get("summary", "")
        garbled_reason = _check_summary_garbled(summary)
        if garbled_reason:
            err_msg = (
                f"\n{'='*70}\n"
                f"[ERROR] 拒绝保存：检测到总结内容存在严重中文乱码！\n"
                f"原因: {garbled_reason}\n"
                f"URL: {it.get('url', '')}\n"
                f"{'-'*70}\n"
                f"【根本原因与禁令】\n"
                f"你违规在 PowerShell 中使用了 '@\\'...'@ | python -' 或 'python -c' 进行中转执行，导致 Windows 管道默认编码将中文强行转成了问号！\n"
                f"绝对严禁编写或执行任何 Python 脚本中转！请直接在 Shell 中使用标准单引号 Here-String 管道保存：\n\n"
                f"$OutputEncoding = [System.Text.Encoding]::UTF8\n\n"
                f"@'\n"
                f"[\n"
                f"  {{\n"
                f"    \"business\": \"{it.get('business', '')}\",\n"
                f"    \"url\": \"{it.get('url', '')}\",\n"
                f"    \"timestamp\": \"{it.get('timestamp', '')}\",\n"
                f"    \"summary\": \"这里是符合9模块的纯中文Markdown总结\"\n"
                f"  }}\n"
                f"]\n"
                f"'@ | python llm/agent_helper.py save --file -\n"
                f"{'='*70}\n"
            )
            print(err_msg, file=sys.stderr)
            raise ValueError(f"检测到中文乱码，已拒绝入库: {garbled_reason}")

    # 1. 预先查询本地 Parquet 文件中的 magazine_date 映射，避免依赖 LLM 入参传回该字段
    local_mag_date_map: dict[str, dict[str, str]] = {}  # {biz: {url: magazine_date}}
    for it in items:
        biz = it.get("business", "")
        url = it.get("url", "")
        if biz and url and biz not in local_mag_date_map:
            data_path = BUSINESS.get(biz, {}).get("data")
            if data_path and os.path.exists(data_path):
                abs_data_path = os.path.abspath(data_path)
                with PARQUET_LOCK:
                    try:
                        df = pd.read_parquet(abs_data_path)
                        if "url" in df.columns and "magazine_date" in df.columns:
                            valid_df = df[df["magazine_date"].notna()]
                            local_mag_date_map[biz] = dict(zip(valid_df["url"].astype(str), valid_df["magazine_date"].astype(str)))
                        else:
                            local_mag_date_map[biz] = {}
                    except Exception as e:
                        print(f"[Warning] 预查本地 magazine_date 失败 ({data_path}): {e}", file=sys.stderr)
                        local_mag_date_map[biz] = {}
            else:
                local_mag_date_map[biz] = {}

    # 2. 分组组织待写入的摘要
    biz_date_map: dict[str, dict[str, list[str]]] = {}
    biz_urls_map: dict[str, set[str]] = {}

    for it in items:
        biz = it.get("business", "")
        url = it.get("url", "")
        ts = it.get("timestamp", "")
        summary = it.get("summary", "")

        if not (biz and url and summary):
            continue

        # 优先从本地 Parquet 读取 magazine_date；若无则回退入参中的显式日期，最后从 timestamp 推导
        local_mag_date = local_mag_date_map.get(biz, {}).get(url)
        explicit_date = local_mag_date or it.get("magazine_date") or it.get("date")
        date_str = _date_str(explicit_date) if explicit_date else _date_str(ts)
        biz_date_map.setdefault(biz, {}).setdefault(date_str, []).append(summary)
        biz_urls_map.setdefault(biz, set()).add(url)

    total_saved = 0

    # 3. 写入对应日期的 Markdown 文件 (带 YYYY-MM 年月子目录)
    for biz, date_dict in biz_date_map.items():
        summary_dir = BUSINESS.get(biz, {}).get("summary", biz)
        for date_str, summaries in date_dict.items():
            month_str = date_str[:7]
            combined = "\n\n---\n\n".join(summaries)
            target_path = os.path.join(SUMMARY_BASE, summary_dir, month_str, f"{date_str}.md")
            save_summary(combined, append_mode=False, save_path=target_path)
            total_saved += len(summaries)

    # 4. 根据 url 批量更新各来源 Parquet 中的 llmDone = True
    for biz, urls in biz_urls_map.items():
        data_path = BUSINESS.get(biz, {}).get("data")
        if not data_path or not os.path.exists(data_path):
            continue

        abs_data_path = os.path.abspath(data_path)
        with PARQUET_LOCK:
            for attempt in range(5):
                try:
                    df = pd.read_parquet(abs_data_path)
                    if "url" in df.columns:
                        df.loc[df["url"].isin(urls), "llmDone"] = True
                        df.to_parquet(abs_data_path, engine="pyarrow", index=False)
                    break
                except Exception as e:
                    if attempt < 4:
                        import time
                        time.sleep(0.5)
                    else:
                        print(f"[Error] 回写 {data_path} 失败: {e}", file=sys.stderr)

        # 总结完成，清空该业务源的 lock 状态
        clear_lock(biz)

    return {"status": "ok", "saved_count": total_saved}


def main():
    parser = argparse.ArgumentParser(description="Antigravity Agent 辅助新闻读取与存储脚本")
    subparsers = parser.add_subparsers(dest="action", help="执行动作")

    # pending 子命令
    p_parser = subparsers.add_parser("pending", help="获取待总结新闻")
    p_parser.add_argument("--business", "-b", type=str, default=None, help="指定来源标识 (如 wsj, bloomberg 等)")
    p_parser.add_argument("--limit", "-n", type=int, default=20, help="获取条数上限 (默认 20，支持单篇读取传入 1)")
    p_parser.add_argument("--url", "-u", type=str, default=None, help="指定单篇 URL")

    # status 子命令
    subparsers.add_parser("status", help="查看各媒体待处理文章统计及 Lock 状态")

    # save 子命令
    s_parser = subparsers.add_parser("save", help="批量保存总结结果并回写状态")
    s_parser.add_argument("--file", "-f", type=str, help="包含总结结果的 JSON 文件路径，若为 '-' 则从标准输入 (stdin) 读取")
    s_parser.add_argument("--json", "-j", type=str, help="直接传入 JSON 字符串")

    args = parser.parse_args()

    if args.action == "pending":
        records = get_pending(business=args.business, limit=args.limit, url=args.url)
        print(json.dumps(records, ensure_ascii=False))

    elif args.action == "status":
        status_info = get_status()
        pending_counts = status_info.get("pending_counts", {})
        total = sum(pending_counts.values())
        print(json.dumps({
            "pending_counts": pending_counts,
            "lock_statuses": status_info.get("lock_statuses", {}),
            "total_pending": total
        }, ensure_ascii=False, indent=2))

    elif args.action == "save":
        payload = []
        if args.file == "-":
            raw_bytes = sys.stdin.buffer.read()
            raw_input = raw_bytes.decode("utf-8").strip().lstrip("\ufeff")
            payload = json.loads(raw_input) if raw_input else []
        elif args.file and os.path.exists(args.file):
            with open(args.file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        elif args.json:
            payload = json.loads(args.json)

        if isinstance(payload, dict):
            payload = [payload]

        try:
            res = save_batch(payload)
            print(json.dumps(res, ensure_ascii=False, indent=2))
        except ValueError as e:
            sys.exit(1)
        except Exception as e:
            print(f"[Error] 保存异常: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()



if __name__ == "__main__":
    main()
