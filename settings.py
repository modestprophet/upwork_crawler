import os
import hvac
from os.path import join, dirname
from urllib.parse import quote
from dotenv import load_dotenv

dotenv_path = join(dirname(__file__), ".env")
load_dotenv(dotenv_path)

# for vault authentication
VAULT_ADDR = os.environ.get("VAULT_ADDR")
VAULT_ROLE_ID = os.environ.get("VAULT_ROLE_ID")
VAULT_SECRET_ID = os.environ.get("VAULT_SECRET_ID")

# setup vault client
multipass = hvac.Client(url=VAULT_ADDR)
multipass.auth.approle.login(VAULT_ROLE_ID, VAULT_SECRET_ID)

_telegram = multipass.read("secret/etl/consulting/telegram")["data"]
TELEGRAM_BOT_TOKEN = _telegram["bot_token"]
TELEGRAM_CHAT_ID = _telegram["chat_id"]

# Persistent Camoufox profile (cookies + cf_clearance + pinned fingerprint).
# In Docker this should point at a mounted volume so it survives restarts.
PROFILE_DIR = os.environ.get("PROFILE_DIR", "/data/profile")

# Headless for scheduled runs; --login flips this to headful automatically.
HEADLESS = os.environ.get("HEADLESS", "true").lower() not in ("0", "false", "no")

# Derive timezone/geolocation from the exit IP. Needs a GeoLite2 DB present;
# off by default so runs don't crash if it isn't downloaded.
GEOIP = os.environ.get("GEOIP", "false").lower() in ("1", "true", "yes")

# app db
DB_URL = {
    "drivername": "postgresql+psycopg2",
    "username": multipass.read("secret/etl/consulting/db/user")["data"]["user"],
    "password": multipass.read("secret/etl/consulting/db/password")["data"]["password"],
    "host": "10.0.20.18",
    "port": 5432,
    "database": "plumbus",
}

# Upwork job-search URL, parameterized by query. Used both for the configured
# base searches below and for ad-hoc `/search <terms>` requests from Telegram.
SEARCH_URL_TEMPLATE = (
    "https://www.upwork.com/nx/search/jobs/?nbs=1&q={q}&sort=recency&page=1&per_page=50"
)


def build_search_url(query: str) -> str:
    """Build an Upwork search URL for a query string (URL-encoded)."""
    return SEARCH_URL_TEMPLATE.format(q=quote(query))


# Base searches run on the schedule and by a bare `/search` (no terms).
SEARCH_TERMS = ["tableau dashboard", "etl"]
urls = [build_search_url(t) for t in SEARCH_TERMS]
