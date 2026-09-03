"""Human review console service.

Every action here is an accountable state transition: it uses optimistic locking so
two operators cannot both action the same item, and it writes an audit record naming
the operator and their reason.
"""

import uuid
from datetime import UTC, datetime

import structlog

from taskflow.domain.enums import Channel, ReviewState
from taskflow.domain.models import EditRecord, OutboundMessage, ReviewItem
from taskflow.ports.repositories import AuditRepository, OutboxRepository, ReviewRepository

logger = structlog.get_logger()

_FALLBACK_RECIPIENT_REASON = "review item has no sender_email; cannot address a reply"


async def list_pending_reviews(repo: ReviewRepository) -> list[ReviewItem]:
    """All pending review items, soonest SLA deadline first."""
    return await repo.pending()


async def get_review_detail(repo: ReviewRepository, review_id: str) -> ReviewItem | None:
    return await repo.get(review_id)


async def _audit(
    audit_repo: AuditRepository | None,
    item: ReviewItem,
    action: str,
    operator: str,
    reason: str | None,
) -> None:
    if audit_repo is None:
        return
    await audit_repo.record(
        tenant_id=item.tenant_id,
        actor=operator,
        action=action,
        entity_type="review",
        entity_id=item.review_id,
        payload={"reason": reason} if reason else {},
    )


def _build_reply(item: ReviewItem, body_text: str, suffix: str) -> OutboundMessage | None:
    """Build the outbound reply for an approved review, or None if unaddressable.

    Returning None rather than inventing a placeholder address matters: a reply sent
    to `support-fallback@taskflow.dev` is a reply the customer never receives, and
    the outbox would have recorded it as delivered.
    """
    if not item.sender_email:
        logger.error(
            "review_reply_not_enqueued", review_id=item.review_id, reason=_FALLBACK_RECIPIENT_REASON
        )
        return None

    return OutboundMessage(
        outbound_id=f"out-{uuid.uuid4().hex[:12]}",
        conversation_id=item.conversation_id,
        tenant_id=item.tenant_id,
        channel=Channel.EMAIL,
        recipient=item.sender_email,
        subject="Re: Support Request",
        body_text=body_text,
        # Scoped to the review and the action, so approving once cannot enqueue twice.
        idempotency_key=f"review:{item.review_id}:{suffix}",
    )


async def approve_review(
    review_repo: ReviewRepository,
    outbox_repo: OutboxRepository,
    review_id: str,
    operator: str = "operator",
    reason: str = "approved by operator",
    audit_repo: AuditRepository | None = None,
) -> bool:
    """Approve a pending review and enqueue the drafted reply for delivery."""
    item = await review_repo.get(review_id)
    if not item or item.state != ReviewState.PENDING:
        return False

    if not await review_repo.transition(review_id, ReviewState.APPROVED, ReviewState.PENDING):
        return False

    await _audit(audit_repo, item, "review.approved", operator, reason)

    if item.draft and item.draft.response_text:
        outbound = _build_reply(item, item.draft.response_text, "approved")
        if outbound and not await outbox_repo.enqueue(outbound):
            logger.info("review_reply_already_enqueued", review_id=review_id)

    return True


async def edit_and_approve_review(
    review_repo: ReviewRepository,
    outbox_repo: OutboxRepository,
    review_id: str,
    edited_text: str,
    operator: str = "operator",
    reason: str = "human operator edit",
    audit_repo: AuditRepository | None = None,
) -> bool:
    """Record the operator's edit, mark the item EDITED, and enqueue the edited reply."""
    item = await review_repo.get(review_id)
    if not item or item.state != ReviewState.PENDING:
        return False

    if not await review_repo.transition(review_id, ReviewState.EDITED, ReviewState.PENDING):
        return False

    original_text = item.draft.response_text if item.draft else ""
    await review_repo.record_edit(
        EditRecord(
            review_id=review_id,
            original_draft=original_text,
            edited_draft=edited_text,
            editor=operator,
            edited_at=datetime.now(UTC),
            reason=reason,
            char_diff=len(edited_text) - len(original_text),
        )
    )
    await _audit(audit_repo, item, "review.edited", operator, reason)

    outbound = _build_reply(item, edited_text, "edited")
    if outbound and not await outbox_repo.enqueue(outbound):
        logger.info("review_reply_already_enqueued", review_id=review_id)

    return True


async def reject_review(
    review_repo: ReviewRepository,
    review_id: str,
    operator: str = "operator",
    reason: str = "",
    audit_repo: AuditRepository | None = None,
) -> bool:
    """Reject a pending review. No reply is sent."""
    item = await review_repo.get(review_id)
    if not item or item.state != ReviewState.PENDING:
        return False

    if not await review_repo.transition(review_id, ReviewState.REJECTED, ReviewState.PENDING):
        return False

    await _audit(audit_repo, item, "review.rejected", operator, reason)
    return True


async def escalate_review(
    review_repo: ReviewRepository,
    review_id: str,
    operator: str = "operator",
    reason: str = "",
    audit_repo: AuditRepository | None = None,
) -> bool:
    """Escalate a pending review to tier-2 support. No reply is sent."""
    item = await review_repo.get(review_id)
    if not item or item.state != ReviewState.PENDING:
        return False

    if not await review_repo.transition(review_id, ReviewState.ESCALATED, ReviewState.PENDING):
        return False

    await _audit(audit_repo, item, "review.escalated", operator, reason)
    return True
