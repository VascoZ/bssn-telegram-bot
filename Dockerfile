FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_FILE=/app/data/bot.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data \
    && chown bot:bot /app/data

COPY bot.py storage.py whatsapp.py faq.json ./

USER bot
VOLUME ["/app/data"]

CMD ["python", "bot.py"]
