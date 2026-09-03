#!/usr/bin/env python3
"""SLA breach sweeper.

Escalates and alerts on review items that have passed their SLA deadline. The
interval comes from `pollers.sla_tick_s` in config/settings.yaml.

    uv run python scripts/start_sla_sweeper.py
"""

import asyncio
import sys

from taskflow.bootstrap import runtime
from taskflow.config.settings import app_config
from taskflow.services.sla.service import sweep_sla_breaches


async def main() -> int:
    tick = float(app_config().get("pollers", {}).get("sla_tick_s", 30))

    async with runtime(with_vector_store=False) as rt:
        print(f"TaskFlow SLA sweeper — checking every {tick:.0f}s. Ctrl-C to stop.")
        try:
            while True:
                escalated = await sweep_sla_breaches(rt.review_repo)
                for item in escalated:
                    print(
                        f"  escalated {item.review_id} (deadline {item.sla_deadline.isoformat()})"
                    )
                await asyncio.sleep(tick)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nSweeper stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
