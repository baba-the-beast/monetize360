#!/bin/sh
# The app connects as m360_app (NOT the owner), so Row-Level Security and the
# no-UPDATE/DELETE rules on audit_log actually apply to it.
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "CREATE ROLE m360_app LOGIN PASSWORD '${APP_DB_PASSWORD:?set APP_DB_PASSWORD in .env}';"
