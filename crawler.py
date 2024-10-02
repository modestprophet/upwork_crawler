import settings
import cloudscraper
from bs4 import BeautifulSoup


urls = ['http://www.upwork.com/nx/search/jobs/?nbs=1&per_page=50&q=tableau%20dashboard',
        'http://www.upwork.com/nx/search/jobs/?nbs=1&q=tableau%20developer&page=1&per_page=50']

browser_headers = {
    'browser': 'chrome',
    'platform': 'windows',
    'desktop': True
}


def fetch_url(url, browser=None):
    scraper = cloudscraper.create_scraper(browser=browser)
    response = scraper.get(url)
    # TODO:  need a way to handle 403 errors.  Ignore 403 jobs or store and retry?
    # retry once if not 200
    if response.status_code != 200:
        scraper = cloudscraper.create_scraper(browser=browser)
        response = scraper.get(url)
        print("Retrying!")
        if response.status_code == 200:
            print("Retry successful!")
    print(response.status_code)
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

    # job title
    job_title = soup.find('h4').get_text()

    # job description
    job_description_element = soup.find('p', class_='text-body-sm')
    if not job_description_element:
        print("No job description section found")
        return
    # Extract all text, preserving line breaks
    job_description_text = ''
    for element in job_description_element.contents:
        if element.name == 'br':
            job_description_text += '\n'
        elif element.name == 'a':
            job_description_text += element.get_text() + '\n'
        else:
            job_description_text += str(element)

    # budget amount
    budget = ''
    p_tags = soup.find_all('p', class_='m-0')
    if len(p_tags) <= 0 :
        budget = "No budget info found in post"
        return

    # Extract amounts from <strong> tags within those <p> tags
    values = []
    for p in p_tags:
        strong_tag = p.find('strong')
        if strong_tag:
            amount = strong_tag.get_text(strip=True).replace('$', '').strip()
            values.append(amount)

    # Join the values with a hyphen if there are exactly two values
    if len(values) == 2:
        budget = f"Hourly:  {values[0]}-{values[1]}"
    elif len(values) == 1:
        budget = f"Total budget:  {values[0]}"
    else:
        budget = "No budget values found"

    print(budget)



    # Extract
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
        # strip text after the ? in job URLs
        job_url = f"https://www.upwork.com{job.split('?')[0]}"
        print(job_url)
        parsed_job = parse_job(fetch_url(job_url))



if __name__ == '__main__':
    main()

