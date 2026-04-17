FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Minimal system deps for psycopg + healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better Docker layer caching).
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

# Copy the app — backend + logo used by the SPA.
COPY backend /app/backend
COPY tmc_logo.jpg /app/tmc_logo.jpg

# Non-root user — safer on Render / Railway / Fly which all run containers
# with root by default if you don't specify otherwise.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin tmc \
    && chown -R tmc:tmc /app
USER tmc

WORKDIR /app/backend

# Most PaaS (Railway, Fly, Render) set $PORT; default to 8000 for `docker run`.
ENV PORT=8000 \
    WEB_CONCURRENCY=2
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# Shell form so $PORT / $WEB_CONCURRENCY expand at runtime. Two uvicorn
# workers by default — the background analysis ThreadPool runs inside each
# worker so more workers = more parallel AI jobs. Scale WEB_CONCURRENCY by
# CPU count (e.g. `WEB_CONCURRENCY=4` on a 4-vCPU Render instance).
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips='*'
