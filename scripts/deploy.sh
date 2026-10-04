#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"
app=${1:?application is required}
stage=${2:?staging directory is required}
infra_sha=${3:?infra commit is required}
run_id=${4:?run id is required}
attempt=${5:?attempt is required}
[[ "$app" = backend || "$app" = frontend || "$app" = infra ]] || exit 1
[[ -d "$stage/runtime" && -d "$stage/scripts" ]] || exit 1
lock_server

if [[ "$app" = infra ]]; then
  mkdir -p "$stage/previous-shared"
  for file in compose.yaml Caddyfile; do
    [[ ! -f "$ROOT/$file" ]] || cp "$ROOT/$file" "$stage/previous-shared/$file"
  done
  for directory in runtime scripts postgres; do
    [[ ! -d "$ROOT/$directory" ]] || cp -a "$ROOT/$directory" "$stage/previous-shared/$directory"
  done
  # shellcheck disable=SC2317,SC2329 # Called indirectly by the ERR trap below.
  rollback_infra() {
    local status=$?
    trap - ERR
    echo 'Shared deployment failed; restoring previous configuration.' >&2
    for file in compose.yaml Caddyfile; do
      [[ ! -f "$stage/previous-shared/$file" ]] || cp "$stage/previous-shared/$file" "$ROOT/$file"
    done
    for directory in runtime scripts postgres; do
      if [[ -d "$stage/previous-shared/$directory" ]]; then
        rm -f "$ROOT/$directory"/*
        cp -a "$stage/previous-shared/$directory/." "$ROOT/$directory/"
      fi
    done
    COMPOSE_APP='' compose up -d --wait --wait-timeout 180 postgres redis caddy \
      || echo 'Shared recovery failed; inspect the server.' >&2
    exit "$status"
  }
  trap rollback_infra ERR
  install_bundle "$stage"
  install_runtime "$stage"
  COMPOSE_APP='' compose config --quiet
  COMPOSE_APP='' compose run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  COMPOSE_APP='' compose up -d --wait --wait-timeout 180 postgres redis caddy
  "$ROOT/scripts/backup.sh" --setup --locked
  trap - ERR
  echo 'Shared services and configuration applied. Redeploy backend to apply changed runtime variables.'
  exit 0
fi

candidate=$(python3 "$stage/scripts/server_state.py" image "$app" "$stage/apps/$app/image.env")
state="$ROOT/state/$app.json"
current=$(python3 "$stage/scripts/server_state.py" field "$state" current)
previous=$(python3 "$stage/scripts/server_state.py" field "$state" previous)

# Back up using the running database/config before replacing runtime secrets.
if [[ "$app" = backend && -n "$current" ]]; then
  BACKUP_CONFIG="$stage/runtime/backup.json" "$ROOT/scripts/backup.sh" --locked
fi

mkdir -p "$stage/previous/apps/$app"
for file in compose.yaml Caddyfile; do
  [[ ! -f "$ROOT/$file" ]] || cp "$ROOT/$file" "$stage/previous/$file"
done
for file in compose.yaml image.env; do
  [[ ! -f "$ROOT/apps/$app/$file" ]] || cp "$ROOT/apps/$app/$file" "$stage/previous/apps/$app/$file"
done
[[ ! -d "$ROOT/runtime" ]] || cp -a "$ROOT/runtime" "$stage/previous/runtime"

rollback() {
  local status=$?
  trap - ERR
  if [[ -f "$stage/public-health.headers" ]]; then
    echo 'Public HTTPS probe response (routing/security diagnostics):' >&2
    awk 'tolower($0) ~ /^(http\/|server:|content-type:|cf-ray:|cf-mitigated:|cf-error-type:|cf-error-origin:)/' \
      "$stage/public-health.headers" >&2
    if grep -qi '^cf-mitigated: challenge' "$stage/public-health.headers"; then
      echo 'Cloudflare challenged the public probe. Inspect the cf-ray in Cloudflare Security Events; curl cannot complete a browser challenge.' >&2
    fi
  fi
  echo "Deployment failed; restoring $app's previous application configuration." >&2
  if [[ -n "$current" ]]; then
    for file in compose.yaml Caddyfile; do
      [[ ! -f "$stage/previous/$file" ]] || cp "$stage/previous/$file" "$ROOT/$file"
    done
    for file in compose.yaml image.env; do
      [[ ! -f "$stage/previous/apps/$app/$file" ]] || cp "$stage/previous/apps/$app/$file" "$ROOT/apps/$app/$file"
    done
    if [[ -d "$stage/previous/runtime" ]]; then
      rm -f "$ROOT"/runtime/*
      cp -a "$stage/previous/runtime/." "$ROOT/runtime/"
    fi
    COMPOSE_APP="$app" compose up -d --no-deps --wait --wait-timeout 300 "$app" \
      && echo 'Previous application image restored. Database migrations were not reversed.' >&2 \
      || echo 'ROLLBACK FAILED: inspect the service on the server.' >&2
  else
    COMPOSE_APP="$app" compose stop "$app" || true
    echo 'First deployment failed; there is no previous image to restore.' >&2
  fi
  exit "$status"
}
trap rollback ERR

install_bundle "$stage" "$app"
install_runtime "$stage"
install -m 600 "$stage/apps/$app/image.env" "$ROOT/apps/$app/image.env"
export COMPOSE_APP="$app"
compose config --quiet
compose pull "$app"
compose run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
compose up -d --wait --wait-timeout 180 postgres redis caddy
"$ROOT/scripts/backup.sh" --setup --locked
compose up -d --no-deps --wait --wait-timeout 300 "$app"

domain=$(sed -n 's/^DOMAIN=//p' "$ROOT/runtime/compose.env")
if [[ "$app" = backend ]]; then
  curl --fail --silent --show-error --retry 12 --retry-delay 5 --retry-all-errors \
    --dump-header "$stage/public-health.headers" \
    --max-time 15 "https://$domain/api/v1/health/" >/dev/null
  curl --fail --silent --show-error --retry 3 --retry-all-errors --max-time 15 \
    --dump-header "$stage/public-health.headers" \
    "https://$domain/django-admin/login/" >/dev/null
  curl --fail --silent --show-error --max-time 15 --dump-header "$stage/public-health.headers" \
    "https://$domain/static/admin/css/base.css" >/dev/null
else
  curl --fail --silent --show-error --retry 12 --retry-delay 5 --retry-all-errors \
    --dump-header "$stage/public-health.headers" \
    --max-time 15 "https://$domain/healthz" >/dev/null
  curl --fail --silent --show-error --max-time 15 --dump-header "$stage/public-health.headers" \
    "https://$domain/admin/users" >/dev/null
fi

if [[ "$candidate" != "$current" ]]; then previous=$current; fi
python3 "$ROOT/scripts/server_state.py" receipt "$app" "$stage/receipt.json" \
  "$candidate" "$previous" "$infra_sha" "$run_id" "$attempt"
install -m 600 "$stage/receipt.json" "$state.tmp"
mv "$state.tmp" "$state"
trap - ERR
echo "$app passed readiness and external HTTPS checks."
