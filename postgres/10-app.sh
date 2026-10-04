#!/bin/sh
set -eu
# psql variables quote arbitrary passwords safely; never concatenate SQL.
app_password=$(cat /run/secrets/postgres_app_password)
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set=app_password="$app_password" <<'SQL'
CREATE ROLE agrozanjir LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE DATABASE agrozanjir OWNER agrozanjir;
\connect agrozanjir
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO agrozanjir;
SQL
