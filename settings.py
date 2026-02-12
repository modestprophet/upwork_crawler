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

# Discord settings
# DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DISCORD_WEBHOOK_URL = multipass.read("secret/etl/consulting/discord/webhookurl")["data"]["url"]

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
    "https://www.upwork.com/nx/search/jobs/?nbs=1&per_page=50&q=tableau%20dashboard",
    "https://www.upwork.com/nx/search/jobs/?nbs=1&q=tableau%20developer&page=1&per_page=50",
]

# Path to Playwright session state file (exported by login_helper.py)
# Override via STORAGE_STATE_PATH env var for Docker deployments
STORAGE_STATE_PATH = os.environ.get(
    "STORAGE_STATE_PATH",
    os.path.join(os.path.dirname(__file__), "state.json"),
)
