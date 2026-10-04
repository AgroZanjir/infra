#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"
locked=false
setup=false
for argument in "$@"; do
  case "$argument" in --locked) locked=true ;; --setup) setup=true ;; *) exit 1 ;; esac
done
$locked || lock_server
export BACKUP_CONFIG=${BACKUP_CONFIG:-$ROOT/runtime/backup.json}
if [[ ! -f "$BACKUP_CONFIG" ]]; then
  echo 'Offsite backups disabled: missing or incomplete S3/restic configuration.'
  exit 0
fi
exec 8>"$ROOT/state/backup.lock"
flock -w 1800 8
restic_cmd() { python3 "$ROOT/scripts/restic_cmd.py" "$@"; }
if $setup; then
  restic_cmd init-if-needed
  echo 'Encrypted offsite backup repository is ready.'
  exit 0
fi

mkdir -p "$ROOT/data/backup-work"
work=$(mktemp -d "$ROOT/data/backup-work/run.XXXXXXXX")
trap 'rm -f "$work/postgres.dump"; rmdir "$work"' EXIT
COMPOSE_APP='' compose exec -T --user postgres postgres \
  pg_dump -U agrozanjir -d agrozanjir --format custom >"$work/postgres.dump"
[[ -s "$work/postgres.dump" ]] || { echo 'Database dump is empty' >&2; exit 1; }
restic_cmd backup --tag agrozanjir "$work/postgres.dump" "$ROOT/data/media"
restic_cmd forget --tag agrozanjir --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
echo 'Encrypted PostgreSQL and media backup completed.'
