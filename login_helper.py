"""
Login Helper for Upwork Crawler

Run this script on a local machine with a GUI to log in to Upwork
and export the session state for use by the headless crawler.

This launches your system Chrome as a normal process (not via Playwright's
automation hooks), then connects via CDP to export cookies. This avoids
Cloudflare's bot detection which flags Playwright-launched browsers.

Usage:
    python login_helper.py [--output state.json]

After running:
    1. A Chrome browser window will open to the Upwork login page.
    2. Log in manually (including any 2FA prompts).
    3. Once login is detected, the session is exported to state.json.
    4. Transfer state.json to your Docker VM:
       rsync state.json dockervm:/path/to/crawler-data/
"""

import argparse
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


UPWORK_LOGIN_URL = "https://www.upwork.com/ab/account-security/login"
UPWORK_LOGGED_IN_INDICATORS = [
    "/nx/find-work",
    "/ab/find-work",
    "/nx/dashboard",
    "/ab/dashboard",
    "/home",
]

LOGIN_PAGE_INDICATORS = [
    "/login",
    "account-security",
]


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


def is_logged_in(url: str) -> bool:
    """Check if the current URL indicates a successful login."""
    return any(indicator in url for indicator in UPWORK_LOGGED_IN_INDICATORS)


def is_login_page(url: str) -> bool:
    """Check if the current URL is still a login page."""
    return any(indicator in url for indicator in LOGIN_PAGE_INDICATORS)


def export_session(output_path: str, debug_port: int = 9222) -> None:
    """Launch Chrome via CDP, wait for manual login, export session state."""

    chrome_path = os.environ.get("CHROME_PATH") or find_chrome_binary()
    print(f"Using Chrome: {chrome_path}")

    # Create a temporary user data directory so Chrome launches as a fully
    # independent instance. Without this, if Chrome is already running,
    # the new process just opens a tab in the existing instance (which
    # doesn't have --remote-debugging-port) and exits immediately.
    user_data_dir = tempfile.mkdtemp(prefix="upwork_login_")
    print(f"Using temporary profile: {user_data_dir}")

    # Launch Chrome as a normal process with remote debugging.
    # This is NOT launched through Playwright, so Cloudflare sees a normal
    # browser -- no automation flags, no webdriver markers.
    chrome_args = [
        chrome_path,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--disable-popup-blocking",
        "--window-size=1280,800",
        UPWORK_LOGIN_URL,
    ]

    print(f"Launching Chrome on debugging port {debug_port}...")
    chrome_proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Wait for debugging port to be ready
    for attempt in range(30):
        # Check if Chrome exited early
        if chrome_proc.poll() is not None:
            stderr = chrome_proc.stderr.read().decode() if chrome_proc.stderr else ""
            print(f"Error: Chrome exited with code {chrome_proc.returncode}")
            if stderr:
                print(f"Chrome stderr: {stderr[:500]}")
            sys.exit(1)
        try:
            with socket.create_connection(("127.0.0.1", debug_port), timeout=1):
                break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    else:
        chrome_proc.kill()
        print("Error: Chrome did not start in time.")
        sys.exit(1)

    try:
        with sync_playwright() as p:
            # Connect to the already-running Chrome instance
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")

            # Get the existing context and page (Chrome already opened the login URL)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()

            print("\nPlease log in to Upwork in the browser window.")
            print("Waiting for login to complete (including any 2FA)...")
            print("(This will wait up to 5 minutes)\n")

            # Poll for login completion
            max_wait_seconds = 300
            poll_interval_ms = 2000
            elapsed = 0

            while elapsed < max_wait_seconds:
                try:
                    page.wait_for_timeout(poll_interval_ms)
                except PlaywrightTimeout:
                    pass
                elapsed += poll_interval_ms // 1000

                current_url = page.url
                if is_logged_in(current_url):
                    print(f"Login detected! Current URL: {current_url}")
                    break
                elif not is_login_page(current_url) and "upwork.com" in current_url:
                    print(f"Login detected (redirected to: {current_url})")
                    break
            else:
                print(
                    "Timed out waiting for login. "
                    "Exporting state anyway in case login succeeded."
                )

            # Give the page a moment to settle
            page.wait_for_timeout(3000)

            # Export session state -- build it manually from CDP context
            cookies = context.cookies()
            upwork_cookies = [c for c in cookies if "upwork" in c.get("domain", "")]

            # Build storage state JSON in the same format as Playwright's
            # context.storage_state() output
            state = {
                "cookies": cookies,
                "origins": [],
            }

            # Try to capture localStorage for upwork.com
            try:
                local_storage_data = page.evaluate("""() => {
                    const items = {};
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        items[key] = localStorage.getItem(key);
                    }
                    return items;
                }""")
                if local_storage_data:
                    origin = page.evaluate("() => window.location.origin")
                    state["origins"].append(
                        {
                            "origin": origin,
                            "localStorage": [
                                {"name": k, "value": v}
                                for k, v in local_storage_data.items()
                            ],
                        }
                    )
            except Exception:
                pass  # localStorage capture is best-effort

            with open(output_path, "w") as f:
                json.dump(state, f, indent=2)

            print(f"\nSession state exported to: {output_path}")
            print(f"Exported {len(upwork_cookies)} Upwork cookies.")

            if upwork_cookies:
                print("\nKey cookies found:")
                for cookie in upwork_cookies:
                    name = cookie["name"]
                    expires = cookie.get("expires", -1)
                    if expires > 0:
                        exp_str = datetime.datetime.fromtimestamp(expires).strftime(
                            "%Y-%m-%d %H:%M"
                        )
                    else:
                        exp_str = "session"
                    print(f"  {name}: expires {exp_str}")

            browser.close()

    finally:
        chrome_proc.terminate()
        try:
            chrome_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome_proc.kill()

        # Clean up temporary profile directory
        try:
            shutil.rmtree(user_data_dir, ignore_errors=True)
        except Exception:
            pass

    print(f"\nDone! Transfer {output_path} to your Docker VM:")
    print(f"  rsync {output_path} dockervm:/path/to/crawler-data/")


def main():
    parser = argparse.ArgumentParser(
        description="Log in to Upwork and export session state for the headless crawler."
    )
    parser.add_argument(
        "--output",
        "-o",
        default="state.json",
        help="Output path for the session state file (default: state.json)",
    )
    parser.add_argument(
        "--debug-port",
        type=int,
        default=9222,
        help="Chrome remote debugging port (default: 9222)",
    )
    args = parser.parse_args()

    try:
        export_session(args.output, args.debug_port)
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
