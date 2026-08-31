ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.06.1
FROM ${BUILD_FROM} AS builder

RUN apk add --no-cache build-base libxml2-dev libxslt-dev libffi-dev openssl-dev cargo
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

FROM builder AS test
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt
COPY rootfs/app /app
COPY tests /tests
COPY pyproject.toml /pyproject.toml
COPY config.yaml Dockerfile requirements.txt /addon/
ENV PYTHONPATH="/app"
ENTRYPOINT ["/opt/venv/bin/python3"]
CMD ["-m", "pytest", "/tests", "-q"]

FROM ${BUILD_FROM} AS runtime
RUN apk add --no-cache libxml2 libxslt libffi openssl ca-certificates
COPY --from=builder /opt/venv /opt/venv
COPY rootfs/app /app
COPY run.sh /run.sh
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED="1"
RUN chmod 0755 /run.sh
CMD ["/run.sh"]
