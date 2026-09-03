"""Ollama local LLM Provider Adapter.

Implements ports.llm.LLMProvider using ollama.AsyncClient.
Provides 100% offline fallback using local models.
"""

import time

import httpx
import ollama
from pydantic import BaseModel

from taskflow.config.settings import settings
from taskflow.domain.errors import ProviderError, SchemaError, TransientError
from taskflow.domain.models import LLMCall
from taskflow.ports.llm import LLMResponse, ProviderCapabilities
from taskflow.services.cost.service import calculate_cost


class OllamaProvider:
    name: str = "ollama"
    capabilities: ProviderCapabilities = ProviderCapabilities(
        supports_json_schema=True,
        supports_tools=False,
        max_context=32000,
    )

    def __init__(self, base_url: str | None = None) -> None:
        url = base_url or settings().ollama_base_url
        self._client = ollama.AsyncClient(host=url)

    async def health(self) -> bool:
        """Check whether local Ollama daemon is active and responding."""
        try:
            res = await self._client.list()
            return res is not None
        except Exception:
            return False

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int = 1200,
        temperature: float = 0.2,
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        start_time = time.perf_counter_ns()

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        format_arg = schema.model_json_schema() if schema is not None else None

        try:
            try:
                response = await self._client.chat(
                    model=model,
                    messages=messages,
                    format=format_arg,
                    options={
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                )
            except ollama.ResponseError:
                if format_arg and isinstance(format_arg, dict):
                    # Fallback to standard format="json" if model GBNF grammar generation fails
                    response = await self._client.chat(
                        model=model,
                        messages=messages,
                        format="json",
                        options={
                            "temperature": temperature,
                            "num_predict": max_tokens,
                        },
                    )
                else:
                    raise

            content = response.message.content or ""
            if not content.strip():
                raise SchemaError("Ollama produced empty response content")

            prompt_tokens = getattr(response, "prompt_eval_count", 0) or 0
            completion_tokens = getattr(response, "eval_count", 0) or 0
            elapsed_ms = (time.perf_counter_ns() - start_time) // 1_000_000

            cost_usd = calculate_cost(self.name, prompt_tokens, completion_tokens)

            call = LLMCall(
                purpose="complete",
                provider=self.name,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
                latency_ms=elapsed_ms,
            )

            return LLMResponse(text=content, call=call)

        except (httpx.ConnectError, httpx.ConnectTimeout) as err:
            raise ProviderError(
                "Ollama local service unavailable. Please run `ollama serve` or pull required models."
            ) from err
        except ollama.ResponseError as err:
            raise ProviderError(f"Ollama provider error: {err}") from err
        except Exception as err:
            if isinstance(err, (ProviderError, SchemaError, TransientError)):
                raise
            raise ProviderError(f"Unexpected Ollama provider error: {err}") from err
