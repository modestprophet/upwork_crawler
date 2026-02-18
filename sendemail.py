import settings
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from data_models import JobsModel


def get_new_jobs(session):
    """Retrieve new jobs from the database"""
    return session.query(JobsModel).filter(JobsModel.status == 'new').all()


def compile_email_body(jobs):
    """Compile the email body from the job data"""
    body = "New Jobs:\n\n"
    for job in jobs:
        body += f"ID: {job.id}\n"
        body += f"URL: {job.url}\n"
        body += f"Title: {job.title}\n"
        body += f"Description: {job.description}\n\n"
    return body


def send_email(subject, body):
    """Send an email using the SMTP server"""
    msg = MIMEMultipart()
    msg['From'] = settings.FROM_EMAIL
    msg['To'] = settings.TO_EMAIL
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    server = smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT)
    server.starttls()
    server.login(settings.FROM_EMAIL, settings.PASSWORD)
    text = msg.as_string()
    server.sendmail(settings.FROM_EMAIL, settings.TO_EMAIL, text)
    server.quit()

def update_job_status(session, jobs):
    """Update the status of the jobs in the database"""
    for job in jobs:
        job.status = 'review'
    session.commit()


def email_main(session):
    jobs = get_new_jobs(session)
    if jobs:
        email_body = compile_email_body(jobs)
        send_email("New Job Notifications", email_body)
        print("Email sent successfully")
    else:
        print("No new jobs found")

    update_job_status(session, jobs)
