import os
import sys

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from llm.summarizer import call_llm
from llm.agent_helper import save_summary_item
from llm.provider import LLMProvider
from llm.batch import BUSINESS, submit_analysis_batches

def process_parquet_by_link_matching(
    parquet_input_path: str,
    parquet_output_path: str,
    summary_dir: str = './llm/summary',
    url_column: str = 'url'  # Parquet 中存放文章链接/URL的列名，根据实际情况修改
):
    # 1. 递归读取所有 .md 文件，构建 [内容 -> 文件名(日期)] 的映射关系
    # 建立一个字典，存储每个 md 文件包含的所有链接或文本
    file_contents = {}
    
    print(f"正在扫描并读取 '{summary_dir}' 及其子目录下的 .md 文件...")
    for root, dirs, files in os.walk(summary_dir):
        for file in files:
            if file.endswith('.md'):
                # 提取 md 文件名（去除后缀），作为日期
                date_name = os.path.splitext(file)[0]
                file_path = os.path.join(root, file)
                
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        file_contents[date_name] = content
                except Exception as e:
                    print(f"读取文件失败 {file_path}: {e}")

    print(f"共加载 {len(file_contents)} 个 .md 文件。")

    # 2. 读取 Parquet 文件
    df = pd.read_parquet(parquet_input_path)

    # 3. 定义查找函数：根据文章链接在 md 内容中查找匹配的日期
    def find_magazine_date(url):
        if not url or pd.isna(url):
            return None
        url_str = str(url).strip()
        
        # 遍历所有 md 文件内容，查看当前文章链接是否存在于 md 中
        for date_name, content in file_contents.items():
            if url_str in content:
                return date_name
        return None

    # 4. 执行匹配（将 url 替换为对应 .md 的文件名）
    print("正在匹配文章链接与 md 内容...")
    df['magazine_date'] = df[url_column].apply(find_magazine_date)

    # 5. 保存新的 Parquet 文件
    df.to_parquet(parquet_output_path, index=False)

    matched_count = df['magazine_date'].notna().sum()
    print(f"处理完成！共有 {matched_count}/{len(df)} 条记录成功匹配到 md 文件日期。")
    print(f"新文件已保存至: {parquet_output_path}")


def print_unmatched_articles(
    parquet_or_df, 
    summary_dir: str = './llm/summary', 
    url_column: str = 'url'
):
    """
    检查并在终端直接 print 出未在 md 文件中找到匹配的文章
    """
    # 1. 兼容输入类型（文件路径或 DataFrame）
    if isinstance(parquet_or_df, str):
        df = pd.read_parquet(parquet_or_df)
    else:
        df = parquet_or_df.copy()

    # 2. 读取所有 .md 内容并合并
    file_contents = []
    for root, _, files in os.walk(summary_dir):
        for file in files:
            if file.endswith('.md'):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_contents.append(f.read())
                except Exception as e:
                    print(f"读取文件失败 {file_path}: {e}")

    all_md_text = "\n".join(file_contents)

    # 3. 找出未匹配的数据
    unmatched_df = df[
        df[url_column].apply(lambda url: str(url).strip() not in all_md_text if pd.notna(url) else True)
    ]

    # 4. 直接 print 输出结果
    print("\n" + "="*50)
    print(f"📊 未匹配文章排查结果（总计 {len(df)} 条，未匹配 {len(unmatched_df)} 条）：")
    print("="*50)

    if unmatched_df.empty:
        print("🎉 恭喜！所有文章都在 .md 文件中找到了匹配。")
    else:
        # 遍历打印未匹配的文章 URL（包含原 DataFrame 中的索引 index）
        for idx, row in unmatched_df.iterrows():
            url_val = row[url_column]
            print(f"[ 行索引 {idx} ] -> {url_val}")
    
    print("="*50 + "\n")



