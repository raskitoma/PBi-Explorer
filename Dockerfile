# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential curl && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl tzdata && rm -rf /var/lib/apt/lists/* && \
    addgroup --system --gid 10001 app && \
    adduser --system --uid 10001 --gid 10001 --no-create-home --disabled-password app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/app /app/app
WORKDIR /app
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1
ENTRYPOINT ["python", "-m"]
CMD ["app.web"]
