"""Outbox delivery worker.

Claims QUEUED messages, hands each to a channel, and records what actually happened.

The single rule this module exists to enforce: a message is marked SENT if and only
if the channel returned a delivery receipt. Anything else is FAILED (retryable, the
row returns to QUEUED) or DEAD (terminal, retries exhausted or the failure is not
the kind that retrying fixes).
"""

import asyncio

import structlog

from taskflow.config.settings import app_config
from taskflow.domain.enums import OutboxState
from taskflow.domain.errors import ConfigError
from taskflow.domain.models import OutboundMessage
from taskflow.ports.channel import OutboundChannel
from taskflow.ports.repositories import OutboxRepository

logger = structlog.get_logger()


def _worker_config() -> dict:
    return dict(app_config().get("worker", {}))


async def process_outbox_batch(
    outbox_repo: OutboxRepository,
    channel: OutboundChannel,
    limit: int = 10,
) -> dict[str, int]:
    """Claim and deliver one batch.

    Returns counts per outcome: ``{"sent": n, "retried": n, "dead": n}``.
    """
    max_attempts = int(_worker_config().get("max_attempts", 3))
    claimed = await outbox_repo.claim(limit=limit)
    if not claimed:
        return {"sent": 0, "retried": 0, "dead": 0}

    logger.info("worker_claimed_batch", count=len(claimed))
    outcome = {"sent": 0, "retried": 0, "dead": 0}

    for msg in claimed:
        result = await _deliver_one(outbox_repo, channel, msg, max_attempts)
        outcome[result] += 1

    return outcome


async def _deliver_one(
    outbox_repo: OutboxRepository,
    channel: OutboundChannel,
    msg: OutboundMessage,
    max_attempts: int,
) -> str:
    """Deliver a single claimed message and record the true outcome."""
    try:
        receipt = await channel.send(msg)
    except ConfigError as err:
        # Misconfiguration and undeliverable addresses do not improve with retries.
        logger.error(
            "worker_delivery_permanently_failed", outbound_id=msg.outbound_id, error=str(err)
        )
        await outbox_repo.mark(msg.outbound_id, state=OutboxState.DEAD, error=str(err))
        return "dead"
    except Exception as err:
        attempts_made = msg.retry_count + 1
        if attempts_made >= max_attempts:
            logger.error(
                "worker_delivery_retries_exhausted",
                outbound_id=msg.outbound_id,
                attempts=attempts_made,
                error=str(err),
            )
            await outbox_repo.mark(
                msg.outbound_id,
                state=OutboxState.DEAD,
                error=f"retries exhausted after {attempts_made} attempts: {err}",
            )
            return "dead"

        logger.warning(
            "worker_delivery_failed_will_retry",
            outbound_id=msg.outbound_id,
            attempt=attempts_made,
            max_attempts=max_attempts,
            error=str(err),
        )
        await outbox_repo.mark(
            msg.outbound_id, state=OutboxState.FAILED, error=str(err), increment_retry=True
        )
        return "retried"

    logger.info(
        "worker_message_sent",
        outbound_id=msg.outbound_id,
        provider_message_id=receipt.provider_message_id,
    )
    await outbox_repo.mark(msg.outbound_id, state=OutboxState.SENT)
    return "sent"


async def run_worker_loop(
    outbox_repo: OutboxRepository,
    channel: OutboundChannel,
    poll_interval_s: float | None = None,
    run_once: bool = False,
) -> dict[str, int]:
    """Poll the outbox until cancelled (or once, for tests)."""
    cfg = _worker_config()
    interval = (
        poll_interval_s if poll_interval_s is not None else float(cfg.get("poll_interval_s", 1.0))
    )
    lease_minutes = int(cfg.get("claim_lease_minutes", 5))

    totals = {"sent": 0, "retried": 0, "dead": 0}
    logger.info("worker_loop_starting", poll_interval_s=interval, run_once=run_once)

    while True:
        # Reclaim rows a crashed worker left in SENDING past its lease.
        reclaimed = await outbox_repo.requeue_expired(lease_minutes=lease_minutes)
        if reclaimed:
            logger.warning("worker_reclaimed_stalled_messages", count=reclaimed)

        batch = await process_outbox_batch(outbox_repo, channel)
        for key, value in batch.items():
            totals[key] += value

        if run_once:
            break

        if sum(batch.values()) == 0:
            await asyncio.sleep(interval)

    logger.info("worker_loop_finished", **totals)
    return totals
