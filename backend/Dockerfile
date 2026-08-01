FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UVICORN_RELOAD=true

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

# --reload defaults on because docker-compose.yml bind-mounts ./backend/app for
# local development. It forks a separate reloader process, which complicates
# SIGTERM handling for the graceful-drain logic in lifecycle.py, so anything
# running this image outside that compose setup should set
# UVICORN_RELOAD=false to get a plain single-process worker.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 $( [ \"$UVICORN_RELOAD\" = \"true\" ] && echo --reload )"]
