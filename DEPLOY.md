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
# "deploy" the code
rsync -avz --exclude='.venv/' /path/to/upwork_crawler/ user@host:~/upwork_crawler/

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
docker compose up -d crawler telegram
```

- `crawler` runs cron: a headless crawl every 2 hours from 07:00–19:00
  plus a daily login/health check at 06:30 (America/Chicago). Adjust the
  schedule in the `Dockerfile` (`/etc/cron.d`).
- `telegram` runs the listener for the on-demand `/health` command. It shares
  the profile volume with `crawler`; a file lock keeps their browser launches
  from colliding.

## Deploying a code change

Sync the code to the host, then **rebuild** — `docker compose up -d` on its own
will happily restart the containers with the old code and look like it worked:

```bash
rsync -avz --exclude='.venv/' /path/to/upwork_crawler/ user@host:~/upwork_crawler/

docker compose up -d --build crawler telegram   # build once, recreate both
docker compose run --rm crawler run --health    # verify the new code end to end
```

The verification step should put a report in Telegram naming **both** subsystems
— e.g. `✅ Upwork session: logged in` and `✅ Database: reachable / newest job
50m ago`. If the format looks different from what you just deployed, the rebuild
didn't take. (It acquires the browser flock, so it may wait behind a scheduled
run; that's harmless.)

**Rebuild both services, not just `crawler`.** `.py` files and
`docker-entrypoint.sh` are `COPY`'d into the image at build time, and the
`telegram` listener imports from `crawler.py` — so an updated `crawler` running
alongside a stale `telegram` can fail only later, on a command you rarely
exercise. They share one image, so `--build` builds it once for both.

Two things that do **not** need a rebuild, because they're bind-mounted or live
outside the image:

- `.env` (mounted at `/app/.env`) — restart the services to pick it up.
- The browser profile — it's in the `upwork_profile` named volume, so recreating
  containers never costs you `cf_clearance` or the login session. **No re-login
  is needed after a deploy.**

Changing the crawl schedule *does* need a rebuild: the cron table is baked into
the image (`/etc/cron.d` in the `Dockerfile`).

## Handy commands

```bash
# Trigger a search from Telegram: send /search to the bot (handled by the
# `telegram` listener) for the configured base searches, or /search <terms>
# (e.g. /search power bi) for a one-off search. It posts new jobs and replies
# with a completion note.

# Run a single crawl immediately (headless), see logs live
docker compose run --rm crawler run

# Debug: dump raw page HTML + all JSON responses to /data/dumps for tuning
docker compose run --rm crawler run --dump

# Re-authenticate when cf_clearance / session expires (rare, low volume)
docker compose run --rm --service-ports login

# Check both subsystems now (session + database), reported to Telegram
docker compose run --rm crawler run --health

# Redeploy after a code change (see "Deploying a code change" above)
docker compose up -d --build crawler telegram
```

## Inspecting the browser in a running deployment

**There is no live browser to attach to.** `browser.py` launches Camoufox inside
a context manager for the duration of a single fetch or health check and closes
it on the way out. The `crawler` container runs cron; the `telegram` container
sits in a Telegram long-poll. Between runs no Firefox process exists, and neither
service exposes a remote-debugging port or VNC. What persists is the *profile*
on the `upwork_profile` volume.

So there are two ways to look at the session, in increasing order of risk.

### 1. Read the profile without launching anything (safe, zero risk)

Cookies live in `cookies.sqlite` in the profile. This tells you whether the
session and `cf_clearance` are actually alive, and when they expire:

```bash
docker compose exec crawler python - <<'PY'
import os, sqlite3, shutil, tempfile, datetime
src = os.path.join(os.environ["PROFILE_DIR"], "cookies.sqlite")
tmp = os.path.join(tempfile.mkdtemp(), "c.sqlite")
shutil.copy(src, tmp)          # copy: Firefox may hold a lock on the original
rows = sqlite3.connect(tmp).execute(
    "SELECT name, host, expiry FROM moz_cookies "
    "WHERE host LIKE '%upwork.com' ORDER BY name"
).fetchall()
now = datetime.datetime.now().timestamp()
for name, host, exp in rows:
    when = datetime.datetime.fromtimestamp(exp).isoformat() if exp else "session"
    flag = "EXPIRED" if exp and exp < now else ""
    print(f"{name:32} {host:24} {when} {flag}")
