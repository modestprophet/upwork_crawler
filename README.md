# Upwork Crawler

Monitors Upwork job searches as a **logged-in user**, stores new postings in
Postgres, and pings Discord — feeding a downstream n8n pipeline that drafts
proposals. Scraping is done with a self-hosted anti-detect browser
([Camoufox](https://camoufox.com)) so there's no third-party scraping service in
the loop.

## Why Camoufox

Upwork sits behind Cloudflare Turnstile, which kills naive headless scraping.
Instead of fighting the challenge on every request, this crawler reuses a
**persistent, hand-authenticated browser profile**: you sign in and solve
Turnstile **once**, by hand, and every scheduled run afterwards reuses the saved
session (cookies + `cf_clearance`) with a **pinned browser fingerprint**, so
Cloudflare sees a stable, already-cleared, logged-in browser and rarely
re-challenges. This suits a personal, low-volume cadence.

> This replaced an earlier Apify integration (and before that, a raw Playwright
> attempt that Cloudflare blocked).

## How it works

```
                 ┌─────────────────────────────────────────────┐
                 │  crawler.py (in Docker, on cron)             │
                 │                                              │
  settings.urls ─┤  Camoufox (browser.py)                      │
  (search URLs)  │    → logged-in profile @ /data/profile      │
                 │    → reads window.__NUXT__ job data          │
                 │  save new jobs ──────────────► Postgres      │
                 │  notify ─────────────────────► Discord       │
                 └─────────────────────────────────────────────┘
                                    │
                                    ▼
                     n8n/ "Upwork Proposal Generator"
                 (drafts application copy, Google Doc, diagram)
```

- **`browser.py`** — Camoufox launcher (persistent profile + pinned fingerprint),
  the `--login` bootstrap, and `fetch_jobs()`, which reads Upwork's server-rendered
  `__NUXT__` state to extract job records.
- **`crawler.py`** — orchestration: fetch each search URL, dedupe, save, notify.
- **`data_models.py`** — the `consulting.jobs` SQLAlchemy model.
- **`notifications.py`** — Discord webhook alerts for new jobs.
- **`settings.py`** — config + secrets, pulled from HashiCorp Vault.
- **`n8n/`** — exported n8n workflows that turn new jobs into draft proposals.

## Configuration

Secrets come from Vault (see `settings.py`); a local `.env` supplies the Vault
address and AppRole credentials:

```
VAULT_ADDR=...
VAULT_ROLE_ID=...
VAULT_SECRET_ID=...
```

Vault paths used: Discord webhook URL, Postgres user/password. Search URLs and
the DB host live in `settings.py`.

Runtime env vars (with defaults):

| Var          | Default          | Purpose                                        |
|--------------|------------------|------------------------------------------------|
| `PROFILE_DIR`| `/data/profile`  | Persistent Camoufox profile (mount a volume)   |
| `HEADLESS`   | `true`           | Headless scheduled runs; `--login` overrides   |
| `GEOIP`      | `false`          | Derive timezone/geo from exit IP (needs DB)    |

## Running

Local (dev):

```bash
uv venv --python 3.11 .venv && . .venv/bin/activate
uv pip install -r requirements.txt
python -m camoufox fetch
python crawler.py --login     # one-time interactive sign-in (needs a display)
python crawler.py             # a headless crawl
```

Docker (production) — see **[DEPLOY.md](DEPLOY.md)** for the full walkthrough,
including the one-time VNC login. In short:

```bash
docker compose build
docker compose run --rm --service-ports login   # sign in via http://<host>:6080/vnc.html
docker compose up -d crawler                     # scheduled crawls (cron)
```

### CLI

```
python crawler.py             # scheduled-style crawl (headless)
python crawler.py --login     # open a headful browser to authenticate once
python crawler.py --dump      # also write raw HTML/JSON to /data/dumps for debugging
python crawler.py --limit N   # cap jobs fetched per search URL (default 20)
```

## Operational notes

- **Run from a residential/business IP, not a datacenter IP** — Cloudflare weights
  ASN reputation heavily. Do the login from the same host that runs the crawler so
  `cf_clearance` is issued against the right IP.
- Re-run `--login` if the session expires (rare at low volume); scheduled runs log
  a clear message when they hit a Cloudflare challenge.
- Schedule lives in the `Dockerfile` cron entry (default: every 2h, 07:00–19:00
  America/Chicago).
