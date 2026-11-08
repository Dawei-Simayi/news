# -*- coding: utf-8 -*-
import os
from typing import ClassVar, Optional

import httpx
from openai import OpenAI


class LLMProvider:
    DEEP_SEEK: ClassVar['LLMProvider']
    OPEN_ROUTER: ClassVar['LLMProvider']
    LOCAL: ClassVar['LLMProvider']
    OMNI_AI: ClassVar['LLMProvider']
    vectorengine: ClassVar['LLMProvider']
    AG_TOOL : ClassVar['LLMProvider']

    def __init__(
        self,
        *,
        api_key_env: Optional[str] = None,
        base_url: str = "",
        model: str = "",
        models: Optional[list[str]] = None,
        batch_model: Optional[str] = None,
        proxy: Optional[str] = None,
        max_tokens: int = 65532,
        temperature: float = 0,
        reasoning_effort: Optional[str] = None,
        provider_options: Optional[dict] = None,
        trust_env: bool=True
    ):
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.model = model
        self.models = models
        self.proxy = proxy
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.provider_options = provider_options
        self.batch_model = batch_model
        self.trust_env = trust_env

    def create_client(self) -> OpenAI:
        kwargs = {"base_url": self.base_url}
        if self.api_key_env:
            kwargs["api_key"] = os.environ.get(self.api_key_env)
        if self.proxy:
            kwargs["http_client"] = httpx.Client(proxy=self.proxy)
        return OpenAI(**kwargs)
    
    def create_httpx_client(self) -> httpx.Client:
        kwargs = {
            "timeout": 3000.0,
            "trust_env": self.trust_env, 
        }

        if self.proxy:
            kwargs["proxy"] = self.proxy

        return httpx.Client(**kwargs)


LLMProvider.DEEP_SEEK = LLMProvider(
    api_key_env="DEEPSEEK_API_KEY",
    base_url="https://api.deepseek.com",
    model="deepseek-v4-flash",
    reasoning_effort="medium",
)

LLMProvider.OPEN_ROUTER = LLMProvider(
    api_key_env="OPEN_ROUTER_API_KEY",
    base_url="https://openrouter.ai",
    model="z-ai/glm-5.3-flash",
    models=["z-ai/glm-5.3-flash"],
    batch_model="z-ai/glm-5.3-flash",
    trust_env= False,
    provider_options={
        "sort": "price",
        "allow_fallbacks": True,
    }
)

LLMProvider.LOCAL = LLMProvider(
    api_key_env="LLAMA_CPP",
    base_url="http://127.0.0.1:8090/v1",
    model="Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_NL",
    max_tokens=36864,
)

LLMProvider.vectorengine = LLMProvider(
    api_key_env="vectorengine",
    base_url="https://api.vectorengine.cn/v1",
    # model="gpt-5-nano-2025-08-07",
    model="gemini-3.1-flash-lite",
    reasoning_effort="medium",
)

LLMProvider.AG_TOOL = LLMProvider(
    api_key_env="AG_TOOL_KEY",
    base_url="http://127.0.0.1:8045/v1",
    # model="gpt-5-nano-2025-08-07",
    model="gemini-3.8-flash-medium"
)