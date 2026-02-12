# Upwork Crawler Implementation Summary

## Overview

This document summarizes the key architectural decisions and implementation details for the Playwright-based Upwork crawler that successfully bypasses Cloudflare bot detection while maintaining authenticated access to job listings.

## Core Problem

The original cloudscraper-based implementation was blocked by Cloudflare's bot detection. Additionally, Upwork job visibility requires authenticated access -- anonymous requests receive limited or no job data.

## Solution Architecture

### 1. Dual-Mode Browser Strategy

**Local Development (with GUI):**
- Uses system-installed Chrome via CDP (Chrome DevTools Protocol)
- Chrome launched as independent process with `--user-data-dir` to avoid conflicts with existing Chrome instances
- Headed mode allows manual Cloudflare challenge resolution
- Session exported via `login_helper.py` for reuse

**Production (Docker/Headless):**
- Uses Playwright's bundled Chromium in headless mode
- Relies on valid session cookies from `state.json` to bypass Cloudflare challenges
- No manual intervention required once session is established

### 2. Session Management

**Session Export (`login_helper.py`):**
```python
# Key technique: Launch Chrome OUTSIDE Playwright first
chrome_proc = subprocess.Popen([
    chrome_path,
    f"--remote-debugging-port={port}",
    f"--user-data-dir={temp_dir}",  # Critical: ensures independent instance
    # ... other args
])

# Then connect via CDP - Playwright attaches to already-running browser
browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
```

**Why this works:**
- Chrome starts as a completely normal browser process
- No Playwright automation hooks injected at launch
- Cloudflare sees standard browser fingerprint
- User manually completes login + any challenges
- Cookies exported to `state.json` for headless reuse

**Session Import (`crawler.py`):**
- Loads cookies from `state.json` into browser context
- Valid session cookies allow headless crawler to access authenticated content
- Cloudflare typically allows requests with valid session cookies

### 3. Navigation Strategy

**Warmup Pattern:**
```python
# Navigate to simple page first to establish session
fetch_page(page, "https://www.upwork.com")
# Then fetch search pages - session already validated
```

**Challenge Handling:**
```python
def fetch_page(page, url):
    response = page.goto(url, wait_until="domcontentloaded")
    
    # Give Cloudflare time to render challenge
    page.wait_for_timeout(3000)
    
    # Detect and wait for challenge resolution
    if is_cloudflare_challenge(page):
        wait_for_cloudflare(page)
    
    # Wait for post-challenge navigation to complete
    page.wait_for_load_state("load", timeout=15000)
    page.wait_for_timeout(2000)  # Final settle
    
    return page.content()
```

**Key insight:** Cloudflare challenges appear AFTER initial page load, so we need delays and detection logic, not just status code checking.

### 4. HTML Parsing Strategy

Upwork uses Nuxt.js SSR with data embedded in `__NUXT_DATA__` script tag:

```python
def parse_nuxt_data(html: str) -> list | None:
    pattern = r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>'
    match = re.search(pattern, html, re.DOTALL)
    if match:
        return json.loads(match.group(1))
```

**Parsing priority:**
1. Extract from `__NUXT_DATA__` JSON (most reliable)
2. Fallback to data-test attributes
3. Fallback to CSS class selectors (legacy)

### 5. Cloudflare Detection

```python
def is_cloudflare_challenge(page) -> bool:
    return page.evaluate("""() => {
        const body = document.body ? document.body.innerText : '';
        return body.includes('Verify you are human') ||
               body.includes('Checking your browser') ||
               !!document.querySelector('#challenge-running') ||
               !!document.querySelector('.cf-turnstile');
    }""")
```

## File Structure

```
├── crawler.py           # Main crawler with CDP support
├── login_helper.py      # Session export tool
├── settings.py          # Configuration (URLs, DB, paths)
├── data_models.py       # SQLAlchemy models
├── sendemail.py         # Email notifications
├── Dockerfile           # Production container
├── docker-compose.yml   # Production deployment
└── state.json           # Session state (gitignored)
```

