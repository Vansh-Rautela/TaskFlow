"""Human Review Console Service for Phase P8.

Manages review item listing, optimistic concurrency state transitions, edit diff tracking,
and outbox message enqueueing for approved replies.
"""

import uuid
from datetime import UTC, datetime

from taskflow.domain.enums import Channel, OutboxState, ReviewState
from taskflow.domain.models import (
    EditRecord,
    OutboundMessage,
    ReviewItem,
)
from taskflow.ports.repositories import OutboxRepository, ReviewRepository


async def list_pending_reviews(repo: ReviewRepository) -> list[ReviewItem]:
    """Retrieve all pending review queue items."""
    return await repo.pending()


async def get_review_detail(repo: ReviewRepository, review_id: str) -> ReviewItem | None:
    """Retrieve detailed review item by ID."""
    return await repo.get(review_id)


async def approve_review(
    review_repo: ReviewRepository,
    outbox_repo: OutboxRepository,
    review_id: str,
    operator: str = "operator",
    reason: str = "approved by operator",
) -> bool:
    """Approve a pending review item and enqueue outbound message for sending."""
    item = await review_repo.get(review_id)
    if not item or item.state != ReviewState.PENDING:
        return False

    success = await review_repo.transition(
        review_id=review_id, to=ReviewState.APPROVED, expected=ReviewState.PENDING
    )
    if not success:
        return False

    if item.draft and item.draft.response_text:
        recipient = item.sender_email or "support-fallback@taskflow.dev"
        outbound = OutboundMessage(
            outbound_id=uuid.uuid4().hex[:12],
            conversation_id=item.conversation_id,
            tenant_id=item.tenant_id,
            channel=Channel.EMAIL,
            recipient=recipient,
            subject="Re: Support Request",
            body_text=item.draft.response_text,
            reply_headers={},
            state=OutboxState.QUEUED,
            idempotency_key=f"outbound:{review_id}:approved",
            retry_count=0,
        )
        await outbox_repo.enqueue(outbound)

    return True


async def edit_and_approve_review(
    review_repo: ReviewRepository,
    outbox_repo: OutboxRepository,
    review_id: str,
    edited_text: str,
    operator: str = "operator",
    reason: str = "human operator edit",
) -> bool:
    """Record operator edit diff, transition review item to EDITED, and enqueue outbound message."""
    item = await review_repo.get(review_id)
    if not item or item.state != ReviewState.PENDING:
        return False

    original_text = item.draft.response_text if item.draft else ""
    char_diff = len(edited_text) - len(original_text)

    success = await review_repo.transition(
        review_id=review_id, to=ReviewState.EDITED, expected=ReviewState.PENDING
    )
    if not success:
        return False

    edit_rec = EditRecord(
        review_id=review_id,
        original_draft=original_text,
        edited_draft=edited_text,
        editor=operator,
        edited_at=datetime.now(UTC),
        reason=reason,
        char_diff=char_diff,
    )
    await review_repo.record_edit(edit_rec)

    recipient = item.sender_email or "support-fallback@taskflow.dev"
    outbound = OutboundMessage(
        outbound_id=uuid.uuid4().hex[:12],
        conversation_id=item.conversation_id,
        tenant_id=item.tenant_id,
        channel=Channel.EMAIL,
        recipient=recipient,
        subject="Re: Support Request",
        body_text=edited_text,
        reply_headers={},
        state=OutboxState.QUEUED,
        idempotency_key=f"outbound:{review_id}:edited",
        retry_count=0,
    )
    await outbox_repo.enqueue(outbound)

    return True


async def reject_review(
    review_repo: ReviewRepository,
    review_id: str,
    operator: str = "operator",
    reason: str = "",
) -> bool:
    """Reject a pending review item (no outbound message sent)."""
    return await review_repo.transition(
        review_id=review_id, to=ReviewState.REJECTED, expected=ReviewState.PENDING
    )


async def escalate_review(
    review_repo: ReviewRepository,
    review_id: str,
    operator: str = "operator",
    reason: str = "",
) -> bool:
    """Escalate a pending review item to tier-2 support."""
    return await review_repo.transition(
        review_id=review_id, to=ReviewState.ESCALATED, expected=ReviewState.PENDING
    )
