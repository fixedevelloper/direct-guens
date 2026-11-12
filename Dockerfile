FROM mcr.microsoft.com/playwright/python:v1.65.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY db.py app_scraper.py ./
ENV HEADLESS=true PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app_scraper:app", "--host", "0.0.0.0", "--port", "8000"]
