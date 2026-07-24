FROM python:3.11-slim

WORKDIR /app

# System dependencies:
#  - Firefox/Camoufox runtime libraries
#  - Xvfb + fluxbox + x11vnc + noVNC/websockify for the interactive --login flow
#  - cron + tzdata for scheduling
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron tzdata ca-certificates \
    xvfb x11vnc fluxbox novnc websockify \
    libgtk-3-0 libx11-xcb1 libdbus-glib-1-2 libxt6 libxtst6 \
    libasound2 libpangocairo-1.0-0 libpango-1.0-0 libcairo-gobject2 \
    libgdk-pixbuf-2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libxcb-shm0 libnss3 libnspr4 libfontconfig1 libfreetype6 \
    libgl1 libegl1 fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/America/Chicago /etc/localtime \
    && echo "America/Chicago" > /etc/timezone \
    && ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download the Camoufox browser binary into the image
RUN python -m camoufox fetch

# Application code
COPY crawler.py settings.py data_models.py notifications.py browser.py ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Scheduled jobs (Debian cron reads /etc/environment, written by the entrypoint):
#   - crawl every 2 hours from 07:00 to 19:00
#   - a daily login/health check at 06:30 (alerts Telegram only if logged out)
RUN printf '%s\n' \
    "0 7-19/2 * * * root cd /app && /usr/local/bin/python /app/crawler.py >> /var/log/cron.log 2>&1" \
    "30 6 * * * root cd /app && /usr/local/bin/python /app/crawler.py --health >> /var/log/cron.log 2>&1" \
    > /etc/cron.d/crawler-cron \
    && chmod 0644 /etc/cron.d/crawler-cron \
    && crontab /etc/cron.d/crawler-cron

ENV PROFILE_DIR=/data/profile
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["cron"]
