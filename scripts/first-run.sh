#!/usr/bin/env bash
#
# The first deployment, once.
#
#   sudo ./scripts/first-run.sh
#
# Creates the virtualenv, installs both halves, migrates, loads the reference
# catalogues, creates the accounts and prints their passwords. Read the README
# before running it - the settings in backend/.env have to be right first, and
# `check --deploy` will stop this script if they are not.

set -euo pipefail

ROOT=/srv/agrozanjir     # CHANGE ME
USER=agrozanjir
SERVICE=agrozanjir-api

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if [ ! -f "$ROOT/backend/.env" ]; then
  echo "No $ROOT/backend/.env. Copy env/backend.env.example there and fill it in." >&2
  exit 1
fi

say "Python environment"
sudo -u "$USER" python3 -m venv "$ROOT/backend/.venv"
sudo -u "$USER" "$ROOT/backend/.venv/bin/pip" install -q -U pip
sudo -u "$USER" "$ROOT/backend/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"

say "Checking the deployment settings"
sudo -u "$USER" "$ROOT/backend/.venv/bin/python" "$ROOT/backend/manage.py" check --deploy --fail-level WARNING

say "Database"
sudo -u "$USER" "$ROOT/backend/.venv/bin/python" "$ROOT/backend/manage.py" migrate --noinput
sudo -u "$USER" "$ROOT/backend/.venv/bin/python" "$ROOT/backend/manage.py" seed_reference
sudo -u "$USER" "$ROOT/backend/.venv/bin/python" "$ROOT/backend/manage.py" collectstatic --noinput

# The pilot dataset: illustrative figures, useful for a demonstration and not
# for a production database. Uncomment deliberately.
# sudo -u "$USER" "$ROOT/backend/.venv/bin/python" "$ROOT/backend/manage.py" seed_demo

say "Frontend"
sudo -u "$USER" bash -c "cd $ROOT/frontend && npm ci --silent && npm run build"
sudo -u "$USER" rm -rf "$ROOT/web"
sudo -u "$USER" cp -r "$ROOT/frontend/dist" "$ROOT/web"

say "Service"
cp "$(dirname "$0")/../systemd/agrozanjir-api.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now "$SERVICE"
sleep 2
curl -fsS http://127.0.0.1:8000/api/v1/health/ && echo

say "Accounts"
# Printed once, stored only as a hash. Write them down now; --rotate is the
# only way to get a password back, and it issues a new one.
sudo -u "$USER" "$ROOT/backend/.venv/bin/python" "$ROOT/backend/manage.py" seed_accounts

say "Now the web server: see the README, step 4"
