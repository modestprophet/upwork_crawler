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

APIFY_API_KEY = multipass.read("secret/etl/consulting/apify")["data"]["api_key"]
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
    "https://www.upwork.com/nx/search/jobs/?nbs=1&q=tableau%20dashboard&sort=recency&page=1&per_page=50",
    "https://www.upwork.com/nx/search/jobs/?nbs=1&q=etl&sort=recency&page=1&per_page=50",
]
