FROM python:3.12-slim

WORKDIR /app

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY flickr_interestingness.py .
COPY web_app.py .
COPY settings.json .
COPY .env .

EXPOSE 8000

CMD uvicorn web_app:app --host 0.0.0.0 --port ${PORT:-8000}