def main(
    parquet_path: str,
    business: str,
    provider: LLMProvider = LLMProvider.AG_TOOL,
    prompt_path: str = "./llm/prompt/prompt.md",
):
    """
    读取 parquet_path 中 llmDone=False 且 content 非空的文章，
    若 provider 为 OpenRouter，则批量提交至 Batch API (submit_analysis_batches)；
    否则逐篇调用 call_llm 进行总结，通过 save_summary_item 保存到本地
    Markdown 并将对应行的 llmDone 回写为 True。

    :param parquet_path: Parquet 文件路径，如 './.data/economist_weekly.parquet'
    :param business:     业务标识，如 'economist_weekly'（须在 BUSINESS 字典中存在）
    :param provider:     LLM 提供者，默认 LLMProvider.AG_TOOL
    :param prompt_path:  提示词模板路径，默认 './llm/prompt/prompt.md'
    """
    if not os.path.exists(parquet_path):
        print(f"[Error] 文件不存在: {parquet_path}", file=sys.stderr)
        sys.exit(1)

    df = pd.read_parquet(parquet_path)

    if "llmDone" not in df.columns:
        df["llmDone"] = False

    # 筛选 llmDone=False 且 content 非空的行
    has_content = df["content"].apply(lambda c: isinstance(c, str) and len(c.strip()) > 0)
    pending = df[(df["llmDone"] != True) & has_content].copy()

    total = len(pending)
    print(f"[Info] 共找到 {total} 篇待总结文章（来源: {business}）")

    if total == 0:
        print("[Info] 无待处理文章，退出。")
        return

    # 若模型提供商为 OpenRouter，切换为异步批量提交
    if provider == LLMProvider.OPEN_ROUTER:
        articles = pending.to_dict(orient="records")
        submitted = submit_analysis_batches(articles, business)
        print(f"[Batch] OpenRouter 批处理已提交 {len(submitted)} 个批次（共 {total} 篇待分析文章）。后续由后台 batch_job 处理并回写。")
        return

    success = 0
    failed = 0

    for idx, (row_idx, row) in enumerate(pending.iterrows(), start=1):
        url = str(row.get("url", ""))
        timestamp = str(row.get("timestamp", ""))
        content = str(row.get("content", ""))

        # 构造输入给 LLM 的上下文（与 prompt.md 要求的格式对应）
        article_input = (
            f"source: {business}\n"
            f"timestamp: {timestamp}\n"
            f"url: {url}\n\n"
            f"content:\n{content}"
        )

        print(f"[{idx}/{total}] 正在总结: {url[:80]} ...")

        try:
            summary = call_llm(article_input, provider=provider, save_path=prompt_path)
            ok = save_summary_item(
                business=business,
                url=url,
                timestamp=timestamp,
                summary=summary,
            )
            if ok:
                success += 1
                print(f"  ✅ 已保存并标记 llmDone=True")
            else:
                failed += 1
                print(f"  ⚠️  save_summary_item 返回 False，跳过", file=sys.stderr)
        except Exception as e:
            failed += 1
            print(f"  ❌ 总结失败: {e}", file=sys.stderr)

    print(f"\n[完成] 处理 {total} 篇 | 成功 {success} 篇 | 失败 {failed} 篇")


if __name__ == "__main__":

    provider = LLMProvider.OPEN_ROUTER
    prompt_path = "./llm/prompt/prompt.md"

    total_success = 0
    total_failed = 0

    for biz, cfg in BUSINESS.items():
        parquet_path = cfg["data"]
        if not os.path.exists(parquet_path):
            print(f"\n[跳过] {biz} — 数据文件不存在: {parquet_path}")
            continue

        print(f"\n{'='*60}")
        print(f"📰 开始处理业务源: {biz}  ({parquet_path})")
        print(f"{'='*60}")

        main(
            parquet_path=parquet_path,
            business=biz,
            provider=provider,
            prompt_path=prompt_path,
        )


