FROM python:3.12-alpine@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Stdlib only. Removed files stay out of the final scratch layer.
RUN rm -rf /usr/local/lib/python3.12/ensurepip /usr/local/lib/python3.12/idlelib \
       /usr/local/lib/python3.12/tkinter /usr/local/lib/python3.12/turtledemo \
       /usr/local/lib/python3.12/lib2to3 /usr/local/lib/python3.12/pydoc_data \
       /usr/local/lib/python3.12/site-packages/pip* /usr/local/include \
       /usr/local/bin/pip* /root/.cache \
    && find /usr/local -type d -name __pycache__ -exec rm -rf '{}' + \
    && mkdir -p /data && chown 10001:10001 /data
COPY app.py .

# Flatten the pruned filesystem so deleted files do not remain in older layers.
FROM scratch
COPY --from=runtime / /
ENV PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/data
WORKDIR /app
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080
CMD ["/usr/local/bin/python", "-u", "/app/app.py"]
