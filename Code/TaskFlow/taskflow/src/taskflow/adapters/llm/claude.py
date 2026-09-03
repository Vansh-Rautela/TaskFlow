"""Anthropic Claude LLM Provider Adapter.

Implements ports.llm.LLMProvider using anthropic.AsyncAnthropic.
Translates Anthropic SDK exceptions into domain.errors types.
"""

import time

import anthropic
from anthropic.types import TextBlock
from pydantic import BaseModel

from taskflow.config.settings import settings
from taskflow.domain.errors import ProviderError, SchemaError, TransientError
from taskflow.domain.models import LLMCall
from taskflow.ports.llm import LLMResponse, ProviderCapabilities
from taskflow.services.cost.service import calculate_cost


class ClaudeProvider:
    name: str = "claude"
    capabilities: ProviderCapabilities = ProviderCapabilities(
        supports_json_schema=True,
        supports_tools=True,
        max_context=200000,
    )

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or settings().anthropic_api_key
        self._client = anthropic.AsyncAnthropic(api_key=key) if key else None

    async def health(self) -> bool:
        """Check whether the API key is configured and reachable."""
        return self._client is not None and len(settings().anthropic_api_key) > 0

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
        if not self._client:
            raise ProviderError("Claude API key not configured (ANTHROPIC_API_KEY missing)")

        start_time = time.perf_counter_ns()

        try:
            if schema is not None:
                response = await self._client.messages.parse(
                    model=model,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    messages=[{"role": "user", "content": user}],
                    output_format=schema,
                )
                if response.stop_reason in ("refusal", "max_tokens"):
                    raise SchemaError(
                        f"Claude schema validation failed: stop_reason={response.stop_reason}"
                    )

                parsed = response.parsed_output
                text_content = (
                    parsed.model_dump_json() if isinstance(parsed, BaseModel) else str(parsed)
                )
                prompt_tokens = response.usage.input_tokens
                completion_tokens = response.usage.output_tokens
            else:
                msg_resp = await self._client.messages.create(
                    model=model,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    messages=[{"role": "user", "content": user}],
                )
                text_content = "".join(
                    block.text for block in msg_resp.content if isinstance(block, TextBlock)
                )
                prompt_tokens = msg_resp.usage.input_tokens
                completion_tokens = msg_resp.usage.output_tokens

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

            return LLMResponse(text=text_content, call=call)

        except anthropic.APITimeoutError as err:
            raise TransientError("Claude API request timed out") from err
        except anthropic.APIError as err:
            raise ProviderError(f"Claude API error: {err}") from err
        except Exception as err:
            if isinstance(err, (ProviderError, SchemaError, TransientError)):
                raise
            raise ProviderError(f"Unexpected Claude provider error: {err}") from err
