import argparse
import html
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from sqlalchemy import create_engine, func, select, URL
from sqlalchemy.orm import sessionmaker

import settings
import browser
import notifications
from data_models import JobsModel

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Default number of jobs fetched per search URL (used by cron and /search).
DEFAULT_LIMIT = 20

# Bounds on every database interaction. A host that is wedged rather than
# cleanly down can complete a TCP handshake and then never answer; without
# these, a crawl (or the Telegram listener, which shares this code path) blocks
# indefinitely instead of failing loudly.
DB_CONNECT_TIMEOUT = 10          # seconds
DB_STATEMENT_TIMEOUT_MS = 15_000

# Keep driver exceptions short enough to read in a Telegram message.
_MAX_DETAIL_CHARS = 300


def _db_engine():
    """Build an engine with bounded connect and statement timeouts."""
    return create_engine(
        URL.create(**settings.DB_URL),
        connect_args={
            "connect_timeout": DB_CONNECT_TIMEOUT,
            "options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
        },
        pool_pre_ping=True,
    )


def _describe_exception(e: Exception) -> str:
    text = f"{type(e).__name__}: {e}".replace("\n", " ").strip()
    return text[:_MAX_DETAIL_CHARS] + "…" if len(text) > _MAX_DETAIL_CHARS else text


def _format_age(delta) -> str:
    """Render a timedelta as a compact '4d 2h' / '3h 12m' / '45m' string."""
    total = int(delta.total_seconds())
    if total < 0:
        return "0m"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def check_db() -> tuple[bool, str]:
    """Verify Postgres is reachable and the jobs table is queryable.

    Returns ``(ok, detail)`` and never raises. On success the detail reports how
    long ago the newest job landed — the signal that distinguishes "the pipeline
    is healthy and Upwork is quiet" from "nothing has been written for days".
    """
    engine = None
    try:
        engine = _db_engine()
        with engine.connect() as conn:
            newest = conn.execute(select(func.max(JobsModel.time_created))).scalar()
    except Exception as e:
        return False, _describe_exception(e)
    finally:
        if engine is not None:
            engine.dispose()

    if newest is None:
        return True, "reachable; jobs table is empty"
    if newest.tzinfo is None:  # defensive: column is timestamptz
        newest = newest.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - newest
    return True, f"reachable; newest job {_format_age(age)} ago"


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

    return new_jobs_count


def run_crawl(limit: int, dump: bool, urls: list[str] | None = None):
    """Fetch each search URL via the logged-in browser profile, persist new
    jobs, and send notifications.

    Args:
        urls: Search URLs to crawl. Defaults to the configured base searches
            (``settings.urls``); an ad-hoc ``/search <terms>`` passes its own.

    Returns the number of new jobs saved this run, or ``None`` if the crawl was
    aborted before searching (login not OK, profile busy, or DB unreachable).
    """
    search_urls = urls if urls is not None else settings.urls
    # Verify the session is still authenticated before crawling; alert if not.
    status, detail = browser.check_login()
    if status in (browser.STATUS_LOGGED_OUT, browser.STATUS_CHALLENGE):
        logging.error("Aborting crawl — not logged in: %s", detail)
        notifications.send_health_report(status, detail, *check_db())
        return None
    if status != browser.STATUS_OK:
        # Busy (another run holds the profile) or transient error — skip quietly.
        logging.warning("Skipping crawl — login check inconclusive (%s): %s", status, detail)
        return None

    # Database setup. A failure here is alerted, not just logged: an unreachable
    # database is indistinguishable from "Upwork had no new jobs" otherwise, and
    # the browser-only health check keeps reporting green throughout.
    try:
        engine = _db_engine()
        Session = sessionmaker(bind=engine)
        db_session = Session()

        # Ensure table exists
        JobsModel.__table__.create(bind=engine, checkfirst=True)
    except Exception as e:
        db_detail = _describe_exception(e)
        logging.error("Database connection failed: %s", db_detail)
        notifications.send_health_report(status, detail, False, db_detail)
        return None

    try:
        all_jobs = []
        for url in search_urls:
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

        new_count = save_jobs(db_session, all_jobs)

        # Send notifications
        try:
            notifications.notify_main(db_session)
        except Exception as e:
            logging.error(f"Notification failed: {e}")

        return new_count
    finally:
        db_session.close()


