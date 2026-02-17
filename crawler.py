import argparse
import logging
from apify_client import ApifyClient
from sqlalchemy import create_engine, URL
from sqlalchemy.orm import sessionmaker

import settings
from data_models import JobsModel
from notifications import notify_main

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def fetch_jobs_from_apify(client: ApifyClient, url: str, limit: int) -> list[dict]:
    """
    Fetch jobs using the Apify Upwork Job Scraper actor.

    Args:
        client: Authenticated ApifyClient instance.
        url: The Upwork search URL to scrape.
        limit: Maximum number of jobs to retrieve.

    Returns:
        A list of dictionaries containing job details.
    """
    run_input = {
        "rawUrl": url,
        "limit": limit,
        "maxJobAge": {
            "value": 1,
            "unit": "days",
        },  # Fetch jobs posted within the last day
    }

    logging.info(f"Starting Apify actor for URL: {url}")

    # Run the actor and wait for it to finish
    run = client.actor("neatrat/upwork-job-scraper").call(run_input=run_input)

    logging.info(f"Actor run finished. Dataset ID: {run['defaultDatasetId']}")

    # Fetch results from the dataset
    dataset_items = client.dataset(run["defaultDatasetId"]).list_items().items
    logging.info(f"Retrieved {len(dataset_items)} items from dataset.")

    return dataset_items


def parse_apify_job(job_data: dict) -> dict:
    """
    Map Apify job data to our internal JobsModel structure.
    """
    # Format budget string
    budget = "N/A"
    if job_data.get("budget"):
        budget = f"Total budget: ${job_data['budget']}"
    elif job_data.get("hourlyMin") and job_data.get("hourlyMax"):
        budget = f"Hourly: ${job_data['hourlyMin']}-${job_data['hourlyMax']}"
    elif job_data.get("hourlyMin"):
        budget = f"Hourly: ${job_data['hourlyMin']}+"

    return {
        "url": job_data.get("url"),
        "title": job_data.get("title"),
        "description": job_data.get("description"),
        "budget": budget,
        "status": "new",
    }


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


def main():
    parser = argparse.ArgumentParser(description="Upwork Crawler using Apify")
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of jobs to fetch per URL (default: 20)",
    )
    args = parser.parse_args()

    # check for API key
    if not settings.APIFY_API_KEY:
        logging.error(
            "APIFY_API_KEY not found in settings. Please set it in your .env file."
        )
        return

    # Initialize Apify client
    client = ApifyClient(settings.APIFY_API_KEY)

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
                raw_jobs = fetch_jobs_from_apify(client, url, args.limit)
                for job in raw_jobs:
                    parsed_job = parse_apify_job(job)
                    if parsed_job["url"]:  # Ensure URL exists
                        all_jobs.append(parsed_job)
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


if __name__ == "__main__":
    main()
