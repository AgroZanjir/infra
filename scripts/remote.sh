#!/usr/bin/env bash
# GitHub-runner transport: pinned SSH host keys, private staging, ephemeral GHCR auth.
# Client-side expansion is intentional: every inserted value is allowlisted below.
# shellcheck disable=SC2029
set -Eeuo pipefail
umask 077
mode=${1:?bootstrap or deploy is required}
app=${2:-infra}
[[ "$mode" = bootstrap || "$mode" = deploy ]] || exit 1
[[ "$app" = backend || "$app" = frontend || "$app" = infra ]] || exit 1
: "${VPS_HOST:?Set VPS_HOST}"
: "${VPS_USERNAME:?Set VPS_USERNAME}"
: "${VPS_SSH_KEY:?Set VPS_SSH_KEY}"
: "${VPS_SSH_KNOWN_HOSTS:?Supply independently verified VPS SSH host keys}"
: "${GITHUB_RUN_ID:?}"
: "${GITHUB_RUN_ATTEMPT:?}"
[[ "$VPS_HOST" =~ ^[a-zA-Z0-9:.%-]+$ && "$VPS_USERNAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ && "$VPS_USERNAME" != root ]] || exit 1
[[ "$GITHUB_RUN_ID" =~ ^[1-9][0-9]*$ && "$GITHUB_RUN_ATTEMPT" =~ ^[1-9][0-9]*$ ]] || exit 1
port=${VPS_SSH_PORT:-22}
[[ "$port" =~ ^[0-9]+$ && "$port" -gt 0 && "$port" -le 65535 ]] || exit 1
login=$VPS_USERNAME
if [[ "$mode" = bootstrap ]]; then
  login=${VPS_BOOTSTRAP_USERNAME:-$VPS_USERNAME}
  [[ "$login" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || exit 1
fi
work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/agrozanjir.XXXXXXXX")
stage="/tmp/agrozanjir-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
printf '%s\n' "$VPS_SSH_KEY" >"$work/key"
printf '%s\n' "$VPS_SSH_KNOWN_HOSTS" >"$work/known_hosts"
chmod 600 "$work/key" "$work/known_hosts"
ssh_opts=(-i "$work/key" -p "$port" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$work/known_hosts" -o ConnectTimeout=15)
cleanup() {
  ssh "${ssh_opts[@]}" "$login@$VPS_HOST" "rm -rf -- '$stage'" >/dev/null 2>&1 || true
  rm -rf -- "$work"
}
trap cleanup EXIT
if [[ "$mode" = bootstrap ]]; then
  python3 scripts/render_env.py "$work/runtime" --bootstrap
  ssh-keygen -y -f "$work/key" >"$work/deploy.pub"
else
  [[ "$GITHUB_SHA" =~ ^[a-f0-9]{40}$ ]] || exit 1
  python3 scripts/render_env.py "$work/runtime"
fi
mkdir "$work/docker-config"
if [[ "$mode" = deploy ]]; then
  : "${GHCR_TOKEN:?Infra requires packages read access to both app images}"
  export AUTH_DIRECTORY="$work/docker-config"
  python3 - <<'PY'
import base64, json, os
from pathlib import Path
auth = base64.b64encode((os.environ['GITHUB_ACTOR'] + ':' + os.environ['GHCR_TOKEN']).encode()).decode()
path = Path(os.environ['AUTH_DIRECTORY']) / 'config.json'
path.write_text(json.dumps({'auths': {'ghcr.io': {'auth': auth}}}))
path.chmod(0o600)
PY
fi
ssh "${ssh_opts[@]}" "$login@$VPS_HOST" "mkdir -m 700 -- '$stage'"
archive_files=(compose.yaml Caddyfile scripts postgres)
if [[ "$mode" = deploy && "$app" != infra ]]; then archive_files+=("apps/$app"); fi
local_files=(runtime docker-config)
if [[ "$mode" = bootstrap ]]; then local_files+=(deploy.pub); fi
tar -czf - "${archive_files[@]}" -C "$work" "${local_files[@]}" \
  | ssh "${ssh_opts[@]}" "$login@$VPS_HOST" "tar -xzf - -C '$stage'"
if [[ "$mode" = bootstrap ]]; then
  ssh "${ssh_opts[@]}" "$login@$VPS_HOST" \
    "sudo -n bash '$stage/scripts/bootstrap.sh' '$stage' '$VPS_USERNAME' '$stage/deploy.pub' '$port'"
  # Verify a second connection with the non-root deployment key before hardening SSH.
  ssh "${ssh_opts[@]}" "$VPS_USERNAME@$VPS_HOST" 'docker info --format "{{.ServerVersion}}" >/dev/null'
  ssh "${ssh_opts[@]}" "$VPS_USERNAME@$VPS_HOST" 'sudo -n /usr/local/sbin/agrozanjir-finalize-ssh'
  # Bootstrap made the private staging directory removable by the verified user.
  login=$VPS_USERNAME
else
  ssh "${ssh_opts[@]}" "$login@$VPS_HOST" \
    "export DOCKER_CONFIG='$stage/docker-config'; bash '$stage/scripts/deploy.sh' '$app' '$stage' '$GITHUB_SHA' '$GITHUB_RUN_ID' '$GITHUB_RUN_ATTEMPT'"
  if [[ "$app" != infra ]]; then
    mkdir -p deployment-receipt
    ssh "${ssh_opts[@]}" "$login@$VPS_HOST" "cat '$stage/receipt.json'" >"deployment-receipt/$app.json"
  fi
fi