def run_health(always_report: bool, dump: bool = False):
    """Check both halves of the pipeline: the Upwork session and the database.

    Checking only the browser is what let the 2026-08-14 database outage run for
    four days behind green health reports — the crawl needs *both* to work, so
    the health check verifies both.

    Args:
        always_report: If True (on-demand /health), always send the result to
            Telegram. If False (daily cron), only alert when something is wrong.
        dump: If True, write the probe's landing URL/cookies/HTML to dumps/ for
            debugging false alerts.
    """
    dump_dir = Path(settings.PROFILE_DIR).parent / "dumps" if dump else None
    status, detail = browser.check_login(dump_dir=dump_dir)
    logging.info("Health check — browser: %s — %s", status, detail)

    db_ok, db_detail = check_db()
    logging.info(
        "Health check — database: %s — %s", "ok" if db_ok else "FAILED", db_detail
    )

    degraded = not db_ok or status in (
        browser.STATUS_LOGGED_OUT,
        browser.STATUS_CHALLENGE,
        browser.STATUS_ERROR,
    )
    if always_report or degraded:
        notifications.send_health_report(status, detail, db_ok, db_detail)


def run_listener():
    """Long-poll Telegram for commands (currently /health) from the owner chat."""
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = str(settings.TELEGRAM_CHAT_ID)
    base = f"https://api.telegram.org/bot{token}"

    # Advertise the commands in the Telegram UI (shows in the "/" menu).
    try:
        requests.post(
            f"{base}/setMyCommands",
            json={"commands": [
                {"command": "search", "description": "Run a search now (optionally: /search <terms>)"},
                {"command": "health", "description": "Check Upwork login status"},
            ]},
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
            if cmd == "/search":
                # Terms after the command run a one-off search for just those
                # terms; a bare /search runs the configured base searches.
                parts = text.split(maxsplit=1)
                query = parts[1].strip() if len(parts) > 1 else ""
                if query:
                    logging.info("Received /search command: %r", query)
                    search_urls = [settings.build_search_url(query)]
                    notifications.send_telegram_message(
                        f"🔎 Searching Upwork for “{html.escape(query)}”…"
                    )
                else:
                    logging.info("Received /search command (base searches)")
                    search_urls = None
                    notifications.send_telegram_message("🔎 Running Upwork search…")
                try:
                    new_count = run_crawl(limit=DEFAULT_LIMIT, dump=False, urls=search_urls)
                except Exception as e:
                    logging.error("Manual search failed: %s", e)
                    notifications.send_telegram_message(f"⚠️ Search errored: {e}")
                    continue
                if new_count is None:
                    # Aborted before searching. run_crawl sends a health report
                    # naming the failed subsystem for a dead session or database;
                    # only the "browser busy" case arrives here unexplained.
                    notifications.send_telegram_message(
                        "⚠️ Search didn't complete. If no health report follows, "
                        "the browser was busy with another run — send /health."
                    )
                elif new_count == 0:
                    notifications.send_telegram_message("✅ Search complete — no new jobs.")
                # else: notify_main already posted the new jobs + a summary.
            elif cmd == "/health":
                logging.info("Received /health command")
                notifications.send_telegram_message("🔍 Running Upwork health check…")
                run_health(always_report=True)
            elif cmd == "/start":
                notifications.send_telegram_message(
                    "Upwork crawler bot online.\n"
                    "• /search — run the configured base searches now\n"
                    "• /search &lt;terms&gt; — one-off search for those terms\n"
                    "• /health — check login status"
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
        default=DEFAULT_LIMIT,
        help=f"Maximum number of jobs to fetch per URL (default: {DEFAULT_LIMIT})",
    )
    args = parser.parse_args()

    if args.login:
        browser.login_bootstrap()
        return

    if args.listen:
        run_listener()
        return

    if args.health:
        run_health(always_report=True, dump=args.dump)
        return

    run_crawl(limit=args.limit, dump=args.dump)


if __name__ == "__main__":
    main()
