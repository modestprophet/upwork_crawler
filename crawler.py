import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import settings
from data_models import JobsModel
from notifications import notify_main
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, URL
from sqlalchemy.orm import sessionmaker
from playwright.sync_api import sync_playwright


class SessionExpiredError(Exception):
    """Raised when the Upwork session has expired and login is required."""

    pass


# URLs that indicate we've been redirected to a login page
LOGIN_PAGE_INDICATORS = [
    "/login",
    "account-security",
]


def check_session_valid(page) -> None:
    """Check if the current page indicates an expired session.

    Raises SessionExpiredError if the browser was redirected to a login page.
    """
    current_url = page.url
    if any(indicator in current_url for indicator in LOGIN_PAGE_INDICATORS):
        raise SessionExpiredError(
            f"Session expired -- redirected to login page ({current_url}). "
            "Re-run login_helper.py and transfer fresh state.json to the server."
        )


def is_cloudflare_challenge(page) -> bool:
    """Detect if the current page is a Cloudflare challenge/interstitial."""
    # Check page title and content for Cloudflare markers
    try:
        title = page.title().lower()
        if "just a moment" in title or "attention required" in title:
            return True
    except Exception:
        pass

    # Check for Cloudflare challenge elements in the DOM
    try:
        cf_present = page.evaluate("""() => {
            const body = document.body ? document.body.innerText : '';
            return body.includes('Verify you are human') ||
                   body.includes('Checking your browser') ||
                   body.includes('cf-challenge') ||
                   !!document.querySelector('#challenge-running') ||
                   !!document.querySelector('#challenge-stage') ||
                   !!document.querySelector('.cf-turnstile');
        }""")
        return cf_present
    except Exception:
        return False


def wait_for_cloudflare(page, timeout_seconds: int = 60) -> None:
    """Wait for a Cloudflare challenge to resolve.

    If the page is showing a Cloudflare challenge, this polls every 2 seconds
    until the challenge is gone or the timeout is reached. If running headed,
    the user can click the challenge manually.
    """
    if not is_cloudflare_challenge(page):
        return

    print("  Cloudflare challenge detected -- waiting for it to resolve...")
    print("  (If running --headed, click the checkbox if prompted)")

    elapsed = 0
    while elapsed < timeout_seconds:
        page.wait_for_timeout(2000)
        elapsed += 2
        if not is_cloudflare_challenge(page):
            print("  Cloudflare challenge resolved.")
            # Give the real page a moment to load after challenge clears
            page.wait_for_timeout(3000)
            return

    print("  Warning: Cloudflare challenge did not resolve within timeout.")


def fetch_page(page, url: str) -> str:
    """Navigate to a URL and return the rendered HTML content.

    Uses 'domcontentloaded' instead of 'networkidle' because Upwork's SPA
    keeps background connections alive (analytics, websockets) that prevent
    networkidle from ever resolving.

    Detects and waits for Cloudflare challenges before returning content.

    Args:
        page: Playwright page object (reused across calls).
        url: The URL to navigate to.

    Returns:
        The fully rendered HTML content of the page.

    Raises:
        SessionExpiredError: If redirected to a login page.
    """
    response = page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # Give Cloudflare time to render any challenge before checking status
    # The challenge JS runs after domcontentloaded
    page.wait_for_timeout(3000)

    # Handle Cloudflare challenge if present
    wait_for_cloudflare(page)

    # After any challenge resolves, the page may still be navigating.
    # Wait for navigation to fully settle before accessing content.
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass

    # Small additional settle time for any post-navigation JS
    page.wait_for_timeout(2000)

    # Re-check response status after all waiting
    current_status = response.status if response else None

    if current_status in (401, 403):
        # Debug: show what page we're actually seeing
        try:
            title = page.title()
            current_url = page.url
            print(f"  Debug: HTTP {current_status}, title='{title}', url={current_url}")
        except Exception:
            pass
        # Check if it's a session issue vs. a genuinely private listing
        check_session_valid(page)
        print(f"  Warning: got HTTP {current_status} for {url}")

    check_session_valid(page)

    return page.content()


