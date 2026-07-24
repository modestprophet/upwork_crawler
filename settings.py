import os
import hvac
from os.path import join, dirname
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

urls = [
    "https://www.upwork.com/nx/search/jobs/?nbs=1&q=tableau%20dashboard&sort=recency&page=1&per_page=50",
    "https://www.upwork.com/nx/search/jobs/?nbs=1&q=etl&sort=recency&page=1&per_page=50",
]
