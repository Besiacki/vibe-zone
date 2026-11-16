FROM python:3.12-alpine@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /usr/local/lib/python3.12/ensurepip /usr/local/lib/python3.12/idlelib \
       /usr/local/lib/python3.12/tkinter /usr/local/lib/python3.12/turtledemo \
       /usr/local/lib/python3.12/site-packages/pip* /usr/local/include \
       /usr/local/bin/pip* /root/.cache \
    && find /usr/local -type d -name __pycache__ -exec rm -rf '{}' + \
    && mkdir -p /data && chown 10001:10001 /data
COPY app.py .

# Flatten the pruned Python Alpine filesystem: removed files must not remain in layers.
FROM scratch
COPY --from=runtime / /
ENV PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/data
WORKDIR /app
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=2s --start-period=5s --retries=3 \
    CMD python -c 'import os,urllib.request; urllib.request.urlopen("http://127.0.0.1:"+os.environ["PORT"]+"/health",timeout=1)'
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --worker-class sync --timeout 30 --access-logfile /dev/null app:app"]
