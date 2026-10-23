FROM python:3.12-alpine3.23 AS build
WORKDIR /build
COPY requirements.txt .
RUN python -m pip wheel --wheel-dir /wheels -r requirements.txt
COPY app app
COPY db db
COPY main.py .
RUN python -m compileall -q app db main.py

FROM python:3.12-alpine3.23 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/file-manager
COPY --from=build /wheels /wheels
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /wheels /tmp/requirements.txt \
    && addgroup -S -g 10001 manager \
    && adduser -S -D -u 10001 -G manager manager \
    && mkdir /workspace && chown manager:manager /workspace
COPY --from=build /build/app /opt/file-manager/app
COPY --from=build /build/db /opt/file-manager/db
COPY --from=build /build/main.py /opt/file-manager/main.py
RUN chmod -R go-w /opt/file-manager
USER 10001:10001
WORKDIR /workspace
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"
CMD ["python", "-m", "app.health"]