## Usage Workflows

### Local Development

```bash
# 1. Export session (requires GUI)
uv run python login_helper.py
# -> Opens Chrome, user logs in, exports state.json

# 2. Test crawler locally with system Chrome
uv run python crawler.py --use-chrome --headed

# 3. Transfer session to server
rsync state.json server:/opt/crawler-data/
```

### Production (Docker)

```bash
# On server with state.json in place:
docker compose run --rm crawler

# Or via cron (hourly 7am-7pm):
0 7-19 * * * cd /opt/upwork-crawler && docker compose run --rm crawler
```

## Key Configuration

**settings.py:**
```python
STORAGE_STATE_PATH = os.environ.get(
    "STORAGE_STATE_PATH",
    os.path.join(os.path.dirname(__file__), "state.json"),
)

urls = [
    "https://www.upwork.com/nx/search/jobs/?nbs=1&per_page=50&q=tableau%20dashboard",
    "https://www.upwork.com/nx/search/jobs/?nbs=1&q=tableau%20developer&page=1&per_page=50",
]
```

**Docker:**
- Uses `mcr.microsoft.com/playwright/python` base image
- Requires `--init` (prevents zombie processes)
- Requires `--ipc=host` (Chromium shared memory)
- Bind mounts `/opt/crawler-data` for state.json

## Critical Design Decisions

### 1. Why CDP instead of direct Playwright launch?

Playwright's `chromium.launch()` injects automation markers that Cloudflare detects:
- `Runtime.enable` CDP command
- Modified `navigator` properties
- Missing plugins/extensions

By launching Chrome separately and connecting via CDP, the browser appears completely normal to Cloudflare.

### 2. Why `--user-data-dir`?

Without this, Chrome checks for existing instances and joins them (ignoring `--remote-debugging-port`). Each script run needs its own isolated Chrome process.

### 3. Why warmup navigation?

Cloudflare challenges are more likely on the first request. By warming up on a simple page (upwork.com), we:
- Complete any initial challenge
- Establish session cookies
- Reduce challenge probability on subsequent API calls

### 4. Why `domcontentloaded` instead of `networkidle`?

Upwork's SPA keeps persistent connections (analytics, websockets) alive indefinitely. `networkidle` (no network activity for 500ms) never fires.

## Session Refresh Workflow

Sessions expire periodically (typically days to weeks):

1. Run `login_helper.py` locally
2. Complete login + 2FA in browser
3. Transfer fresh `state.json` to server
4. Next crawler run uses new session

The crawler detects expired sessions and sends alert emails.

## Known Limitations

1. **Session expiry:** Requires manual re-authentication when cookies expire
2. **Selector fragility:** Upwork's HTML structure may change, requiring parser updates
3. **Rate limiting:** Aggressive crawling may trigger additional Cloudflare checks
4. **Headless detection:** Some sites detect headless Chrome even with cookies; this solution relies on Upwork being relatively permissive with authenticated headless requests

## Success Metrics

A successful run shows:
- "Warmup complete" message
- Job links found in search pages (not 0)
- Job titles successfully parsed (not "No title found")
- Database entries created
- Email notification sent

## Debugging Tips

**Check if session is valid:**
```python
# In browser console when logged in:
document.cookie.includes('master_access_token')
```

**Check for Cloudflare block:**
- Look for "Just a moment..." or "Checking your browser" in page title
- Check if `state.json` contains Upwork cookies (not just Cloudflare cookies)

**Force fresh session:**
- Delete `state.json`
- Re-run `login_helper.py`

## Technical Dependencies

- Python 3.11+
- Playwright 1.50+
- Chrome/Chromium (system or bundled)
- PostgreSQL (for job storage)
- HashiCorp Vault (for secrets, optional)

---

*This implementation represents a working solution for authenticated web scraping of Upwork job listings while respecting Cloudflare's security measures through legitimate session-based access.*
