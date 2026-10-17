#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ "$#" -ne 2 ] || [ "$2" != "--confirm-replace-database" ]; then
    printf 'Usage: %s backup.dump --confirm-replace-database\n' "$0" >&2
    exit 2
fi
test -r "$1"
docker compose stop app worker
docker compose exec -T postgres pg_restore -U studio -d studio --clean --if-exists --no-owner --exit-on-error < "$1"
docker compose run --rm migrate
docker compose up -d app worker
