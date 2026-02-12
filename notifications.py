import settings
import requests
import time
from data_models import JobsModel


# Discord message limits
DISCORD_MAX_MESSAGE_LENGTH = 2000


def get_new_jobs(session):
    """Retrieve new jobs from the database"""
    return session.query(JobsModel).filter(JobsModel.status == "new").all()


def compile_discord_message(job):
    """Compile a single Discord message from the job data, respecting limits"""
    # Discord markdown for a nice link
    title_link = f"[{job.title}]({job.url})"

    # Simple message format
    # Reserve space for ID, URL, Title, and other formatting.
    # Estimated header/footer length: 150 characters
    max_desc_len = DISCORD_MAX_MESSAGE_LENGTH - len(title_link) - 150

    description = job.description
    if len(description) > max_desc_len:
        description = description[:max_desc_len] + "..."

    message = f"""**New Job Alert**

**Title:** {title_link}
**ID:** `{job.id}`

{description}
"""

    # Discord messages are sent in a JSON payload.
    return {"content": message}


def send_discord_webhook(message_data):
    """Send a message to the Discord webhook URL"""
    if not settings.DISCORD_WEBHOOK_URL:
        print("Warning: DISCORD_WEBHOOK_URL is not set. Skipping notification.")
        return

    try:
        response = requests.post(
            settings.DISCORD_WEBHOOK_URL, json=message_data, timeout=10
        )

        if response.status_code not in [200, 204]:
            print(f"Error sending Discord notification: HTTP {response.status_code}")
            print(f"Response Body: {response.text}")

    except requests.exceptions.RequestException as e:
        print(f"Error sending Discord notification: {e}")


def send_alert(subject, body):
    """Send a simple alert notification for errors like session expiry"""
    message = f"**{subject}**\n\n{body}"
    message_data = {"content": message}
    send_discord_webhook(message_data)


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
    summary_message = f"**Upwork Crawler:** Found {len(jobs)} new jobs!"
    send_discord_webhook({"content": summary_message})

    for job in jobs:
        message_data = compile_discord_message(job)
        send_discord_webhook(message_data)
        # Discord rate limit is 50 requests per second, but 1 second delay is safe.
        time.sleep(1)

    print(f"Successfully sent {len(jobs)} job notifications to Discord.")
    update_job_status(session, jobs)
