import requests
import cloudscraper
from bs4 import BeautifulSoup


urls = ['http://www.upwork.com/nx/search/jobs/?nbs=1&per_page=50&q=tableau%20dashboard',
        'http://www.upwork.com/nx/search/jobs/?nbs=1&q=tableau%20developer&page=1&per_page=50']

browser_headers = {
    'browser': 'chrome',
    'platform': 'android',
    'desktop': False
}


def fetch_url(url, browser=None):
    scraper = cloudscraper.create_scraper(browser=browser)
    response = scraper.get(url)
    return response


def parse_job_links(response):
    job_links = []
    soup = BeautifulSoup(response.text, 'html.parser')
    job_soup = soup.find_all('a', class_='up-n-link', attrs={'data-test': 'job-tile-title-link'})
    for link in job_soup:
        href = link.get('href')
        job_links.append(href)
    return job_links


def parse_job(response):
    out_dict = dict
    soup = BeautifulSoup(response.text, 'html.parser')
    # look at a sample posting and parse the soup for various objects like description and hourly price
    return out_dict

# # DB stuff
# engine = create_engine(URL.create(**settings.DB_URL))
# Session = sessionmaker(bind=engine)
# Base = declarative_base()
# table_name = settings.table_name

def main():
    links_set = set()
    for url in urls:
        job_search_raw = fetch_url(url, browser_headers)
        links_set.update(parse_job_links(job_search_raw))
    print(links_set)

    for job in links_set:
        # strip the trailing bit of text in each URL
        job_url = f"https://www.upwork.com{job.split('?')[0]}"
        parsed_job = parse_job(fetch_url(job_url))



if __name__ == '__main__':
    main()

