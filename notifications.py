import html
import time

import requests

import settings
from data_models import JobsModel


# Telegram message hard limit.
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def get_new_jobs(session):
    """Retrieve new jobs from the database"""
    return session.query(JobsModel).filter(JobsModel.status == "new").all()


def compile_telegram_message(job):
    """Compile a single Telegram message (HTML parse mode) from the job data.

    Everything user-provided is HTML-escaped, and the description is truncated
    (before escaping, so we never split an HTML entity) to keep the whole
    message under Telegram's 4096-character limit.
    """
    title = html.escape(job.title or "Untitled")
    url = html.escape(job.url or "", quote=True)
    budget = html.escape(job.budget or "N/A")

    title_link = f'<a href="{url}">{title}</a>'
    header = (
        "<b>New Job Alert</b>\n\n"
        f"<b>Title:</b> {title_link}\n"
        f"<b>Budget:</b> {budget}\n"
        f"<b>ID:</b> <code>{job.id}</code>\n\n"
    )

    # Reserve room for the header plus a safety margin for entity expansion
    # (escaping can lengthen the text, e.g. "&" -> "&amp;").
    avail = TELEGRAM_MAX_MESSAGE_LENGTH - len(header) - 300
    raw_desc = job.description or ""
    if len(raw_desc) > avail:
        raw_desc = raw_desc[:avail] + "…"

    return header + html.escape(raw_desc)


def send_telegram_message(text):
    """Send a message to the configured Telegram chat via the Bot API."""
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        print("Warning: Telegram credentials are not set. Skipping notification.")
        return

    try:
        response = requests.post(
            TELEGRAM_API_URL.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )

        if response.status_code != 200:
            print(f"Error sending Telegram notification: HTTP {response.status_code}")
            print(f"Response Body: {response.text}")

    except requests.exceptions.RequestException as e:
        print(f"Error sending Telegram notification: {e}")


def send_health_report(status, detail, db_ok, db_detail):
    """Send a Telegram message covering both halves of the pipeline.

    A crawl needs the Upwork session *and* the database, so the report always
    names both — a green line for one must never be mistaken for overall health.

    Args:
        status: One of the ``browser.STATUS_*`` codes.
        detail: Short human-readable explanation of the browser status.
        db_ok: Whether Postgres was reachable and queryable.
        db_detail: Explanation of the database result (on success, this reports
            how long ago the newest job landed).
    """
    detail = html.escape(detail or "")
    db_detail = html.escape(db_detail or "")

    if status == "ok":
        browser_line = f"✅ <b>Upwork session:</b> logged in\n   <i>{detail}</i>"
    elif status in ("logged_out", "challenge"):
        browser_line = f"🚨 <b>Upwork session:</b> NOT logged in\n   <i>{detail}</i>"
    elif status == "busy":
        browser_line = f"⏳ <b>Upwork session:</b> not checked — browser busy\n   <i>{detail}</i>"
    else:  # error
        browser_line = f"⚠️ <b>Upwork session:</b> check errored\n   <i>{detail}</i>"

    if db_ok:
        db_line = f"✅ <b>Database:</b> reachable\n   <i>{db_detail}</i>"
    else:
        db_line = f"🚨 <b>Database:</b> UNREACHABLE\n   <i>{db_detail}</i>"

    # Headline reflects the worst of the two.
    if status in ("logged_out", "challenge") or not db_ok:
        headline = "🚨 <b>Upwork crawler: DEGRADED</b>"
    elif status == "ok" and db_ok:
        headline = "✅ <b>Upwork crawler health</b>"
    else:
        headline = "⚠️ <b>Upwork crawler health</b>"

    remedies = []
    if status in ("logged_out", "challenge"):
        remedies.append(
            "Refresh the Upwork session:\n"
            "<code>docker compose run --rm --service-ports login</code>"
        )
    if not db_ok:
        remedies.append(
            "Check the database host — crawls are being dropped until it returns. "
            "It does not need a crawler restart once it is back."
        )

    text = f"{headline}\n\n{browser_line}\n{db_line}"
    if remedies:
        text += "\n\n" + "\n\n".join(remedies)
    send_telegram_message(text)


def update_job_status(session, jobs):
    """Update the status of the jobs in the database"""
    for job in jobs:
        job.status = "review"
    session.commit()


def notify_main(session):
    """Main function to retrieve jobs and send notifications"""
    jobs = get_new_jobs(session)
    if not jobs:
        print("No new jobs found. Skipping notification.")
        return

    # Send a summary message first
    summary_message = f"<b>Upwork Crawler:</b> Found {len(jobs)} new job(s)!"
    send_telegram_message(summary_message)

    for job in jobs:
        message = compile_telegram_message(job)
        send_telegram_message(message)
        # Stay well under Telegram's ~1 msg/sec-per-chat rate limit.
        time.sleep(1)

    print(f"Successfully sent {len(jobs)} job notifications to Telegram.")
    update_job_status(session, jobs)
