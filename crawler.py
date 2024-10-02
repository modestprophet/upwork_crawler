import settings
import cloudscraper
from data_models import JobsModel
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, URL
from sqlalchemy.orm import sessionmaker


def fetch_url(url, browser=None):
    scraper = cloudscraper.create_scraper(browser=browser)
    response = scraper.get(url)
    # TODO:  need a way to handle 403 errors.  Ignore 403 jobs or store and retry?
    # retry once if not 200
    if response.status_code != 200:
        print("Retrying!")
        response = scraper.get(url)
        print(response.status_code)
        if response.status_code == 403:
            pass
            # TODO:  maybe 403 is for 'private' listings? (i.e., must be signed in to upwork to view posting)
            # return None
        if response.status_code == 200:
            print("Retry successful!")
    return response


def parse_job_links(response):
    job_links = []
    soup = BeautifulSoup(response.text, 'html.parser')
    job_soup = soup.find_all('a', class_='up-n-link', attrs={'data-test': 'job-tile-title-link'})
    for link in job_soup:
        href = link.get('href')
        href = f"https://www.upwork.com{href.split('?')[0]}" # strip text after the ? in job URLs
        job_links.append(href)
    return job_links


def parse_job(response, url):
    soup = BeautifulSoup(response.text, 'html.parser')
    out_dict = {
        "url": url,
        "title": parse_job_title(soup),
        "description": parse_job_description(soup),
        "budget": parse_job_budget(soup),
        "status": "new"
    }

    return out_dict


def parse_job_title(soup):
    title = soup.find('h4')
    if not title:
        print("No job title section found")
    return title.get_text()

def parse_job_description(soup):
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

    return job_description_text

def parse_job_budget(soup):
    # budget amount
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

    return budget

def write_to_db(session, data_dict):
    # TODO:  what do when zero rows?
    for row in data_dict:
        row_out = JobsModel(**row)
        session.add(row_out)
    try:
        session.commit()
        print("Data inserted successfully")
    except Exception as e:
        session.rollback()
        print(f"Error inserting data: {e}")

def main():
    # # DB stuff
    engine = create_engine(URL.create(**settings.DB_URL))
    Session = sessionmaker(bind=engine)
    session = Session()
    JobsModel().__table__.create(bind=engine, checkfirst=True)

    links_set = set()
    for url in settings.urls:
        job_search_raw = fetch_url(url, settings.browser_headers)
        links_set.update(parse_job_links(job_search_raw))
        print(f"Link set size: {len(links_set)}")
    print(links_set)

    # Query existing URLs from the database
    existing_urls = set(url.url for url in session.query(JobsModel.url).all())
    # Filter the list to keep only URLs that don't exist in the database
    new_urls = [url for url in links_set if url not in existing_urls]

    parsed_jobs = []
    for job_url in new_urls:
        print(job_url)
        raw_job = fetch_url(job_url)
        # if raw_job is None:
        #     print(f"No data from fetch.  Skipping:  {job_url}")
        #     continue
        parsed_job = parse_job(raw_job, job_url)
        parsed_jobs.append(parsed_job)
        if len(parsed_jobs) > 50:
            break

    write_to_db(session, parsed_jobs)
    session.close()


if __name__ == '__main__':
    main()

