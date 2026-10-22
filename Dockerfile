FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/data/leads.sqlite3
WORKDIR /app
COPY bot.py app.py storage.py backup.py ./
RUN mkdir -p /data && python -m compileall -q /app
VOLUME ["/data"]
CMD ["python", "-u", "bot.py"]
