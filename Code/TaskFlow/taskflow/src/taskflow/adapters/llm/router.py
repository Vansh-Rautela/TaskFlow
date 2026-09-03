"""Provider Router implementation for Phase P4 & P10.

Routes structured LLM requests through prioritized providers with automatic failover,
schema validation, cost tracking, and offline mode support.
"""

import structlog
from pydantic import BaseModel, ValidationError

from taskflow.adapters.llm.claude import ClaudeProvider
from taskflow.adapters.llm.ollama import OllamaProvider
from taskflow.adapters.llm.openrouter import OpenRouterProvider
from taskflow.config.settings import model_for, provider_priority, settings
from taskflow.domain.errors import (
    AllProvidersFailed,
    ConfigError,
    ProviderError,
    SchemaError,
    TransientError,
)
from taskflow.domain.models import LLMCall
from taskflow.ports.llm import LLMProvider, LLMRouter

logger = structlog.get_logger()


class ProviderRouter(LLMRouter):
    """Priority-based failover LLM router implementing ports.llm.LLMRouter."""

    def __init__(self, providers: dict[str, LLMProvider] | None = None) -> None:
        if providers is not None:
            self._providers = providers
        else:
            self._providers = {
                "openrouter": OpenRouterProvider(),
                "claude": ClaudeProvider(),
                "ollama": OllamaProvider(),
            }

    async def complete_structured[T: BaseModel](
        self, *, purpose: str, system: str, user: str, schema: type[T]
    ) -> tuple[T, LLMCall]:
        """Attempt completion using priority order until one succeeds or all fail."""
        priority = provider_priority()
        mode = settings().taskflow_llm_mode

        logger.info(
            "router_structured_complete_start", purpose=purpose, mode=mode, priority=priority
        )

        attempts = 0
        last_error: Exception | None = None

        for provider_name in priority:
            provider = self._providers.get(provider_name)
            if not provider:
                logger.warning("provider_not_found", provider=provider_name)
                continue

            # A provider that declares no model for this purpose is skipped, not fatal:
            # a configuration gap in one provider must not disable the whole chain.
            try:
                model_name = model_for(provider_name, purpose)
            except ConfigError as err:
                logger.warning(
                    "provider_has_no_model_for_purpose",
                    provider=provider_name,
                    purpose=purpose,
                )
                last_error = err
                continue

            attempts += 1

            try:
                response = await provider.complete(
                    system=system,
                    user=user,
                    model=model_name,
                    schema=schema,
                )

                call_record = response.call.model_copy(
                    update={
                        "provider": provider_name,
                        "attempts": attempts,
                        "failed_over": attempts > 1,
                    }
                )
                logger.info(
                    "router_complete_success",
                    provider=provider_name,
                    model=model_name,
                    cost_usd=call_record.cost_usd,
                    attempts=attempts,
                )

                if schema:
                    try:
                        parsed_object = schema.model_validate_json(response.text)
                        return parsed_object, call_record
                    except ValidationError as err:
                        raise SchemaError(
                            f"Response from {provider_name} failed schema validation: {err}"
                        ) from err

                raise SchemaError(f"Schema required for complete_structured call ({purpose})")

            except (TransientError, ProviderError, SchemaError) as err:
                logger.warning(
                    "provider_attempt_failed",
                    provider=provider_name,
                    attempt=attempts,
                    error=str(err),
                )
                last_error = err
                continue
            except Exception as err:
                # An adapter raised something it should have translated. Failing over is
                # still the right move, but the untranslated error is itself a defect,
                # so it is logged at error level rather than absorbed quietly.
                logger.error(
                    "provider_raised_untranslated_error",
                    provider=provider_name,
                    attempt=attempts,
                    error_type=type(err).__name__,
                    error=str(err),
                )
                last_error = err
                continue

        logger.error(
            "all_providers_failed",
            attempts=attempts,
            mode=mode,
            priority=priority,
            last_error=str(last_error),
        )
        raise AllProvidersFailed(
            f"All providers failed for mode '{mode}'. Last error: {last_error}"
        )
