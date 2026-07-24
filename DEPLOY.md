# Upwork Crawler — Camoufox deployment

This crawler scrapes Upwork search results as **you**, a logged-in user, using a
self-hosted anti-detect Firefox ([Camoufox](https://camoufox.com)) instead of
Apify. It keeps a persistent, hand-authenticated browser profile so Cloudflare
Turnstile is solved **once, by hand**, and reused silently afterwards.

## How it works

- **Persistent profile** (`/data/profile`, a Docker volume): holds your Upwork
  session cookies and Cloudflare's `cf_clearance` cookie.
- **Pinned fingerprint** (`/data/profile/fingerprint_preset.json`): generated on
  first login and replayed on every run, so the User-Agent — which `cf_clearance`
  is bound to — never drifts.
- **`--login`** opens a headful browser (over VNC in the container) for the
  one-time manual sign-in + Turnstile solve.
- Scheduled runs launch the same profile **headless** and read the search API
  JSON the page fetches, normalizing it into the existing `jobs` table.

> **Run it from a residential/business IP, not a datacenter IP.** Cloudflare
> weights ASN reputation heavily; a cloud IP is the fastest way to get
> challenged. Your on-prem host (same network as the Postgres box) is ideal.

## First-time setup on the remote host

```bash
# 1. Build the image
docker compose build

# 2. One-time interactive login (starts a VNC-visible browser)
docker compose run --rm --service-ports login
```

Then, while that command is running:

1. Open **`http://<remote-host>:6080/vnc.html`** in your browser and click
   *Connect*. You'll see the Camoufox window on the Upwork login page.
2. Log in, and solve any Cloudflare Turnstile / captcha by hand.
3. Land on your logged-in dashboard.
4. Return to the **terminal** where you ran the command and press **ENTER**.

The profile (cookies + `cf_clearance` + pinned fingerprint) is now saved to the
`upwork_profile` volume.

> Doing the login *from the remote host's container* means `cf_clearance` is
> issued against that host's real IP — so the first headless run reuses it
> cleanly instead of tripping a fresh challenge.

## Run the scheduler

```bash
docker compose up -d crawler
```

`crawler` runs cron: a headless crawl every 2 hours from 07:00–19:00
(America/Chicago). Adjust the schedule in the `Dockerfile` (`/etc/cron.d`).

## Handy commands

```bash
# Run a single crawl immediately (headless), see logs live
docker compose run --rm crawler run

# Debug: dump raw page HTML + all JSON responses to /data/dumps for tuning
docker compose run --rm crawler run --dump

# Re-authenticate when cf_clearance / session expires (rare, low volume)
docker compose run --rm --service-ports login
```

## Tuning the parser (first live run)

The extractor (`browser.py`) locates the job list by scanning the JSON responses
Upwork's search page fetches — it looks for the largest list of objects that have
a `title` plus an id/`ciphertext`/`url`. This is resilient to minor API changes,
but if Upwork returns an unexpected shape and you get **0 jobs**, run with
`--dump` and inspect `/data/dumps/responses.json` to confirm the field names,
then adjust `_looks_like_job` / `_normalize_job`.

## What changed from the Apify version

- `browser.py` — new Camoufox fetcher + `--login` bootstrap (replaces
  `fetch_jobs_from_apify`).
- `crawler.py` — calls `browser.fetch_jobs`, adds `--login` / `--dump`.
- `settings.py` — dropped `APIFY_API_KEY`; added `PROFILE_DIR`, `HEADLESS`, `GEOIP`.
- `Dockerfile` — Firefox runtime libs + Xvfb/x11vnc/noVNC for the login flow.
- `docker-compose.yml` — persistent `upwork_profile` volume + `login` service.
- Apify secret in Vault (`secret/etl/consulting/apify`) is no longer read.
