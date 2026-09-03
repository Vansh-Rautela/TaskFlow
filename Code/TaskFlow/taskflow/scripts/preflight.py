#!/usr/bin/env python3
"""Preflight: is this installation actually able to run?

Every check below inspects real state — a live connection, a real row count, a real
provider handshake. Nothing is asserted or simulated. Exits 0 only when every
REQUIRED check passes; OPTIONAL checks report but do not fail the run.

    uv run python scripts/preflight.py
"""

import asyncio
import sys
from dataclasses import dataclass

from taskflow.adapters.llm.claude import ClaudeProvider
from taskflow.adapters.llm.ollama import OllamaProvider
from taskflow.adapters.llm.openrouter import OpenRouterProvider
from taskflow.adapters.vector.qdrant_store import QdrantVectorStore
from taskflow.bootstrap import runtime
from taskflow.config.settings import provider_priority, settings, thresholds_config

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _render(c: Check) -> str:
    if c.ok:
        return f"  {GREEN}PASS{RESET}  {c.name:<34} {c.detail}"
    colour, label = (RED, "FAIL") if c.required else (YELLOW, "SKIP")
    return f"  {colour}{label}{RESET}  {c.name:<34} {c.detail}"


async def run_checks() -> list[Check]:
    checks: list[Check] = []
    cfg = settings()

    # --- configuration ---
    weights = thresholds_config()["weights"]
    total = sum(weights.values())
    checks.append(
        Check(
            "scoring weights sum to 1.0",
            abs(total - 1.0) < 1e-9,
            f"sum={total:.4f} over {len(weights)} signals",
        )
    )

    thresholds = thresholds_config()["thresholds"]
    checks.append(
        Check(
            "per-intent thresholds configured",
            "general" in thresholds,
            f"{len(thresholds)} intents, general={thresholds.get('general')}",
        )
    )

    checks.append(
        Check(
            "API token set",
            bool(cfg.taskflow_api_token) and cfg.taskflow_api_token != "change-me",
            "TASKFLOW_API_TOKEN is set"
            if cfg.taskflow_api_token not in ("", "change-me")
            else "TASKFLOW_API_TOKEN unset — protected endpoints will refuse to serve",
        )
    )

    # --- database: migrate, connect, count ---
    try:
        async with runtime(with_vector_store=False) as rt:
            traces = await rt.trace_repo.recent(limit=1000)
            pending = await rt.review_repo.pending()
            checks.append(
                Check(
                    "database migrated and readable",
                    True,
                    f"{len(traces)} traces, {len(pending)} pending reviews",
                )
            )
            checks.append(
                Check(
                    "SMTP delivery configured",
                    await rt.email_channel.health(),
                    "GMAIL_USER + GMAIL_APP_PASSWORD present"
                    if await rt.email_channel.health()
                    else "unset — auto-sent replies would be marked DEAD",
                    required=False,
                )
            )
    except Exception as err:
        checks.append(
            Check("database migrated and readable", False, f"{type(err).__name__}: {err}")
        )

    # --- retrieval: is the index actually populated? ---
    try:
        store = QdrantVectorStore()
        result = store.search(query="refund policy", tenant_id=cfg.taskflow_tenant_id, limit=5)
        checks.append(
            Check(
                "knowledge base indexed",
                len(result.chunks) > 0,
                f"{len(result.chunks)} chunks for a probe query"
                if result.chunks
                else "no chunks — run `make ingest`",
            )
        )
    except Exception as err:
        checks.append(Check("knowledge base indexed", False, f"{type(err).__name__}: {err}"))

    # --- providers: real handshakes ---
    providers = {
        "openrouter": OpenRouterProvider(),
        "claude": ClaudeProvider(),
        "ollama": OllamaProvider(),
    }
    priority = provider_priority()
    reachable = []
    for name in priority:
        healthy = await providers[name].health()
        if healthy:
            reachable.append(name)
        checks.append(
            Check(
                f"provider: {name}",
                healthy,
                "reachable" if healthy else "unavailable",
                required=False,
            )
        )

    checks.append(
        Check(
            "at least one LLM provider",
            bool(reachable),
            f"reachable: {', '.join(reachable)}"
            if reachable
            else f"none of {priority} — the pipeline will use keyword and template fallbacks",
            required=False,
        )
    )

    return checks


async def main() -> int:
    print("TaskFlow preflight")
    print("=" * 72)
    checks = await run_checks()
    for c in checks:
        print(_render(c))
    print("=" * 72)

    failed = [c for c in checks if not c.ok and c.required]
    skipped = [c for c in checks if not c.ok and not c.required]

    print(
        f"{len(checks) - len(failed) - len(skipped)} passed, "
        f"{len(failed)} failed, {len(skipped)} unavailable (optional)"
    )
    if failed:
        print(f"\n{RED}Preflight failed.{RESET} Fix the items marked FAIL above.")
        return 1

    print(f"\n{GREEN}Preflight passed.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
