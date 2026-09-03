"""OpenRouter LLM Provider Adapter for Phase P10.

Implements ports.llm.LLMProvider using OpenRouter's OpenAI-compatible REST API endpoint
(https://openrouter.ai/api/v1/chat/completions).
"""

import json
import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from taskflow.config.settings import settings
from taskflow.domain.errors import ProviderError, SchemaError, TransientError
from taskflow.domain.models import LLMCall
from taskflow.ports.llm import LLMResponse, ProviderCapabilities
from taskflow.services.cost.service import calculate_cost

OPENROUTER_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider:
    name: str = "openrouter"
    capabilities: ProviderCapabilities = ProviderCapabilities(
        supports_json_schema=True,
        supports_tools=True,
        max_context=200000,
    )

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key if api_key is not None else settings().openrouter_api_key

    async def health(self) -> bool:
        """Check whether the OpenRouter API key is configured."""
        return bool(self._api_key and len(self._api_key) > 0)

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int = 1200,
        temperature: float = 0.7,
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        if not self._api_key:
            raise ProviderError("OpenRouter API key not configured (OPENROUTER_API_KEY missing)")

        system_instruction = system
        if schema:
            json_schema_str = json.dumps(schema.model_json_schema(), indent=2)
            system_instruction = f"{system}\n\nRespond with a valid JSON object matching this schema:\n{json_schema_str}"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "HTTP-Referer": "https://taskflow.ai",
            "X-Title": "TaskFlow Support Engine",
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if schema:
            payload["response_format"] = {"type": "json_object"}

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(OPENROUTER_COMPLETIONS_URL, json=payload, headers=headers)
                if resp.status_code in (429, 502, 503, 504):
                    raise TransientError(f"OpenRouter transient HTTP error ({resp.status_code})")
                if resp.status_code != 200:
                    raise ProviderError(f"OpenRouter error ({resp.status_code}): {resp.text}")

                data = resp.json()

        except (httpx.TimeoutException, httpx.NetworkError) as err:
            raise TransientError(f"OpenRouter network timeout/error: {err}") from err
        except (TransientError, ProviderError):
            raise
        except Exception as err:
            raise ProviderError(f"OpenRouter completion failed: {err}") from err

        latency_ms = int((time.perf_counter() - start) * 1000)

        try:
            choice = data["choices"][0]
            raw_text = choice["message"]["content"] or ""
        except (KeyError, IndexError) as err:
            raise ProviderError(f"Malformed OpenRouter response payload: {data}") from err

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", len(user) // 4)
        completion_tokens = usage.get("completion_tokens", len(raw_text) // 4)

        cost_usd = calculate_cost(
            provider="openrouter",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

        call_record = LLMCall(
            purpose="completion",
            provider="openrouter",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )

        if schema:
            try:
                schema.model_validate_json(raw_text)
            except ValidationError as err:
                raise SchemaError(f"OpenRouter response failed schema validation: {err}") from err

        return LLMResponse(text=raw_text, call=call_record)
