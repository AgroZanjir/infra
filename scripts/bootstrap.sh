#!/usr/bin/env bash
set -Eeuo pipefail
bootstrap_error() {
  local status=$1 file=$2 line=$3
  trap - ERR
  printf 'Bootstrap failed at %s:%s (exit %s).\n' "$file" "$line" "$status" >&2
  exit "$status"
}
trap 'bootstrap_error "$?" "${BASH_SOURCE[0]}" "$LINENO"' ERR
source "$(dirname "$0")/common.sh"
stage=${1:?staging directory is required}
username=${2:?deployment username is required}
ssh_port=${3:-22}
[[ $EUID = 0 ]] || { echo 'Bootstrap requires root/passwordless sudo.' >&2; exit 1; }
[[ "$username" =~ ^[a-z_][a-z0-9_-]{0,31}$ && "$username" != root ]] || exit 1
[[ "$ssh_port" =~ ^[0-9]+$ && "$ssh_port" -gt 0 && "$ssh_port" -le 65535 ]] || exit 1
# Account, authorized keys and sudo access are provisioned manually by the operator.
id "$username" >/dev/null 2>&1 || {
  echo "Create the deployment user '$username' and configure key-based SSH/passwordless sudo before bootstrap." >&2
  exit 1
}
[[ $(id -u "$username") != 0 ]] || { echo 'Deployment user must be non-root.' >&2; exit 1; }
deploy_group=$(id -gn "$username")
# The dedicated host and shared PostgreSQL are provisioned by the operator.
# Bootstrap prepares application storage and timers without reinstalling Docker
# or changing host networking, firewall, SSH, or APT repositories.
for command in docker python3 flock getent; do
  command -v "$command" >/dev/null || { echo "Missing prerequisite: $command" >&2; exit 1; }
done
docker info >/dev/null
docker compose version
python3 - "$stage/runtime/compose.env" <<'PYTHON'
from pathlib import Path
import subprocess
import sys
settings = dict(line.split("=", 1) for line in Path(sys.argv[1]).read_text().splitlines())
subprocess.run(["docker", "network", "inspect", settings["DB_NETWORK"]], check=True,
               stdout=subprocess.DEVNULL)
PYTHON
if [[ -f "$stage/runtime/backup.json" ]]; then
  command -v restic >/dev/null || { echo 'Install restic before enabling offsite backups.' >&2; exit 1; }
fi

usermod -aG docker "$username"
if media_entry=$(getent group 10001); then
  media_group=${media_entry%%:*}
else
  lookup_status=$?
  if [[ "$lookup_status" != 2 ]]; then
    echo "Media group lookup failed with exit code $lookup_status." >&2
    exit "$lookup_status"
  fi
  groupadd --gid 10001 agrozanjir-data
  media_group=agrozanjir-data
fi
usermod -aG "$media_group" "$username"

install -d -m 750 -o "$username" -g "$deploy_group" "$ROOT" "$ROOT/scripts" "$ROOT/state" "$ROOT/apps" \
  "$ROOT/apps/backend" "$ROOT/apps/frontend" "$ROOT/postgres" "$ROOT/data"
install -d -m 700 -o "$username" -g "$deploy_group" "$ROOT/runtime" "$ROOT/data/backup-work"
# Container IDs need no matching host passwd entry. Assign them numerically after
# creating the directories; install --owner requires a resolvable host user.
install -d -m 700 "$ROOT/data/media" "$ROOT/data/caddy" "$ROOT/data/caddy-config"
chown +10001:+10001 "$ROOT/data/media" "$ROOT/data/caddy" "$ROOT/data/caddy-config"
chmod 2770 "$ROOT/data/media"
lock_server
install_bundle "$stage"
install_runtime "$stage"
chown -R "$username:$deploy_group" "$ROOT/scripts" "$ROOT/postgres" "$ROOT/runtime" "$ROOT/state" "$ROOT/apps"
chown "$username:$deploy_group" "$ROOT/compose.yaml" "$ROOT/Caddyfile"
chmod 755 "$ROOT/postgres"

cat >/etc/systemd/system/agrozanjir-backup.service <<UNIT
[Unit]
Description=Encrypted AgroZanjir PostgreSQL and media backup
After=docker.service
ConditionPathExists=$ROOT/runtime/backup.json
[Service]
Type=oneshot
User=$username
SupplementaryGroups=$media_group docker
ExecStart=$ROOT/scripts/backup.sh
UMask=0077
UNIT
cat >/etc/systemd/system/agrozanjir-backup.timer <<'UNIT'
[Unit]
Description=Daily AgroZanjir backup
[Timer]
OnCalendar=*-*-* 02:30:00 Asia/Tashkent
Persistent=true
[Install]
WantedBy=timers.target
UNIT
cat >/etc/systemd/system/agrozanjir-backup-check.service <<UNIT
[Unit]
Description=Verify AgroZanjir offsite backup integrity
After=agrozanjir-backup.service
ConditionPathExists=$ROOT/runtime/backup.json
[Service]
Type=oneshot
User=$username
ExecStart=/usr/bin/python3 $ROOT/scripts/restic_cmd.py check
UMask=0077
UNIT
cat >/etc/systemd/system/agrozanjir-backup-check.timer <<'UNIT'
[Unit]
Description=Weekly AgroZanjir backup verification
[Timer]
OnCalendar=Sun *-*-* 03:30:00 Asia/Tashkent
Persistent=true
[Install]
WantedBy=timers.target
UNIT
cat >"$ROOT/scripts/maintenance.sh" <<'MAINTENANCE'
#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"
lock_server
[[ -f "$ROOT/apps/backend/image.env" ]] || exit 0
COMPOSE_APP=backend compose exec -T backend python manage.py flushexpiredtokens
MAINTENANCE
chmod 750 "$ROOT/scripts/maintenance.sh"
chown "$username:$deploy_group" "$ROOT/scripts/maintenance.sh"
cat >/etc/systemd/system/agrozanjir-maintenance.service <<UNIT
[Unit]
Description=Expire AgroZanjir refresh-token blacklist entries
After=docker.service
[Service]
Type=oneshot
User=$username
SupplementaryGroups=docker
ExecStart=$ROOT/scripts/maintenance.sh
UNIT
cat >/etc/systemd/system/agrozanjir-maintenance.timer <<'UNIT'
[Unit]
Description=Daily AgroZanjir token maintenance
[Timer]
OnCalendar=*-*-* 03:00:00 Asia/Tashkent
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now agrozanjir-backup.timer agrozanjir-backup-check.timer agrozanjir-maintenance.timer
"$ROOT/scripts/backup.sh" --setup --locked
docker compose version
chown -R "$username:$deploy_group" "$stage"
echo 'Bootstrap prepared application storage and maintenance on the existing Docker host.'
