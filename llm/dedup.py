# -*- coding: utf-8 -*-
import json

from llm.provider import LLMProvider

_DEDUP_SYSTEM_PROMPT = (
    "你是一个新闻去重助手。给定一个标题列表（每行格式为 [索引] 标题），"
    "请识别出内容相同或本质相同的新闻标题（例如同一新闻在不同地区版本的微小差异），"
    "并返回需要保留的唯一标题索引列表。\n\n"
    "规则：\n"
    "1. 内容本质相同的新闻只保留第一个出现的索引\n"
    "2. 只输出一个 JSON 数组，如 [0, 3, 5]，不要多余的解释\n"
    "3. 标题即使措辞有细微差异，只要说的是同一件事，就视为重复"
)


def dedup_by_title_llm(articles: list[dict]) -> list[dict]:
    """
    使用 LLM 按标题相似度对文章去重。
    将标题列表发给 LLM，让其返回每组重复标题中保留的索引。

    :param articles: 文章列表，每项需包含 "title" 字段
    :return: 去重后的文章列表
    """
    if not articles:
        return []

    lines = []
    for i, a in enumerate(articles):
        title = a.get("title", "").strip()
        if title:
            lines.append(f"[{i}] {title}")

    if not lines:
        return articles

    title_text = "\n".join(lines)
    user_prompt = f"请对以下标题去重，返回需要保留的索引数组：\n\n{title_text}"

    try:
        client = LLMProvider.vectorengine.create_client()
        response = client.chat.completions.create(
            model=LLMProvider.vectorengine.model,
            messages=[
                {"role": "system", "content": _DEDUP_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
            max_tokens=4096,
            temperature=0,
        )
        result_text = response.choices[0].message.content.strip()
        # 提取 JSON 数组
        if "[" in result_text and "]" in result_text:
            result_text = result_text[result_text.index("["):result_text.index("]") + 1]
        keep_indices = json.loads(result_text)
        from logger import get_logger
        logger = get_logger(__name__)
        logger.info(f"LLM 去重完成：输入 {len(articles)} 篇，保留 {len(keep_indices)} 篇")
        return [articles[i] for i in keep_indices if 0 <= i < len(articles)]
    except Exception as e:
        from logger import get_logger
        logger = get_logger(__name__)
        logger.error(f"LLM 去重失败: {e}，返回原始列表")
        return articles
