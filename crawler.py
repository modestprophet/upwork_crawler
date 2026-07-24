import argparse
import logging
from pathlib import Path

from sqlalchemy import create_engine, URL
from sqlalchemy.orm import sessionmaker

import settings
import browser
from data_models import JobsModel
from notifications import notify_main

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
            notify_main(db_session)
        except Exception as e:
            logging.error(f"Notification failed: {e}")

    finally:
        db_session.close()


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
        "--limit",
        type=int,
        default=20,
        help="Maximum number of jobs to fetch per URL (default: 20)",
    )
    args = parser.parse_args()

    if args.login:
        browser.login_bootstrap()
        return

    run_crawl(limit=args.limit, dump=args.dump)


if __name__ == "__main__":
    main()
