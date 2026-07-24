import argparse
import logging
import time
from pathlib import Path

import requests
from sqlalchemy import create_engine, URL
from sqlalchemy.orm import sessionmaker

import settings
import browser
import notifications
from data_models import JobsModel

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def save_jobs(session, jobs_data: list[dict]):
    """
    Save new jobs to the database, avoiding duplicates.
    """
    if not jobs_data:
        logging.info("No jobs to process.")
        return

    # Get existing URLs to check for duplicates
    existing_urls = {row.url for row in session.query(JobsModel.url).all()}

    new_jobs_count = 0
    for job in jobs_data:
        if job["url"] not in existing_urls:
            new_job = JobsModel(**job)
            session.add(new_job)
            existing_urls.add(
                job["url"]
            )  # Add to set to prevent duplicates within this batch
            new_jobs_count += 1

    if new_jobs_count > 0:
        try:
            session.commit()
            logging.info(
                f"Successfully added {new_jobs_count} new jobs to the database."
            )
        except Exception as e:
            session.rollback()
            logging.error(f"Error saving jobs to database: {e}")
    else:
        logging.info("No new unique jobs found.")


def run_crawl(limit: int, dump: bool):
    """Fetch each configured search URL via the logged-in browser profile,
    persist new jobs, and send notifications."""
    # Verify the session is still authenticated before crawling; alert if not.
    status, detail = browser.check_login()
    if status in (browser.STATUS_LOGGED_OUT, browser.STATUS_CHALLENGE):
        logging.error("Aborting crawl — not logged in: %s", detail)
        notifications.send_health_report(status, detail)
        return
    if status != browser.STATUS_OK:
        # Busy (another run holds the profile) or transient error — skip quietly.
        logging.warning("Skipping crawl — login check inconclusive (%s): %s", status, detail)
        return

    # Database setup
    try:
        engine = create_engine(URL.create(**settings.DB_URL))
        Session = sessionmaker(bind=engine)
        db_session = Session()

        # Ensure table exists
        JobsModel.__table__.create(bind=engine, checkfirst=True)
    except Exception as e:
        logging.error(f"Database connection failed: {e}")
        return

    try:
        all_jobs = []
        for url in settings.urls:
            try:
                dump_dir = (
                    Path(settings.PROFILE_DIR).parent / "dumps"
                    if dump
                    else None
                )
                jobs = browser.fetch_jobs(url, limit, dump_dir=dump_dir)
                all_jobs.extend(jobs)
            except Exception as e:
                logging.error(f"Failed to fetch/parse jobs for {url}: {e}")

        save_jobs(db_session, all_jobs)

        # Send notifications
        try:
            notifications.notify_main(db_session)
        except Exception as e:
            logging.error(f"Notification failed: {e}")

    finally:
        db_session.close()


def run_health(always_report: bool):
    """Check whether the profile is still logged into Upwork.

    Args:
        always_report: If True (on-demand /health), always send the result to
            Telegram. If False (daily cron), only alert when something is wrong.
    """
    status, detail = browser.check_login()
    logging.info("Health check: %s — %s", status, detail)
    if always_report or status in (browser.STATUS_LOGGED_OUT, browser.STATUS_CHALLENGE, browser.STATUS_ERROR):
        notifications.send_health_report(status, detail)


def run_listener():
    """Long-poll Telegram for commands (currently /health) from the owner chat."""
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = str(settings.TELEGRAM_CHAT_ID)
    base = f"https://api.telegram.org/bot{token}"

    # Advertise the command in the Telegram UI (shows in the "/" menu).
    try:
        requests.post(
            f"{base}/setMyCommands",
            json={"commands": [{"command": "health", "description": "Check Upwork login status"}]},
            timeout=10,
        )
    except Exception as e:
        logging.warning("setMyCommands failed: %s", e)

    # Drain any backlog so we don't act on stale commands after a restart.
    offset = None
    try:
        r = requests.get(f"{base}/getUpdates", params={"offset": -1}, timeout=20).json()
        if r.get("result"):
            offset = r["result"][-1]["update_id"] + 1
    except Exception:
        pass

    logging.info("Telegram listener started; send /health from the owner chat.")
    while True:
        try:
            r = requests.get(
                f"{base}/getUpdates", params={"timeout": 50, "offset": offset}, timeout=60
            )
            updates = r.json().get("result", [])
        except Exception as e:
            logging.warning("getUpdates failed: %s", e)
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if chat != chat_id or not text:
                continue  # ignore anyone who isn't the configured owner chat
            cmd = text.split()[0].split("@")[0].lower()
            if cmd == "/health":
                logging.info("Received /health command")
                notifications.send_telegram_message("🔍 Running Upwork health check…")
                run_health(always_report=True)
            elif cmd == "/start":
                notifications.send_telegram_message(
                    "Upwork crawler bot online. Send /health to check login status."
                )


def main():
    parser = argparse.ArgumentParser(description="Upwork Crawler (Camoufox)")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open a headful browser to log in once and save the profile.",
    )
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Write raw page HTML + JSON responses to a dumps/ dir for tuning.",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Check Upwork login status; alert Telegram if logged out.",
    )
    parser.add_argument(
        "--listen",
        action="store_true",
        help="Run the Telegram listener that handles the /health command.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of jobs to fetch per URL (default: 20)",
    )
    args = parser.parse_args()

    if args.login:
        browser.login_bootstrap()
        return

    if args.listen:
        run_listener()
        return

    if args.health:
        run_health(always_report=True)
        return

    run_crawl(limit=args.limit, dump=args.dump)


if __name__ == "__main__":
    main()