def parse_job_links(html: str) -> list[str]:
    """Extract job URLs from a search results page.

    Tries the current Upwork data-test attribute first, then falls back
    to finding job links within article elements.
    """
    job_links = []
    soup = BeautifulSoup(html, "html.parser")

    # Primary selector: current Upwork data-test attribute
    job_soup = soup.find_all("a", attrs={"data-test": "job-tile-title-link UpLink"})

    # Fallback 1: original selector (in case Upwork reverts)
    if not job_soup:
        job_soup = soup.find_all(
            "a", class_="up-n-link", attrs={"data-test": "job-tile-title-link"}
        )

    # Fallback 2: find job links within article tags
    if not job_soup:
        for article in soup.find_all("article"):
            for a in article.find_all("a", href=True):
                if "/jobs/" in a["href"] and "~" in a["href"]:
                    job_soup.append(a)

    # Fallback 3: find any link that looks like a job URL
    if not job_soup:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/jobs/" in href and "~" in href and "/search/" not in href:
                job_soup.append(a)

    for link in job_soup:
        href = link.get("href", "")
        if not href:
            continue
        # Normalize to full URL, strip query params
        clean_path = href.split("?")[0]
        if clean_path.startswith("/"):
            clean_path = f"https://www.upwork.com{clean_path}"
        job_links.append(clean_path)

    return job_links


def parse_nuxt_data(html: str) -> list | None:
    """Extract the __NUXT_DATA__ JSON array from the page HTML.

    Upwork uses Nuxt.js SSR and embeds structured data in a script tag.
    Returns the parsed JSON array, or None if not found.
    """
    pattern = r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>'
    match = re.search(pattern, html, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def find_in_nuxt_data(nuxt_data: list, key: str) -> str | None:
    """Search the NUXT_DATA array for a value following a given key.

    The __NUXT_DATA__ array is a flat list where keys and values are
    adjacent or referenced by index. This searches for common patterns.
    """
    if not nuxt_data:
        return None
    for i, item in enumerate(nuxt_data):
        if item == key and i + 1 < len(nuxt_data):
            # The next item might be the value, or an index reference
            next_item = nuxt_data[i + 1]
            if isinstance(next_item, str) and len(next_item) > 0:
                return next_item
            elif isinstance(next_item, int) and next_item < len(nuxt_data):
                referenced = nuxt_data[next_item]
                if isinstance(referenced, str):
                    return referenced
    return None


def parse_job(html: str, url: str) -> dict:
    """Parse a job detail page into a structured dict.

    Attempts to extract data from __NUXT_DATA__ JSON first,
    then falls back to BeautifulSoup DOM parsing.
    """
    soup = BeautifulSoup(html, "html.parser")
    nuxt_data = parse_nuxt_data(html)

    out_dict = {
        "url": url,
        "title": parse_job_title(soup, nuxt_data),
        "description": parse_job_description(soup, nuxt_data),
        "budget": parse_job_budget(soup, nuxt_data),
        "status": "new",
    }

    return out_dict


def parse_job_title(soup: BeautifulSoup, nuxt_data: list | None = None) -> str:
    """Extract job title from the page.

    Strategy: NUXT_DATA -> data-test attribute -> h4 -> page title tag.
    """
    # Try NUXT_DATA
    if nuxt_data:
        title = find_in_nuxt_data(nuxt_data, "title")
        if title and len(title) > 5 and "Upwork" not in title:
            return title.strip()

    # Try data-test attributes
    title_el = soup.find(attrs={"data-test": "job-title"})
    if not title_el:
        title_el = soup.find(attrs={"data-test": "JobTitle"})
    if title_el:
        return title_el.get_text(strip=True)

    # Fallback: h4 (original selector)
    title_el = soup.find("h4")
    if title_el:
        return title_el.get_text(strip=True)

    # Last resort: page <title> tag (strip " | Upwork" suffix)
    title_el = soup.find("title")
    if title_el:
        raw = title_el.get_text(strip=True)
        # Upwork title tags often look like "Job Title - Upwork"
        for sep in [" - Upwork", " | Upwork"]:
            if sep in raw:
                return raw.split(sep)[0].strip()
        return raw

    return "No title found"


def parse_job_description(soup: BeautifulSoup, nuxt_data: list | None = None) -> str:
    """Extract job description from the page.

    Strategy: NUXT_DATA -> data-test attribute -> class-based selectors.
    """
    # Try NUXT_DATA -- look for the "description" or "snippet" field
    if nuxt_data:
        for key in ["description", "snippet", "descriptionHTML"]:
            desc = find_in_nuxt_data(nuxt_data, key)
            if desc and len(desc) > 20:
                # Strip HTML tags if the value contains them
                if "<" in desc:
                    desc_soup = BeautifulSoup(desc, "html.parser")
                    return desc_soup.get_text(separator="\n").strip()
                return desc.strip()

    # Try data-test attributes (note: Upwork uses uppercase D)
    for test_val in ["Description", "description", "job-description-text"]:
        desc_el = soup.find(attrs={"data-test": test_val})
        if desc_el:
            return desc_el.get_text(separator="\n").strip()

    # Fallback: original selector
    desc_el = soup.find("p", class_="text-body-sm")
    if desc_el:
        text = ""
        for element in desc_el.contents:
            if element.name == "br":
                text += "\n"
            elif element.name == "a":
                text += element.get_text() + "\n"
            else:
                text += str(element)
        return text.strip()

    return "No description found"


def parse_job_budget(soup: BeautifulSoup, nuxt_data: list | None = None) -> str:
    """Extract budget/rate info from the page.

    Strategy: NUXT_DATA -> data-test attributes -> class-based selectors.
    """
    # Try NUXT_DATA -- look for budget-related fields
    if nuxt_data:
        # Look for hourly rate range
        hourly_min = find_in_nuxt_data(nuxt_data, "hourlyBudgetMin")
        hourly_max = find_in_nuxt_data(nuxt_data, "hourlyBudgetMax")
        if hourly_min and hourly_max:
            return f"Hourly: ${hourly_min}-${hourly_max}"

        # Look for fixed budget
        for key in ["fixedBudgetAmount", "budget", "amount"]:
            budget_val = find_in_nuxt_data(nuxt_data, key)
            if budget_val:
                # Clean up the value
                clean = budget_val.replace("$", "").strip()
                if clean and clean != "0":
                    return f"Total budget: ${clean}"

    # Try data-test attributes
    for test_val in ["budget", "Budget", "job-budget"]:
        budget_el = soup.find(attrs={"data-test": test_val})
        if budget_el:
            return budget_el.get_text(strip=True)

    # Fallback: original selector (p.m-0 with strong tags)
    p_tags = soup.find_all("p", class_="m-0")
    if p_tags:
        values = []
        for p in p_tags:
            strong_tag = p.find("strong")
            if strong_tag:
                amount = strong_tag.get_text(strip=True).replace("$", "").strip()
                values.append(amount)
        if len(values) == 2:
            return f"Hourly: ${values[0]}-${values[1]}"
        elif len(values) == 1:
            return f"Total budget: ${values[0]}"

    return "No budget info found"


def write_to_db(session, data_dict: list[dict]) -> None:
    """Write parsed job data to the database."""
    if not data_dict:
        print("No new jobs to write.")
        return

    for row in data_dict:
        row_out = JobsModel(**row)
        session.add(row_out)
    try:
        session.commit()
        print(f"{len(data_dict)} jobs added after parsing.")
    except Exception as e:
        session.rollback()
        print(f"Error inserting data: {e}")


def find_chrome_binary() -> str:
    """Find the system-installed Chrome binary path."""
    candidates = [
        "google-chrome-stable",
        "google-chrome",
        "chromium-browser",
        "chromium",
    ]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError(
        "Could not find Chrome or Chromium. Install Google Chrome or set "
        "the CHROME_PATH environment variable."
    )


def launch_chrome_with_debugging(
    port: int = 9222, headless: bool = False
) -> tuple[subprocess.Popen, str]:
    """Launch Chrome as a normal process with remote debugging enabled.

    This starts Chrome *outside* of Playwright's control, which avoids
    the automation markers that Cloudflare detects. Playwright then
    connects to it via CDP (Chrome DevTools Protocol).

    Uses a temporary user-data-dir so Chrome always launches as an
    independent instance, even if another Chrome is already running.

    Returns:
        A tuple of (process, user_data_dir) -- caller should clean up
        the user_data_dir when done.
    """
    chrome_path = os.environ.get("CHROME_PATH") or find_chrome_binary()
    user_data_dir = tempfile.mkdtemp(prefix="upwork_crawler_")

    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--disable-popup-blocking",
        "--window-size=1280,800",
    ]
    if headless:
        args.append("--headless=new")

    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Wait for the debugging port to become available
    for _ in range(30):
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            shutil.rmtree(user_data_dir, ignore_errors=True)
            raise RuntimeError(
                f"Chrome exited with code {proc.returncode}. stderr: {stderr[:500]}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return proc, user_data_dir
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)

    proc.kill()
    shutil.rmtree(user_data_dir, ignore_errors=True)
    raise TimeoutError(f"Chrome did not start with debugging port {port} in time.")


