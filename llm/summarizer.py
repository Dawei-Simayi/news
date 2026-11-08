# -*- coding: utf-8 -*-
import os
import time

from dotenv import load_dotenv
load_dotenv()
import requests
from llm.provider import LLMProvider

BASE_DIR = os.path.dirname(__file__)
SUMMARY_DIR = os.path.join(BASE_DIR, "summary")
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt", "prompt.md")
MARKET_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt", "market_prompt.md")


def call_llm(article_content: str, provider: LLMProvider, save_path: str = PROMPT_PATH) -> str:
    client = provider.create_client()

    with open(save_path, "r", encoding="utf-8") as f:
        prompt_template = f.read()
    create_kwargs = {
        "model": provider.model,
        "messages": [{"role": "system", "content": prompt_template}, {"role": "user", "content": article_content}],
        "stream": False,
        "max_tokens": provider.max_tokens,
        "temperature": provider.temperature,
        "reasoning_effort": provider.reasoning_effort,
    }

    if provider is LLMProvider.vectorengine:
        create_kwargs.update({"extra_body": {"thinking": {"type": "disabled"}}})
    if provider.provider_options:
        create_kwargs["provider"] = provider.provider_options

    response = client.chat.completions.create(**create_kwargs)
    return response.choices[0].message.content


def httpx_call_llm(
    article_content: str,
    provider: LLMProvider,
    save_path: str = PROMPT_PATH
) -> str:

    with open(save_path, "r", encoding="utf-8") as f:
        prompt_template = f.read()

    create_kwargs = {
        "model": provider.model,
        "messages": [
            {
                "role": "system",
                "content": prompt_template
            },
            {
                "role": "user",
                "content": article_content
            }
        ],
        "stream": False,
        "max_tokens": provider.max_tokens,
        "temperature": provider.temperature,
    }

    if provider.models:
        create_kwargs["models"] = provider.models
    else:
        create_kwargs["model"] = provider.model

    if provider.reasoning_effort is not None:
        create_kwargs["reasoning_effort"] = provider.reasoning_effort

    # OpenRouter 专用参数
    if provider.provider_options:
        create_kwargs["provider"] = provider.provider_options

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.environ.get(provider.api_key_env)}"
    }
    url = f"{provider.base_url.rstrip('/')}/chat/completions"
    with provider.create_httpx_client() as client:
        response = client.post(
            url,
            headers=headers,
            json=create_kwargs,
        )
        response.raise_for_status()
        result = response.json()
    return result["choices"][0]["message"]["content"]


def save_summary(summary: str, append_mode: bool = True, save_path: str = "") -> None:
    """
    将 LLM 总结写入 markdown 文件。

    :param summary: LLM 分析结果
    :param source: 新闻来源标识，如 "WSJ"
    :param append_mode: True=追加到末尾，False=插入到开头
    :param save_path: 自定义保存路径（含文件名），默认存到 llm/summary/{日期}.md
    """
    if save_path:
        file_path = save_path
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
    else:
        date_str = time.strftime("%Y-%m-%d")
        month_str = time.strftime("%Y-%m")
        target_dir = os.path.join(SUMMARY_DIR, month_str)
        os.makedirs(target_dir, exist_ok=True)
        file_path = os.path.join(target_dir, f"{date_str}.md")

    if append_mode:
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(summary)
    else:
        summary = f"\n{summary}\n\n---\n\n"
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                old_content = f.read()
        else:
            old_content = ""
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(summary + old_content)
