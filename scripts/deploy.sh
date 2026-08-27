#!/usr/bin/env bash
#
# Every deployment after the first.
#
#   sudo ./scripts/deploy.sh
#
# Pulls both repositories, installs what changed, migrates, rebuilds the bundle
# and restarts the API. It does not touch the seeds: `seed_demo` would refuse
# on a database that already has lots, and `seed_accounts` does nothing without
# --rotate.

set -euo pipefail

# CHANGE ME if the tree does not live here.
ROOT=/srv/agrozanjir
SERVICE=agrozanjir-api
USER=agrozanjir

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

# The event log is append-only and hash-chained: it is the record the whole
# platform's credibility rests on, and it cannot be reconstructed. Take the
# backup before anything else, and stop if it fails.
say "Backing up the database"
BACKUP="$ROOT/backups/$(date +%Y-%m-%d-%H%M).sql.gz"
mkdir -p "$ROOT/backups"
sudo -u "$USER" bash -c "set -a; source $ROOT/backend/.env; set +a; pg_dump \"\$DATABASE_URL\"" | gzip > "$BACKUP"
echo "    $BACKUP"

say "Pulling the backend"
sudo -u "$USER" git -C "$ROOT/backend" pull --ff-only

say "Installing backend dependencies"
sudo -u "$USER" "$ROOT/backend/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"

say "Checking the deployment settings"
# The gate. If this reports anything, nothing below should run.
sudo -u "$USER" "$ROOT/backend/.venv/bin/python" "$ROOT/backend/manage.py" check --deploy --fail-level WARNING

say "Migrating"
sudo -u "$USER" "$ROOT/backend/.venv/bin/python" "$ROOT/backend/manage.py" migrate --noinput

say "Reference data"
# Idempotent, and it carries the product's own catalogues: ten capabilities,
# thirty-seven roles, thirteen organisation types, six verification checks. A
# label edited upstream reaches production here, without a migration.
sudo -u "$USER" "$ROOT/backend/.venv/bin/python" "$ROOT/backend/manage.py" seed_reference

say "Collecting static files"
sudo -u "$USER" "$ROOT/backend/.venv/bin/python" "$ROOT/backend/manage.py" collectstatic --noinput

say "Pulling the frontend"
sudo -u "$USER" git -C "$ROOT/frontend" pull --ff-only

say "Building the bundle"
# VITE_API_BASE_URL is inlined at build time. It comes from the frontend's
# .env, so that file is what to edit if the API moves - not this script.
sudo -u "$USER" bash -c "cd $ROOT/frontend && npm ci --silent && npm run build"

say "Publishing the bundle"
# Built into a temporary directory and moved into place, so a reader never
# meets a half-copied bundle.
sudo -u "$USER" rm -rf "$ROOT/web.new"
sudo -u "$USER" cp -r "$ROOT/frontend/dist" "$ROOT/web.new"
sudo -u "$USER" rm -rf "$ROOT/web.old"
if [ -d "$ROOT/web" ]; then sudo -u "$USER" mv "$ROOT/web" "$ROOT/web.old"; fi
sudo -u "$USER" mv "$ROOT/web.new" "$ROOT/web"

say "Restarting the API"
systemctl restart "$SERVICE"
sleep 2
systemctl --no-pager --lines=0 status "$SERVICE"

say "Health"
curl -fsS http://127.0.0.1:8000/api/v1/health/ && echo

say "Done"
