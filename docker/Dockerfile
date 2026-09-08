# Server image. The client is NOT containerised -- players install it with pip.
#
# Multi-stage: the builder compiles wheels (argon2-cffi and friends need a
# toolchain), the runtime image carries none of it.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 # postgres extra is included so the PostgreSQL overlay works with no rebuild
 && /opt/venv/bin/pip install ".[server,postgres]"


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    STOCKGAME_HOST=0.0.0.0 \
    STOCKGAME_PORT=8765 \
    STOCKGAME_DATA_DIR=/data \
    STOCKGAME_DATABASE_URL=sqlite+aiosqlite:////data/stockgame.sqlite3

# Run as an unprivileged user; /data is the only writable path it needs.
RUN groupadd --system --gid 1000 stockgame \
 && useradd --system --uid 1000 --gid stockgame --create-home stockgame \
 && mkdir -p /data && chown stockgame:stockgame /data

COPY --from=builder /opt/venv /opt/venv
COPY --chown=stockgame:stockgame migrations /app/migrations
COPY --chown=stockgame:stockgame alembic.ini /app/alembic.ini
COPY --chown=stockgame:stockgame docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /app
USER stockgame
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["stockgame-server"]
