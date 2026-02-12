FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

# Copy dependency file and install Python packages
COPY pyproject.toml .
RUN pip install --no-cache-dir . && \
    playwright install chromium

# Copy application code
COPY crawler.py settings.py data_models.py notifications.py ./

CMD ["python", "crawler.py"]
