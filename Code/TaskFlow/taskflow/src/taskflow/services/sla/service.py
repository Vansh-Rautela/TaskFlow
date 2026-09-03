"""SLA breach sweeper.

`ReviewRepository.due()` has always existed; nothing called it, so a review could sit
past its deadline indefinitely with no one told. This is the caller.

A breach escalates the item (PENDING -> ESCALATED) and alerts. Escalation is done
through the same optimistic-locking transition operators use, so a sweep racing an
operator's approval loses cleanly rather than double-handling the item.
"""

from datetime import UTC, datetime

import structlog

from taskflow.domain.enums import ReviewState
from taskflow.domain.models import ReviewItem
from taskflow.ports.repositories import ReviewRepository
from taskflow.services.alert.service import dispatch_sla_breach_alert

logger = structlog.get_logger()


async def sweep_sla_breaches(
    review_repo: ReviewRepository, now: datetime | None = None
) -> list[ReviewItem]:
    """Escalate and alert on every pending review past its SLA deadline.

    Returns the items actually escalated by this sweep (not those another actor won).
    """
    now = now or datetime.now(UTC)
    breached = await review_repo.due(now)
    if not breached:
        return []

    logger.info("sla_sweep_found_breaches", count=len(breached))
    escalated: list[ReviewItem] = []

    for item in breached:
        won = await review_repo.transition(
            item.review_id, ReviewState.ESCALATED, ReviewState.PENDING
        )
        if not won:
            logger.info("sla_breach_already_actioned", review_id=item.review_id)
            continue

        overdue_minutes = int((now - item.sla_deadline).total_seconds() // 60)
        logger.warning(
            "sla_breached",
            review_id=item.review_id,
            deadline=item.sla_deadline.isoformat(),
            overdue_minutes=overdue_minutes,
        )

        try:
            await dispatch_sla_breach_alert(item, overdue_minutes)
        except Exception as err:
            # The escalation is already durable; a failed alert must not undo it.
            logger.error("sla_breach_alert_failed", review_id=item.review_id, error=str(err))

        escalated.append(item)

    return escalated