def load_storage_state_into_context(context, state_path: str) -> None:
    """Manually load cookies from a storage state file into a browser context.

    When connecting via CDP, we can't pass storage_state to new_context(),
    so we load the cookies manually.
    """
    with open(state_path, "r") as f:
        state = json.load(f)

    cookies = state.get("cookies", [])
    if cookies:
        context.add_cookies(cookies)
        print(f"  Loaded {len(cookies)} cookies from {state_path}")

    # Load localStorage via init script if present
    origins = state.get("origins", [])
    for origin_data in origins:
        origin = origin_data.get("origin", "")
        local_storage = origin_data.get("localStorage", [])
        if local_storage:
            # Build a script that sets localStorage for this origin
            items_json = json.dumps(
                {item["name"]: item["value"] for item in local_storage}
            )
            context.add_init_script(f"""() => {{
                if (window.location.origin === '{origin}') {{
                    const items = {items_json};
                    for (const [key, value] of Object.entries(items)) {{
                        try {{ window.localStorage.setItem(key, value); }} catch(e) {{}}
                    }}
                }}
            }}""")


def main():
    parser = argparse.ArgumentParser(description="Upwork job crawler")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed (visible) mode for debugging",
    )
    parser.add_argument(
        "--use-chrome",
        action="store_true",
        help="Use system-installed Chrome via CDP to bypass Cloudflare. "
        "Required when Cloudflare blocks Playwright's bundled Chromium.",
    )
    parser.add_argument(
        "--debug-port",
        type=int,
        default=9222,
        help="Chrome remote debugging port (default: 9222)",
    )
    args = parser.parse_args()

    # Verify state.json exists before doing anything
    if not os.path.exists(settings.STORAGE_STATE_PATH):
        print(
            f"Error: Session state file not found at {settings.STORAGE_STATE_PATH}\n"
            "Run login_helper.py to create it, then transfer to this machine."
        )
        return

    # DB setup
    engine = create_engine(URL.create(**settings.DB_URL))
    Session = sessionmaker(bind=engine)
    db_session = Session()
    JobsModel().__table__.create(bind=engine, checkfirst=True)

    chrome_proc = None
    chrome_data_dir = None

    try:
        with sync_playwright() as p:
            if args.use_chrome:
                # Launch real Chrome outside Playwright, then connect via CDP.
                # This makes Chrome appear as a normal browser to Cloudflare.
                print("Launching system Chrome with remote debugging...")
                chrome_proc, chrome_data_dir = launch_chrome_with_debugging(
                    port=args.debug_port,
                    headless=not args.headed,
                )
                browser = p.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{args.debug_port}"
                )
                # CDP connection gives us the browser's default context;
                # create a new one so we can inject cookies cleanly.
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                )
                load_storage_state_into_context(context, settings.STORAGE_STATE_PATH)
            else:
                # Standard Playwright launch (for Docker / headless server)
                browser = p.chromium.launch(headless=not args.headed)
                context = browser.new_context(
                    storage_state=settings.STORAGE_STATE_PATH,
                    viewport={"width": 1280, "height": 800},
                )

            page = context.new_page()

            # Warmup: Navigate to Upwork homepage first to establish a clean session.
            # This lets any Cloudflare challenge happen on a simple page before
            # we try to fetch data-heavy search results.
            print("Warming up session on Upwork homepage...")
            try:
                fetch_page(page, "https://www.upwork.com")
                print("Warmup complete.")
            except Exception as e:
                print(f"Warmup navigation had issues (may be OK): {e}")

            # Phase 1: Collect job links from search pages
            links_set = set()
            for url in settings.urls:
                print(f"Fetching search page: {url}")
                html = fetch_page(page, url)
                new_links = parse_job_links(html)
                links_set.update(new_links)
                print(f"  Found {len(new_links)} job links.")

            print(
                f"\n{len(links_set)} total jobs found in {len(settings.urls)} queries."
            )

            # Phase 2: Filter out jobs already in the database
            existing_urls = set(
                row.url for row in db_session.query(JobsModel.url).all()
            )
            new_urls = [url for url in links_set if url not in existing_urls]
            print(f"{len(new_urls)} new jobs to fetch.\n")

            # Phase 3: Fetch and parse each new job
            parsed_jobs = []
            for job_url in new_urls:
                print(f"Fetching job: {job_url}")
                html = fetch_page(page, job_url)
                parsed_job = parse_job(html, job_url)
                parsed_jobs.append(parsed_job)
                print(f"  Title: {parsed_job['title']}")

                if len(parsed_jobs) >= 50:
                    print("Reached 50 job limit, stopping.")
                    break

            browser.close()

        # Phase 4: Write to DB and send notifications
        write_to_db(db_session, parsed_jobs)
        try:
            notify_main(db_session)
        except Exception as e:
            print("Notification failed (jobs were still saved to DB): {e}")

    except SessionExpiredError as e:
        print(f"\n{e}")
        try:
            from notifications import send_alert

            send_alert(
                "Upwork Crawler - Session Expired",
                "The Upwork crawler session has expired. "
                "Please run login_helper.py and transfer a fresh state.json.",
            )
            print("Session expiry alert sent to Discord.")
        except Exception as notification_err:
            print(f"Could not send alert notification: {notification_err}")

    except Exception as e:
        print(f"Crawler error: {e}")
        raise

    finally:
        db_session.close()
        if chrome_proc:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome_proc.kill()
        if chrome_data_dir:
            shutil.rmtree(chrome_data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
