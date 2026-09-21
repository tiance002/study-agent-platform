#!/usr/bin/env bash
set -euo pipefail
umask 077

# shellcheck disable=SC1091
source /etc/study-plan/admin.env
backup_dir=/var/backups/study-plan
stamp=$(date -u +%Y%m%dT%H%M%SZ)
partial="$backup_dir/study-platform-$stamp.dump.partial"
final="$backup_dir/study-platform-$stamp.dump"

install -d -o root -g postgres -m 750 "$backup_dir"
pg_dump --format=custom --compress=6 --file="$partial" "$STUDY_PLATFORM_BACKUP_DSN"
pg_restore --list "$partial" >/dev/null
mv "$partial" "$final"
chown root:postgres "$final"
chmod 640 "$final"
find "$backup_dir" -type f -name 'study-platform-*.dump' -mtime +14 -delete
