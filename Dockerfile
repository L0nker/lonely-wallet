FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DB_PATH=/data/carteira.db DATA_DIR=/data
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /nonexistent --shell /usr/sbin/nologin app
COPY app ./app
COPY scripts ./scripts
COPY VERSION README.md ./
RUN mkdir -p /data/backups && chmod 755 /srv/scripts/entrypoint.py
EXPOSE 8080
ENTRYPOINT ["python", "/srv/scripts/entrypoint.py"]
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "--timeout", "1200", "app.main:app"]
