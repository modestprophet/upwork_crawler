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

smtp_user = multipass.read('secret/etl/consulting/smtpapp/user')['data']['user']
smtp_password = multipass.read('secret/etl/consulting/smtpapp/password')['data']['password']


# Email settings
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587 # 465 for SSL
FROM_EMAIL = multipass.read('secret/etl/consulting/smtpapp/user')['data']['user']
PASSWORD = multipass.read('secret/etl/consulting/smtpapp/password')['data']['password']
TO_EMAIL = 'modestprophet@gmail.com'

# app db
DB_URL = {'drivername': 'postgresql+psycopg2',
          'username': multipass.read('secret/etl/consulting/db/user')['data']['user'],
          'password': multipass.read('secret/etl/consulting/db/password')['data']['password'],
          'host': '10.0.20.18',
          'port': 5432,
          'database': 'plumbus'}

urls = ['http://www.upwork.com/nx/search/jobs/?nbs=1&per_page=50&q=tableau%20dashboard',
        'http://www.upwork.com/nx/search/jobs/?nbs=1&q=tableau%20developer&page=1&per_page=50']

browser_headers = {
    'browser': 'chrome',
    'platform': 'windows',
    'desktop': True
}

