FROM python:3.12-slim

# UVICORN_RELOAD defaults to false. It used to default to true, baked into the
# image, so anything running this image without explicitly overriding it got the
# development auto-reloader -- which forks a reloader process and, per the note
# on CMD below, breaks the SIGTERM handling the graceful drain depends on. The
# dev default belongs in docker-compose.yml, which is where dev happens, not in
# an image someone might deploy.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UVICORN_RELOAD=false

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app

# Runs as a non-root user. The frontend's runner stage already dropped
# privileges; this one stayed root for no reason, so a process that only ever
# needs to read its own code and talk to Postgres and Redis ran as uid 0.
RUN useradd --system --create-home --uid 10001 inflight \
    && chown -R inflight:inflight /app
USER inflight

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health/live', timeout=2)" || exit 1

# --reload is opt-in via UVICORN_RELOAD=true, which docker-compose.yml sets for
# local development because it bind-mounts ./backend/app. It forks a separate
# reloader process, which complicates SIGTERM handling for the graceful-drain
# logic in lifecycle.py, so it stays off wherever that drain matters.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 $( [ \"$UVICORN_RELOAD\" = \"true\" ] && echo --reload )"]
