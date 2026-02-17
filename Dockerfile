FROM python:3.11-slim

WORKDIR /app

# Install system dependencies needed for psycopg2 and cron
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc cron tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/America/Chicago /etc/localtime \
    && echo "America/Chicago" > /etc/timezone

# Copy dependency file and install Python packages
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application code
COPY crawler.py settings.py data_models.py notifications.py ./

# Setup cron job
# Schedule: Every 2 hours from 07:00 to 19:00 (7am, 9am, 11am, 13pm, 15pm, 17pm, 19pm)
RUN echo "0 7-19/2 * * * root cd /app && /usr/local/bin/python /app/crawler.py >> /var/log/cron.log 2>&1" > /etc/cron.d/crawler-cron

# Give execution rights on the cron job
RUN chmod 0644 /etc/cron.d/crawler-cron

# Apply cron job
RUN crontab /etc/cron.d/crawler-cron

# Create the log file to be able to run tail
RUN touch /var/log/cron.log

# Ensure environment variables are available to cron
CMD ["sh", "-c", "printenv > /etc/environment && cron -f"]
