"""Multi-Channel Alerting Service for TaskFlow.

Dispatches instant alerts for items requiring human review or escalations across:
1. Gmail SMTP Email (using GMAIL_USER and GMAIL_APP_PASSWORD)
2. Telegram Bot (using TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
3. Slack Webhook (using SLACK_WEBHOOK_URL)
4. Custom HTTP Webhook (using ALERT_WEBHOOK_URL)
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import html

import httpx
import structlog

from taskflow.config.settings import settings
from taskflow.domain.models import ReviewItem

logger = structlog.get_logger()


async def send_email_alert(subject: str, body: str, recipient: str | None = None) -> bool:
    """Send an instant email notification using Gmail SMTP credentials."""
    user = settings().gmail_user
    password = settings().gmail_app_password
    to_email = recipient or settings().ops_email or user

    if not user or not password:
        logger.debug("email_alert_skipped", reason="GMAIL_USER or GMAIL_APP_PASSWORD not set")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)

        logger.info("email_alert_sent", recipient=to_email, subject=subject)
        return True
    except Exception as err:
        logger.warning("email_alert_failed", error=str(err))
        return False


async def send_telegram_alert(text: str, parse_mode: str = "HTML") -> bool:
    """Send instant push alert to Telegram chat via Bot API."""
    token = settings().telegram_bot_token
    chat_id = settings().telegram_chat_id

    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, json=payload)
            if res.status_code != 200:
                logger.warning("telegram_alert_http_error", status_code=res.status_code, response=res.text)
            return res.status_code == 200
    except Exception as err:
        logger.warning("telegram_alert_failed", error=str(err))
        return False


async def send_slack_alert(text: str) -> bool:
    """Send alert message to Slack incoming webhook."""
    webhook_url = settings().slack_webhook_url
    if not webhook_url:
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(webhook_url, json={"text": text})
            return res.status_code == 200
    except Exception as err:
        logger.warning("slack_alert_failed", error=str(err))
        return False


async def send_custom_webhook_alert(payload: dict) -> bool:
    """Send alert payload to generic HTTP Webhook URL."""
    webhook_url = settings().alert_webhook_url
    if not webhook_url:
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(webhook_url, json=payload)
            return res.status_code in (200, 201, 202, 204)
    except Exception as err:
        logger.warning("custom_webhook_alert_failed", error=str(err))
        return False


async def dispatch_alert(item: ReviewItem) -> dict[str, bool]:
    """Dispatch rich alert across all active alert channels for a review item."""
    customer_email = item.sender_email or "Not Provided"
    query_text = item.inbound_body or "No customer query body provided."
    draft_text = item.draft.response_text if item.draft else "No draft response generated."
    score_val = item.decision.confidence.score if (item.decision and item.decision.confidence) else 0.0
    thresh_val = item.decision.confidence.threshold if (item.decision and item.decision.confidence) else 0.70

    citations_list = []
    if item.draft and item.draft.citations:
        citations_list = [f"{c.doc_title} [{c.chunk_id}]" for c in item.draft.citations]
    citations_str = ", ".join(citations_list) if citations_list else "None"

    subject = f"⚡ [TaskFlow Alert] Human Review Required #{item.review_id}"

    email_body = f"""TaskFlow Support Engine — Human Review Required
=================================================
📋 Review ID:       {item.review_id}
🔍 Trace ID:        {item.trace_id}
🏢 Tenant ID:       {item.tenant_id}
👤 Customer Email:  {customer_email}

📥 Customer Query:
-------------------------------------------------
{query_text}

🏷️ Routing Decision:
-------------------------------------------------
Action:       {item.decision.action.value}
Reason:       {item.decision.reason}
Reason Code:  {item.decision.reason_code}
Quality Score: {score_val:.3f} (Threshold: {thresh_val:.3f})

🤖 Drafted Response:
-------------------------------------------------
{draft_text}

📚 Citations: {citations_str}

👉 Action: Open the TaskFlow Operator Dashboard to review, edit, or approve this item.
"""

    cust_email_h = html.escape(customer_email)
    q_text_h = html.escape(query_text)
    d_text_h = html.escape(draft_text)
    r_reason_h = html.escape(item.decision.reason)
    r_code_h = html.escape(item.decision.reason_code)
    c_str_h = html.escape(citations_str)

    telegram_html = f"""⚡ <b>TaskFlow Alert: Human Review Required</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━
🆔 <b>Review ID:</b> <code>{item.review_id}</code>
🔍 <b>Trace ID:</b> <code>{item.trace_id}</code>
👤 <b>Customer Email:</b> <code>{cust_email_h}</code>

📥 <b>Customer Query:</b>
<i>{q_text_h}</i>

🏷️ <b>Reason:</b> {r_reason_h}
📌 <b>Reason Code:</b> <code>{r_code_h}</code>
📊 <b>Quality Score:</b> <code>{score_val:.3f}</code> (Threshold: <code>{thresh_val:.3f}</code>)

🤖 <b>Drafted Response:</b>
<pre>{d_text_h}</pre>

📚 <b>Citations:</b> <code>{c_str_h}</code>

👉 <b>Action Required:</b> Open TaskFlow Operator Console to Approve, Edit, or Reject."""

    results = {}
    results["email"] = await send_email_alert(subject, email_body)
    results["telegram"] = await send_telegram_alert(telegram_html, parse_mode="HTML")
    results["slack"] = await send_slack_alert(
        f"⚡ *TaskFlow Alert*: Review #{item.review_id} | Customer: {customer_email} | Reason: {item.decision.reason}"
    )
    results["webhook"] = await send_custom_webhook_alert(item.model_dump(mode="json"))

    return results
