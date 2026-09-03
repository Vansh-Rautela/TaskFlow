"""Thin client for `POST /messages` on the already-running API.

Both Gmail inbound adapters (poll and push) hand every message to the API instead
of building their own `Deps` and calling the pipeline in-process. See
`gmail_poller.py`'s module docstring for why: only one process may hold the
embedded Qdrant store's exclusive lock, and the API already holds it.
"""

from typing import Any

import httpx
import structlog

from taskflow.domain.errors import ProviderError, TransientError

logger = structlog.get_logger()


async def post_inbound_message(
    *,
    api_base_url: str,
    api_token: str,
    message_id: str,
    tenant_id: str,
    channel: str,
    sender: str,
    subject: str | None,
    body_text: str,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """POST one inbound message to `{api_base_url}/messages`.

    Raises `TransientError` on a network failure or a 5xx (the caller should retry
    on its own schedule) and `ProviderError` on anything else the API rejected the
    message for — a 4xx is not something retrying will fix.
    """
    payload = {
        "message_id": message_id,
        "tenant_id": tenant_id,
        "channel": channel,
        "sender": sender,
        "subject": subject,
        "body_text": body_text,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{api_base_url.rstrip('/')}/messages",
                json=payload,
                headers={"Authorization": f"Bearer {api_token}"},
            )
    except (httpx.TimeoutException, httpx.NetworkError) as err:
        raise TransientError(f"could not reach TaskFlow API at {api_base_url}: {err}") from err

    if resp.status_code in (429, 502, 503, 504):
        raise TransientError(f"TaskFlow API transient error ({resp.status_code}): {resp.text}")
    if resp.status_code != 200:
        raise ProviderError(f"TaskFlow API rejected message ({resp.status_code}): {resp.text}")

    return resp.json()  # type: ignore[no-any-return]
