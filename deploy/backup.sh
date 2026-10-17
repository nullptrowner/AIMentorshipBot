#!/bin/sh
set -eu
umask 077
cd "$(dirname "$0")/.."
backup_dir=${1:-./data/backups}
mkdir -p "$backup_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
temporary="$backup_dir/studio-$stamp.dump.tmp"
target="$backup_dir/studio-$stamp.dump"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
docker compose exec -T postgres pg_dump -U studio -d studio --format=custom > "$temporary"
test -s "$temporary"
mv "$temporary" "$target"
printf 'Backup created: %s\n' "$target"
# Copy the dump to your configured off-server backup destination after this succeeds.
