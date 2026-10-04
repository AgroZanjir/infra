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
# shellcheck source=/dev/null
source /etc/os-release
[[ "$ID" = ubuntu ]] || { echo 'Bootstrap supports Ubuntu only.' >&2; exit 1; }
case "$VERSION_ID" in 22.04|24.04|26.04) ;; *) echo 'Use a Docker-supported Ubuntu LTS release.' >&2; exit 1 ;; esac
[[ $(dpkg --print-architecture) = amd64 ]] || { echo 'This deployment targets amd64.' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 jq rsync \
  openssh-server ufw restic util-linux sudo iproute2 iptables tzdata
install -d -m 755 /etc/apt/keyrings
curl --fail --silent --show-error https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
chmod 644 /etc/apt/keyrings/docker.gpg
printf 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n' \
  "$VERSION_CODENAME" >/etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

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

install -d -m 755 /etc/docker
python3 - <<'PY'
import json
from pathlib import Path
path = Path('/etc/docker/daemon.json')
settings = json.loads(path.read_text()) if path.exists() else {}
settings.update({'ipv6': True, 'ip6tables': True, 'live-restore': True})
settings.setdefault('fixed-cidr-v6', 'fd42:7a:1::/64')
candidate = path.with_suffix('.candidate')
candidate.write_text(json.dumps(settings, indent=2) + '\n')
PY
dockerd --validate --config-file /etc/docker/daemon.candidate
if ! cmp -s /etc/docker/daemon.candidate /etc/docker/daemon.json; then
  [[ ! -f /etc/docker/daemon.json ]] || cp /etc/docker/daemon.json /etc/docker/daemon.agrozanjir-before.json
  mv /etc/docker/daemon.candidate /etc/docker/daemon.json
  systemctl restart docker
else
  rm -f /etc/docker/daemon.candidate
fi
systemctl enable --now docker

cat >/etc/sysctl.d/60-agrozanjir.conf <<'SYSCTL'
net.core.rmem_max=7500000
net.core.wmem_max=7500000
net.ipv6.conf.all.forwarding=1
net.ipv6.conf.default.forwarding=1
SYSCTL
# Preserve provider router-advertisement routes when enabling forwarding.
while read -r interface; do
  [[ "$interface" =~ ^[a-zA-Z0-9_.:-]+$ ]] || exit 1
  printf 'net.ipv6.conf.%s.accept_ra=2\n' "$interface" >>/etc/sysctl.d/60-agrozanjir.conf
done < <(ip -6 route show default | awk '/proto ra/ {for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | sort -u)
sysctl --system >/dev/null
sed -i 's/^IPV6=.*/IPV6=yes/' /etc/default/ufw
ufw allow "$ssh_port/tcp" comment 'Deployment SSH'
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw default deny incoming
ufw default allow outgoing
ufw --force enable

# Docker's forwarded packets bypass UFW. Filter its ingress explicitly.
cat >/usr/local/sbin/agrozanjir-docker-firewall <<'FIREWALL'
#!/usr/bin/env bash
set -Eeuo pipefail
for table in iptables ip6tables; do
  command -v "$table" >/dev/null
  "$table" -w -N AGROZANJIR-IN 2>/dev/null || true
  "$table" -w -F AGROZANJIR-IN
  "$table" -w -A AGROZANJIR-IN -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  "$table" -w -A AGROZANJIR-IN -p tcp -m multiport --dports 80,443 -j RETURN
  "$table" -w -A AGROZANJIR-IN -p udp --dport 443 -j RETURN
  if [[ "$table" = ip6tables ]]; then
    "$table" -w -A AGROZANJIR-IN -p ipv6-icmp -j RETURN
  else
    "$table" -w -A AGROZANJIR-IN -p icmp -j RETURN
  fi
  "$table" -w -A AGROZANJIR-IN -j DROP
  "$table" -w -N DOCKER-USER 2>/dev/null || true
  while read -r interface; do
    [[ "$interface" =~ ^[a-zA-Z0-9_.:-]+$ ]] || exit 1
    "$table" -w -C DOCKER-USER -i "$interface" -j AGROZANJIR-IN 2>/dev/null \
      || "$table" -w -I DOCKER-USER 1 -i "$interface" -j AGROZANJIR-IN
  done < <({ ip -4 route show default; ip -6 route show default; } | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | sort -u)
done
FIREWALL
chmod 755 /usr/local/sbin/agrozanjir-docker-firewall
mkdir -p /etc/systemd/system/docker.service.d
cat >/etc/systemd/system/docker.service.d/agrozanjir-firewall.conf <<'UNIT'
[Service]
ExecStartPost=/usr/local/sbin/agrozanjir-docker-firewall
UNIT
/usr/local/sbin/agrozanjir-docker-firewall

# Keep the operator's root key login available, matching the manual-user setup.
cat >/etc/ssh/sshd_config.d/00-agrozanjir.conf <<'CONFIG'
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
CONFIG
/usr/sbin/sshd -t
systemctl reload ssh

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
echo 'Bootstrap prepared Docker, IPv6, QUIC ports, persistence and maintenance. No application image was required.'
