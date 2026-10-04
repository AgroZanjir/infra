#!/usr/bin/env bash
# Shared by the four operator scripts; no secrets are evaluated as shell code.
set -Eeuo pipefail
umask 077
ROOT=${DEPLOY_ROOT:-/opt/agrozanjir}
[[ "$ROOT" = /* && "$ROOT" != / && "$ROOT" != *..* ]] || { echo 'Invalid deployment root' >&2; exit 1; }
export DEPLOY_ROOT="$ROOT"

compose() {
  local app=${COMPOSE_APP:-}
  local args=(--project-name agrozanjir --env-file "$ROOT/runtime/compose.env" -f "$ROOT/compose.yaml")
  if [[ -n "$app" ]]; then
    [[ "$app" = backend || "$app" = frontend ]] || return 1
    args+=(--env-file "$ROOT/apps/$app/image.env" -f "$ROOT/apps/$app/compose.yaml")
  fi
  docker compose "${args[@]}" "$@"
}

lock_server() {
  mkdir -p "$ROOT/state"
  exec 9>"$ROOT/state/deploy.lock"
  flock -w 1800 9
}

install_bundle() {
  local stage=$1 app=${2:-}
  mkdir -p "$ROOT/scripts" "$ROOT/postgres" "$ROOT/runtime" "$ROOT/apps/backend" "$ROOT/apps/frontend"
  # App releases cannot revert shared infrastructure from an older checkout.
  # Shared files are installed only by bootstrap/deploy-infra.
  if [[ -z "$app" ]]; then
    install -m 644 "$stage/compose.yaml" "$ROOT/compose.yaml"
    install -m 644 "$stage/Caddyfile" "$ROOT/Caddyfile"
    install -m 644 "$stage/postgres/10-app.sh" "$ROOT/postgres/10-app.sh"
    chmod 755 "$ROOT/postgres"
    for file in "$stage"/scripts/*.sh "$stage"/scripts/*.py; do
      [[ -f "$file" ]] && install -m 750 "$file" "$ROOT/scripts/$(basename "$file")"
    done
  fi
  if [[ -n "$app" ]]; then
    install -m 644 "$stage/apps/$app/compose.yaml" "$ROOT/apps/$app/compose.yaml"
  fi
}

install_runtime() {
  local stage=$1
  # Directory permissions protect host files; readable bind-mounted secret files
  # are necessary for the stock postgres/redis images after they drop root.
  chmod 700 "$ROOT/runtime"
  for file in "$stage"/runtime/*; do
    [[ -f "$file" ]] && install -m 600 "$file" "$ROOT/runtime/$(basename "$file")"
  done
  for name in postgres_admin_password postgres_app_password redis_password redis.conf; do
    [[ ! -f "$ROOT/runtime/$name" ]] || chmod 644 "$ROOT/runtime/$name"
  done
  if [[ $(cat "$stage/runtime/backup-enabled") != true ]]; then
    rm -f "$ROOT/runtime/backup.json"
  fi
}
