import os
import hvac
from os.path import join, dirname
from dotenv import load_dotenv

dotenv_path = join(dirname(__file__), '.env')
load_dotenv(dotenv_path)

# for vault authentication
VAULT_ADDR = os.environ.get("VAULT_ADDR")
VAULT_ROLE_ID = os.environ.get("VAULT_ROLE_ID")
VAULT_SECRET_ID = os.environ.get("VAULT_SECRET_ID")

# setup vault client
multipass = hvac.Client(url=VAULT_ADDR)
multipass.auth.approle.login(VAULT_ROLE_ID, VAULT_SECRET_ID)

# app db
DB_URL = {'drivername': 'postgresql+psycopg2',
          'username': multipass.read('secret/etl/db/user')['data']['user'],
          'password': multipass.read('secret/etl/db/password')['data']['password'],
          'host': '10.0.20.18',
          'port': 5432,
          'database': 'plumbus'}

input_file = "/home/freesample/Downloads/chicago_crime_stats_2001_to_Present_20240706.csv"
schema = None  #  "projects"  #  use None to create table in 'Public' schema
table_name = "chicago_crime_stats_test"

batch_size = 100000
datatype_detection_sample_size = 10000
