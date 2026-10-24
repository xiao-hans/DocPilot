"""LLM 提取器（预留实现，默认不启用）。

通过 OpenAI 兼容接口（DeepSeek / 千问 / OpenAI 等）调用 chat/completions。
构造函数允许注入 httpx.Client，便于测试时 mock HTTP 请求。
"""

from __future__ import annotations

import json
import re

import httpx

from app.platform.config import (
    AI_API_KEY,
    AI_BASE_URL,
    AI_MAX_RETRIES,
    AI_MODEL,
    AI_TEMPERATURE,
    AI_TIMEOUT,
)
from app.models.schema import FieldDefinition

from .base import ExtractionError, ExtractorBase, ExtractResult, apply_type_coercion
from .prompt import build_extraction_prompt


class LLMExtractor(ExtractorBase):
    """基于大模型的提取器（需配置 AI_API_KEY）。"""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def extract(
        self,
        text: str,
        fields: list[FieldDefinition],
        metadata: dict | None = None,
    ) -> ExtractResult:
        if not AI_API_KEY:
            raise ExtractionError(
                "未配置 AI_API_KEY，无法使用 LLM 提取器，请在 .env 中配置后再启用。"
            )

        system_prompt, user_prompt = build_extraction_prompt(fields, text, metadata)
        last_msg = ""
        for attempt in range(AI_MAX_RETRIES + 1):
            try:
                last_msg = self._complete(system_prompt, user_prompt)
                parsed = _extract_json(last_msg)
                if not isinstance(parsed, dict):
                    raise ExtractionError("LLM 返回的 JSON 不是对象类型")
            except ExtractionError:
                if attempt < AI_MAX_RETRIES:
                    continue
                raise ExtractionError(
                    f"LLM 返回无法解析为 JSON 的响应（已重试 {AI_MAX_RETRIES} 次）："
                    f"{last_msg[:500]}"
                ) from None

            raw_values: dict[str, object] = {}
            for f in fields:
                raw_values[f.name] = parsed.get(f.name, "")
            values, warnings = apply_type_coercion(raw_values, fields)
            return ExtractResult(values=values, warnings=warnings)

        raise ExtractionError("LLM 提取失败：超出最大重试次数。")

    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        client = self._client
        owned = client is None
        if owned:
            client = httpx.Client(timeout=AI_TIMEOUT)
        try:
            resp = client.post(
                f"{AI_BASE_URL.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {AI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": AI_MODEL,
                    "temperature": AI_TEMPERATURE,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            # ValueError 覆盖 resp.json() 的 JSONDecodeError；KeyError/IndexError
            # 覆盖非标准响应结构（choices 为空 / message 缺 content）
            raise ExtractionError(f"调用 LLM 接口失败或响应结构异常: {e}") from e
        finally:
            if owned:
                client.close()


def _extract_json(text: str) -> dict:
    """从 LLM 输出中解析 JSON 对象，容忍 markdown 代码围栏与前后缀文本。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ExtractionError("响应中未找到 JSON 对象")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ExtractionError(f"JSON 解析失败: {e}") from e
