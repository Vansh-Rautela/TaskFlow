"""Validator runner.

Executes all independent deterministic and LLM-based validators concurrently
using asyncio.gather, ensuring we fail-closed on any timeouts or unhandled errors.
"""

import asyncio
from collections.abc import Coroutine, Sequence
from typing import Any

from taskflow.domain.models import DraftOutput, RetrievalResult, ValidatorResult
from taskflow.ports.pii import PIIDetector
from taskflow.services.validate.citations import validate_citations
from taskflow.services.validate.grounding import validate_grounding
from taskflow.services.validate.pii_leak import validate_pii


async def run_validators(
    draft: DraftOutput,
    retrieval: RetrievalResult | None,
    timeout_s: float = 15.0,
    pii_detector: PIIDetector | None = None,
) -> Sequence[ValidatorResult]:
    """Run all validators concurrently."""

    # We defensively wrap each validator to ensure it returns a failed ValidatorResult
    # if it crashes or times out, implementing the fail-closed doctrine (Gate G6).
    async def safe_run(name: str, coro: Coroutine[Any, Any, ValidatorResult]) -> ValidatorResult:
        try:
            return await asyncio.wait_for(coro, timeout=timeout_s)
        except TimeoutError:
            return ValidatorResult(
                validator_name=name,
                passed=False,
                score=0.0,
                reason="Validator timed out",
                blocking=True,
                errored=True,
            )
        except Exception as e:
            return ValidatorResult(
                validator_name=name,
                passed=False,
                score=0.0,
                reason=f"Validator crashed: {e}",
                blocking=True,
                errored=True,
            )

    tasks = [
        safe_run("pii_leak", validate_pii(draft, pii_detector)),
        safe_run("citations", validate_citations(draft, retrieval)),
        safe_run("grounding", validate_grounding(draft, retrieval)),
    ]

    return await asyncio.gather(*tasks)
