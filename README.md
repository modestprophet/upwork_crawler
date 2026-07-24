# Upwork Crawler

Monitors Upwork job searches as a **logged-in user**, stores new postings in
Postgres, and pings Telegram — feeding a downstream n8n pipeline that drafts
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
                 │  notify ─────────────────────► Telegram      │
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
- **`notifications.py`** — Telegram bot alerts for new jobs.
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

Vault paths used: Telegram bot token + chat id (`secret/etl/consulting/telegram`,
keys `bot_token` and `chat_id`) and Postgres user/password. Search URLs and the
DB host live in `settings.py`. See **[Telegram setup](#telegram-setup)** to
create the bot and find your chat id.

Runtime env vars (with defaults):

| Var          | Default          | Purpose                                        |
|--------------|------------------|------------------------------------------------|
| `PROFILE_DIR`| `/data/profile`  | Persistent Camoufox profile (mount a volume)   |
| `HEADLESS`   | `true`           | Headless scheduled runs; `--login` overrides   |
| `GEOIP`      | `false`          | Derive timezone/geo from exit IP (needs DB)    |

## Telegram setup

Job alerts are delivered by a Telegram bot you own. One-time setup:

1. **Create the bot.** In Telegram, open a chat with
   [@BotFather](https://t.me/BotFather), send `/newbot`, and follow the prompts
   (give it a name and a username ending in `bot`). BotFather replies with an
   **HTTP API token** like `123456789:AAExampleTokenStringHere`. Keep it secret.
2. **Start a chat with your new bot** — search its username, open it, and press
   **Start** (or send any message). A bot cannot message you until you've
   messaged it first.
3. **Find your chat id.** Visit this URL in a browser (paste your token):
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   In the JSON response, find `"chat":{"id":<number>,...}`. That number is your
   `chat_id` (it's negative for groups/channels).
   > To receive alerts in a **group**: add the bot to the group, send a message
   > mentioning it, then re-check `getUpdates` for the group's (negative) chat id.
4. **Store both values in Vault** at `secret/etl/consulting/telegram`:
   ```bash
   vault kv put secret/etl/consulting/telegram \
     bot_token='123456789:AAExampleTokenStringHere' \
     chat_id='123456789'
   ```
5. **Test it:**
   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage" \
     -d chat_id=<YOUR_CHAT_ID> -d text="Upwork crawler test ✅"
   ```
   You should get the message in Telegram and `{"ok":true,...}` back.

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
python crawler.py --health    # verify Upwork login; alert Telegram if logged out
python crawler.py --listen    # run the Telegram listener (handles /health)
python crawler.py --dump      # also write raw HTML/JSON to /data/dumps for debugging
python crawler.py --limit N   # cap jobs fetched per search URL (default 20)
```

## Health checks

The logged-in session occasionally needs a manual refresh (re-run `--login`).
Three mechanisms surface a dead session via Telegram:

- **Per-crawl guard** — every scheduled crawl first verifies login; if it's
  logged out or challenged, it alerts and skips (instead of silently finding
  nothing).
- **Daily check** — a cron job at 06:30 runs `--health` and alerts *only* if the
  session is dead.
- **On-demand `/health`** — the long-lived `telegram` service (`--listen`)
  responds to a `/health` command you send in the chat with the current status
  (it also registers `/health` in Telegram's command menu). Only your configured
  `chat_id` is honored.

You can also trigger a crawl from Telegram with **`/search`** (handled by the
same `telegram` listener): it runs a search immediately, posts any new jobs, and
replies with a completion note (e.g. "no new jobs") so you get feedback either
way. Bare `/search` runs the configured base searches (`SEARCH_TERMS` in
`settings.py`); **`/search <terms>`** (e.g. `/search power bi`) runs a one-off
search for just those terms instead. Only your configured `chat_id` is honored.

Because the crawler, health check, and listener share one browser profile (which
Firefox locks to a single instance), a file lock in the `/data` volume
serializes their launches so they never collide.

Run the listener alongside the crawler:

```bash
docker compose up -d crawler telegram
```

## Operational notes

- **Run from a residential/business IP, not a datacenter IP** — Cloudflare weights
  ASN reputation heavily. Do the login from the same host that runs the crawler so
  `cf_clearance` is issued against the right IP.
- Re-run `--login` if the session expires (rare at low volume); scheduled runs log
  a clear message when they hit a Cloudflare challenge.
- Schedule lives in the `Dockerfile` cron entry (default: every 2h, 07:00–19:00
  America/Chicago).
