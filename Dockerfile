FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DB_PATH=/data/carteira.db DATA_DIR=/data \
    HOME=/home/app XDG_RUNTIME_DIR=/tmp/lonely-wallet-runtime
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /home/app --shell /usr/sbin/nologin app
COPY app ./app
COPY scripts ./scripts
COPY VERSION README.md ./
# Build-time vendoring: the running app serves Chart.js locally, without a browser CDN dependency.
RUN python /srv/scripts/vendor_chartjs.py \
    && mkdir -p /data/backups /home/app /tmp/lonely-wallet-runtime \
    && chown -R app:app /home/app /tmp/lonely-wallet-runtime \
    && chmod 700 /home/app /tmp/lonely-wallet-runtime \
    && chmod 755 /srv/scripts/entrypoint.py
EXPOSE 8080
ENTRYPOINT ["python", "/srv/scripts/entrypoint.py"]
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "--timeout", "1200", "--control-socket", "/tmp/lonely-wallet-runtime/gunicorn.ctl", "app.main:app"]
