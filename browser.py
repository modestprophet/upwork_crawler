"""Camoufox-based Upwork fetcher.

Replaces the Apify integration with a self-hosted, anti-detect Firefox
(Camoufox) that reuses a *persistent, hand-authenticated* browser profile.

The design assumes a personal, low-volume use case:

* You log in **once**, interactively, via ``--login`` (headful / over VNC in the
  container) and solve any Cloudflare Turnstile by hand. The session cookies and
  Cloudflare ``cf_clearance`` cookie persist on disk in ``PROFILE_DIR``.
* Every scheduled run afterwards launches the *same* profile headless with the
  *same* pinned fingerprint, so Cloudflare sees a stable, already-cleared,
  logged-in browser and rarely re-challenges.

The fingerprint is pinned by generating one Camoufox preset at login time and
saving it next to the profile; it is replayed on every launch so the
User-Agent (which ``cf_clearance`` is bound to) never drifts.
"""

import fcntl
import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from camoufox.fingerprints import get_random_preset
from camoufox.sync_api import Camoufox

import settings

logger = logging.getLogger(__name__)

# Filename (inside the profile dir) where the pinned fingerprint preset lives.
PRESET_FILENAME = "fingerprint_preset.json"

# Heuristic keys that mark a JSON object as an Upwork job record.
_JOB_ID_KEYS = ("ciphertext", "ciphertextUid", "uid", "jobId", "id", "url")

# Health-check status codes returned by check_login().
STATUS_OK = "ok"
STATUS_LOGGED_OUT = "logged_out"
STATUS_CHALLENGE = "challenge"
STATUS_BUSY = "busy"
STATUS_ERROR = "error"


class ProfileBusyError(RuntimeError):
    """Raised when another process is already using the browser profile."""


