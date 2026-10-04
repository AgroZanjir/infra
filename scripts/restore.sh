#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"
snapshot=${1:?restic snapshot id (or latest) is required}
[[ ${2:-} = --confirm-restore ]] || { echo 'Restore replaces database and media. Supply --confirm-restore.' >&2; exit 1; }
[[ $EUID = 0 ]] || { echo 'Run restore with sudo.' >&2; exit 1; }
[[ -f "$ROOT/runtime/backup.json" ]] || { echo 'Offsite backups are not configured.' >&2; exit 1; }
[[ "$snapshot" = latest || "$snapshot" =~ ^[a-f0-9]{8,64}$ ]] || exit 1
lock_server
export BACKUP_CONFIG="$ROOT/runtime/backup.json"
work=$(mktemp -d "$ROOT/data/restore.XXXXXXXX")
trap 'echo "Restore workspace: $work (remove after verification)."' EXIT
python3 "$ROOT/scripts/restic_cmd.py" restore "$snapshot" --tag agrozanjir --target "$work"
mapfile -t dumps < <(find "$work" -type f -name postgres.dump)
[[ ${#dumps[@]} = 1 ]] || { echo 'Snapshot must contain one PostgreSQL dump.' >&2; exit 1; }
media="$work$ROOT/data/media"
[[ -d "$media" ]] || { echo 'Snapshot does not contain the media directory.' >&2; exit 1; }
backend_present=false
if [[ -f "$ROOT/apps/backend/image.env" ]] && python3 "$ROOT/scripts/server_state.py" image backend "$ROOT/apps/backend/image.env" >/dev/null 2>&1; then
  backend_present=true
  COMPOSE_APP=backend compose stop backend
fi
COMPOSE_APP='' compose exec -T --user postgres postgres pg_restore -U agrozanjir -d agrozanjir \
  --clean --if-exists --no-owner --exit-on-error <"${dumps[0]}"
rsync -a --delete --chown=10001:10001 "$media/" "$ROOT/data/media/"
if $backend_present; then
  COMPOSE_APP=backend compose up -d --no-deps --wait --wait-timeout 300 backend
fi
echo 'Restore completed. Verify application records and uploaded files before deleting the restore workspace.'