PY
```

Look for `cf_clearance` (Cloudflare) and `master_access_token` /
`oauth2_global_js_token` / `user_uid` (Upwork auth — the same set
`_has_auth_cookie()` checks). Also worth a glance:

```bash
docker compose exec crawler cat /data/profile/fingerprint_preset.json | head -c 400
docker compose exec crawler ls -la /data /data/profile /data/dumps
docker compose logs --tail=200 crawler     # cron output is tailed to stdout
docker compose exec crawler tail -100 /var/log/cron.log
```

### 2. Probe with a real browser and keep the evidence

`--health --dump` runs the login probe headless and writes the landing URL,
cookie names, challenge/logged-out verdicts and full rendered HTML to
`/data/dumps/health_probe.{json,html}`:

```bash
docker compose run --rm crawler run --health --dump
docker compose exec crawler cat /data/dumps/health_probe.json
```

`run --dump` does the same for a full crawl, adding `page.html`,
`responses.json` and `nuxt_jobs.json`. Both take the `/data/.browser.lock`
flock, so they queue behind a scheduled run rather than colliding with it —
expect up to a 150 s wait, after which they abort as `busy`.

### 3. Watch a browser live over VNC

To *see* the page — a Turnstile interstitial, a login redirect, a consent
banner — start a headful browser against the same profile and connect a VNC
client. The `login` service already wires up Xvfb + x11vnc + noVNC:

```bash
docker compose run --rm --service-ports login
# then open http://<host>:6080/vnc.html and click Connect
```

> **This re-authenticates.** It navigates to the login page and, if you sign in,
> issues a fresh `cf_clearance`. Do not reach for it as a diagnostic while the
> current session still works — confirm the session is genuinely dead (step 1 or
> 2) first. The pinned fingerprint is *not* regenerated when one already exists,
> so re-running `login` is safe in that respect.

To watch a browser **without** touching the login flow, use the `debug` mode —
same VNC plumbing, but it runs an ordinary crawl headful instead of signing in:

```bash
docker compose run --rm --service-ports login debug --dump
# open http://<host>:6080/vnc.html; the browser closes when the crawl finishes
```

(The `login` service is used only because it's the one publishing ports 6080/5900;
the trailing `debug --dump` replaces its command.) `HEADLESS=false` is read by
`settings.HEADLESS`, which only `fetch_jobs()` honors — `check_login()` is
hardcoded headless, so this shows you a real search page render, not the health
probe. Pair it with `--dump` and you get both the live view and the saved HTML.

## Troubleshooting: no new jobs

A crawl needs **two** things — a live Upwork session and a reachable database —
so `--health` checks both and every report names both:

```
🚨 Upwork crawler: DEGRADED

✅ Upwork session: logged in
   reached the logged-in Find Work page
🚨 Database: UNREACHABLE
   OperationalError: could not connect to server: Connection timed out
```

On success the database line reports **how long ago the newest job landed**
(`reachable; newest job 2h 5m ago`). That number is the "is data actually
flowing" signal — a session and database that are both green while the age
climbs into days means the crawl is running but extracting nothing, which points
at the parser (see *Tuning the parser* below), not the infrastructure.

A crawl that cannot reach the database now sends this report instead of failing
silently, so an outage announces itself on the next 2-hourly run rather than
hiding as "Upwork had no new postings." Alerts repeat each run until it recovers.

> Historical note: this behavior exists because a database-host outage on
> 2026-08-14 stalled the crawler for four days behind green, browser-only health
> checks. All database calls now carry a 10 s connect timeout and a 15 s
> statement timeout, so a *wedged* host (one that completes a TCP handshake and
> then never answers) fails loudly instead of hanging the crawl and the listener.

To confirm which half is broken by hand:

```bash
docker compose exec crawler python -c \
  "import crawler; print(crawler.check_db())"
```

If that reports a failure, the browser session is irrelevant — fix the database
and the next cron crawl recovers on its own. **No container restart or re-login
is needed**, and re-running `--login` would needlessly spend a working
`cf_clearance`.

Note that a host wedged by a storage-layer failure will not recover when the
storage does: if nothing on it can `exec()`, nothing is left running to notice.
Verify the database host is actually *serving*, not merely powered on.

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