def _profile_dir() -> Path:
    d = Path(settings.PROFILE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def _profile_lock(timeout: int = 150) -> Iterator[None]:
    """Serialize browser launches that share the profile directory.

    Firefox locks a profile to a single instance, and the crawler, the daily
    health check, and the /health listener may all try to launch against the
    same (volume-mounted) profile. This flock — on a file in the /data volume —
    coordinates them across processes *and* containers.
    """
    lock_path = _profile_dir().parent / ".browser.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() >= deadline:
                handle.close()
                raise ProfileBusyError(
                    "browser profile is busy (another run/login in progress)"
                )
            time.sleep(2)
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _load_or_create_preset(create: bool) -> Optional[dict]:
    """Return the pinned fingerprint preset, generating it on first login.

    Args:
        create: If True (login mode) and no preset exists yet, generate and
            persist one. If False (a scheduled run) and none exists, return
            None and let the caller error out — running without the pinned
            fingerprint would invalidate the cleared session.
    """
    path = _profile_dir() / PRESET_FILENAME
    if path.exists():
        return json.loads(path.read_text())

    if not create:
        return None

    preset = get_random_preset(os="windows")
    if preset is None:
        logger.warning("No bundled fingerprint preset available; using default.")
        return None
    path.write_text(json.dumps(preset))
    logger.info("Generated and pinned a new fingerprint preset at %s", path)
    return preset


@contextmanager
def _browser(headless: bool, require_preset: bool) -> Iterator[Any]:
    """Launch Camoufox against the persistent profile.

    Yields a Playwright ``BrowserContext`` (persistent-context mode).
    """
    preset = _load_or_create_preset(create=not require_preset or headless is False)
    if preset is None and require_preset:
        raise RuntimeError(
            f"No pinned fingerprint found in {_profile_dir()}. "
            "Run `python crawler.py --login` first to create the profile."
        )

    launch_kwargs: dict[str, Any] = dict(
        persistent_context=True,
        user_data_dir=str(_profile_dir()),
        headless=headless,
        os="windows",
        locale="en-US",
        humanize=True,
    )
    if settings.GEOIP:
        # Auto-derive timezone/geolocation from the exit IP so they match the
        # host the container runs on. Requires the GeoLite2 DB to be present
        # (see `camoufox fetch`), hence off by default.
        launch_kwargs["geoip"] = True
    if preset is not None:
        launch_kwargs["fingerprint_preset"] = preset

    with _profile_lock():
        with Camoufox(**launch_kwargs) as context:
            yield context


# --------------------------------------------------------------------------- #
# Login bootstrap
# --------------------------------------------------------------------------- #


def login_bootstrap(login_timeout: int = 600) -> None:
    """Open Upwork headful for a one-time manual login.

    Intended to be run over VNC in the container (or on any machine with a
    display). Log in, solve any Cloudflare Turnstile by hand, then the profile
    — including the ``cf_clearance`` and Upwork session cookies — is saved to
    ``PROFILE_DIR`` for headless reuse.
    """
    logger.info("Launching headful browser for interactive login...")
    with _browser(headless=False, require_preset=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.upwork.com/ab/account-security/login", timeout=60_000)

        print("\n" + "=" * 70)
        print("  INTERACTIVE LOGIN")
        print("  1. Log into Upwork in the browser window.")
        print("  2. Solve any Cloudflare Turnstile / captcha by hand.")
        print("  3. Land on your logged-in dashboard, then come back here.")
        print("=" * 70)

        deadline = time.time() + login_timeout
        while time.time() < deadline:
            try:
                input("\nPress ENTER once you are fully logged in (or Ctrl-C to abort)... ")
                break
            except EOFError:
                # No stdin (e.g. detached). Fall back to polling for auth cookie.
                if _has_auth_cookie(context):
                    break
                time.sleep(5)

        if _has_auth_cookie(context):
            logger.info("Auth cookies detected — profile saved to %s", _profile_dir())
        else:
            logger.warning(
                "Could not confirm an Upwork auth cookie. The profile was still "
                "saved; re-run --login if scheduled fetches fail to authenticate."
            )


def _has_auth_cookie(context: Any) -> bool:
    """Best-effort check that an Upwork login cookie is present."""
    try:
        cookies = context.cookies()
    except Exception:  # pragma: no cover - defensive
        return False
    names = {c.get("name", "") for c in cookies}
    # Upwork sets these once you're authenticated.
    return bool(names & {"master_access_token", "oauth2_global_js_token", "user_uid"})


# --------------------------------------------------------------------------- #
# Health / login check
# --------------------------------------------------------------------------- #

# Upwork redirects unauthenticated users to a login URL; these substrings mark it.
_LOGIN_URL_MARKERS = ("account-security/login", "/ab/account-security", "/login")
# A page requiring auth — Upwork redirects to login if the session is dead.
_LOGIN_PROBE_URL = "https://www.upwork.com/nx/find-work/best-matches"


def _looks_logged_out(final_url: str, html: str) -> bool:
    url = (final_url or "").lower()
    if any(m in url for m in _LOGIN_URL_MARKERS):
        return True
    # Login form fields present on the sign-in page.
    return 'name="login[username]"' in html or 'id="login_password"' in html


def check_login(dump_dir: Optional[Path] = None) -> tuple[str, str]:
    """Verify the persistent profile is still logged into Upwork.

    Returns a ``(status, detail)`` tuple where status is one of the module's
    ``STATUS_*`` constants. Never raises — transient/lock failures map to
    ``STATUS_BUSY``/``STATUS_ERROR`` so callers don't misreport a live session
    as logged out. If ``dump_dir`` is given, the probe's landing URL, cookie
    names and rendered HTML are written there for debugging false alerts.
    """
    cookie_names: list[str] = []
    try:
        with _browser(headless=True, require_preset=True) as context:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(_LOGIN_PROBE_URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            final_url = page.url
            html = page.content()
            cookie_ok = _has_auth_cookie(context)
            try:
                cookie_names = sorted(c.get("name", "") for c in context.cookies())
            except Exception:
                cookie_names = []
    except ProfileBusyError as e:
        return STATUS_BUSY, str(e)
    except Exception as e:  # pragma: no cover - defensive
        return STATUS_ERROR, f"health check failed to launch: {e}"

    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        (dump_dir / "health_probe.html").write_text(html)
        (dump_dir / "health_probe.json").write_text(
            json.dumps(
                {
                    "final_url": final_url,
                    "cookie_ok": cookie_ok,
                    "cookie_names": cookie_names,
                    "is_challenged": _is_challenged(html),
                    "looks_logged_out": _looks_logged_out(final_url, html),
                    "html_len": len(html),
                },
                indent=2,
            )
        )
        logger.info("Wrote health probe dump to %s (landed on %s)", dump_dir, final_url)

    if _is_challenged(html):
        return STATUS_CHALLENGE, "hit a Cloudflare challenge — cf_clearance may be stale"
    if _looks_logged_out(final_url, html) or not cookie_ok:
        return STATUS_LOGGED_OUT, f"session appears logged out (landed on {final_url})"
    return STATUS_OK, "reached the logged-in Find Work page"


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #


def _flatten_candidate_lists(node: Any, out: list) -> None:
    """Recursively collect lists-of-dicts that look like job records."""
    if isinstance(node, list):
        dicts = [x for x in node if isinstance(x, dict)]
        if dicts:
            sample = dicts[:10]
            hits = sum(_looks_like_job(x) for x in sample)
            if hits >= max(1, (len(sample) + 1) // 2):  # majority look like jobs
                out.append(dicts)
        for item in node:
            _flatten_candidate_lists(item, out)
    elif isinstance(node, dict):
        for value in node.values():
            _flatten_candidate_lists(value, out)


def _looks_like_job(obj: dict) -> bool:
    return "title" in obj and any(k in obj for k in _JOB_ID_KEYS)


def _build_url(job: dict) -> Optional[str]:
    if job.get("url"):
        url = job["url"]
        return url if url.startswith("http") else f"https://www.upwork.com{url}"
    for key in ("ciphertext", "ciphertextUid", "uid"):
        cipher = job.get(key)
        if cipher:
            cipher = str(cipher)
            if not cipher.startswith("~"):
                cipher = "~" + cipher
            return f"https://www.upwork.com/jobs/{cipher}"
    return None


def _build_budget(job: dict) -> str:
    amount = job.get("amount")
    if isinstance(amount, dict):
        amount = amount.get("rawValue") or amount.get("amount")
    if amount:
        return f"Total budget: ${amount}"

    hb = job.get("hourlyBudget") or {}
    hmin = job.get("hourlyBudgetMin") or (hb.get("min") if isinstance(hb, dict) else None)
    hmax = job.get("hourlyBudgetMax") or (hb.get("max") if isinstance(hb, dict) else None)
    if hmin and hmax:
        return f"Hourly: ${hmin}-${hmax}"
    if hmin:
        return f"Hourly: ${hmin}+"
    return "N/A"


def _normalize_job(job: dict) -> Optional[dict]:
    url = _build_url(job)
    if not url:
        return None
    return {
        "url": url,
        "title": job.get("title"),
        "description": job.get("description") or job.get("shortDescription"),
        "budget": _build_budget(job),
        "status": "new",
    }


def fetch_jobs(url: str, limit: int, dump_dir: Optional[Path] = None) -> list[dict]:
    """Fetch jobs for one Upwork search URL using the logged-in profile.

    Upwork server-renders the first page of results into the page's ``__NUXT__``
    state (a minified JS blob with de-duplicated variable refs, so it can't be
    text-parsed reliably). We let the live browser resolve it by reading
    ``window.__NUXT__`` via ``page.evaluate`` and pulling the job array. The
    XHR/JSON scan is kept as a fallback. If ``dump_dir`` is given, the raw HTML
    and JSON responses are written there for debugging.
    """
    captured: list[Any] = []

    def _on_response(response: Any) -> None:
        ctype = (response.headers or {}).get("content-type", "")
        if "application/json" not in ctype:
            return
        try:
            captured.append(response.json())
        except Exception:
            return

    nuxt_jobs: list[dict] = []
    with _browser(headless=settings.HEADLESS, require_preset=True) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", _on_response)
        logger.info("Navigating to %s", url)
        page.goto(url, timeout=90_000, wait_until="domcontentloaded")
        # Wait for the server-rendered job tiles to be present.
        try:
            page.wait_for_selector('[data-test="JobTile"]', timeout=30_000)
        except Exception:
            pass
        try:
            nuxt_jobs = page.evaluate(_NUXT_JOBS_JS) or []
        except Exception as e:
            logger.warning("Could not read __NUXT__ state: %s", e)
        html = page.content()

    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        (dump_dir / "page.html").write_text(html)
        (dump_dir / "responses.json").write_text(json.dumps(captured)[:5_000_000])
        (dump_dir / "nuxt_jobs.json").write_text(json.dumps(nuxt_jobs)[:5_000_000])
        logger.info(
            "Wrote debug dump (%d JSON responses, %d __NUXT__ jobs) to %s",
            len(captured), len(nuxt_jobs), dump_dir,
        )

    if _is_challenged(html):
        logger.error(
            "Page looks like a Cloudflare challenge — the session may have "
            "expired. Re-run `--login` to refresh cf_clearance."
        )

    # Primary source: __NUXT__ jobs. Fallback: any job-like list in the XHR JSON.
    raw_groups: list[list] = []
    if nuxt_jobs:
        raw_groups.append(nuxt_jobs)
    else:
        logger.info("No __NUXT__ jobs found; falling back to XHR JSON scan.")
        for blob in captured:
            _flatten_candidate_lists(blob, raw_groups)
        raw_groups.sort(key=len, reverse=True)

    jobs: list[dict] = []
    seen: set[str] = set()
    for group in raw_groups:
        for raw in group:
            norm = _normalize_job(raw)
            if norm and norm["url"] not in seen:
                seen.add(norm["url"])
                jobs.append(norm)
            if len(jobs) >= limit:
                break
        if len(jobs) >= limit:
            break

    logger.info("Extracted %d jobs from %s", len(jobs), url)
    return jobs


# JS run in the page to resolve __NUXT__ and return the job array. Walks the
# state tree for the first list whose items all carry `ciphertext` + `title`.
_NUXT_JOBS_JS = """
() => {
  const root = window.__NUXT__;
  if (!root) return [];
  let found = null;
  const seen = new Set();
  const walk = (o) => {
    if (found || o === null || typeof o !== 'object') return;
    if (seen.has(o)) return;
    seen.add(o);
    if (Array.isArray(o)) {
      if (o.length && o.every(x => x && typeof x === 'object'
          && 'ciphertext' in x && 'title' in x)) { found = o; return; }
      for (const x of o) { walk(x); if (found) return; }
    } else {
      for (const k in o) { walk(o[k]); if (found) return; }
    }
  };
  walk(root);
  if (!found) return [];
  return found.map(j => ({
    ciphertext: j.ciphertext,
    title: j.title,
    description: j.description,
    amount: j.amount || null,
    hourlyBudget: j.hourlyBudget || null,
    hourlyBudgetMin: j.hourlyBudgetMin || null,
    hourlyBudgetMax: j.hourlyBudgetMax || null,
  }));
}
"""


def _is_challenged(html: str) -> bool:
    """True only for an actual Cloudflare interstitial served *instead of* the page.

    Note: Cloudflare injects its ``challenge-platform`` script and the
    "Verifying you are human" Turnstile string into *normal, cleared* pages too,
    so those substrings are NOT reliable markers. The real "Just a moment…"
    interstitial is a small page that carries the managed-challenge JS payload
    (``_cf_chl_opt`` / ``__CF$cv$params``) and a ``challenge-running`` container.
    """
    if len(html) > 100_000:
        # The interstitial is a tiny standalone page; a large document is the
        # real (cleared) page, which may merely embed the Turnstile widget.
        return False
    markers = (
        "_cf_chl_opt",
        "__CF$cv$params",
        "cf-challenge-running",
        "id=\"challenge-running\"",
        "<title>Just a moment...</title>",
    )
    return any(m in html for m in markers)
