#!/bin/sh
# Apply any pending migrations, then hand over to the server.
#
# Running migrations here (rather than in the app) keeps schema changes an
# explicit, observable step, and means a failed migration stops the container
# instead of a half-migrated server accepting trades.
set -e

if [ "${STOCKGAME_SKIP_MIGRATIONS:-0}" != "1" ]; then
  echo "==> applying database migrations"
  alembic -c /app/alembic.ini upgrade head
fi

if [ -z "${STOCKGAME_SECRET_KEY:-}" ]; then
  echo "WARNING: STOCKGAME_SECRET_KEY is not set." >&2
  echo "         Sessions will be invalidated on every restart." >&2
fi

echo "==> starting: $*"
exec "$@"
