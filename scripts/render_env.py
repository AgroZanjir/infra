#!/usr/bin/env python3
"""Render allowlisted configuration without printing secrets or shell evaluation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

ROOT = "/opt/agrozanjir"
BACKEND_DEFAULTS = {
    "DEBUG": "False", "ONEID_ADAPTER": "disabled",
    "SEEDED_PASSWORDS_ROTATED": "False",
    "GUNICORN_WORKERS": "2", "GUNICORN_THREADS": "4",
    "GUNICORN_TIMEOUT": "120", "GUNICORN_GRACEFUL_TIMEOUT": "30",
    "STARTUP_TIMEOUT": "60",
    "ASSISTANT_ADAPTER": "auto", "REFRESH_COOKIE_SECURE": "True",
    "REFRESH_COOKIE_SAMESITE": "Lax", "SECURE_SSL_REDIRECT": "True",
}
OPTIONAL_BACKEND = (
    "ASSISTANT_MODEL", "ASSISTANT_EFFORT", "ASSISTANT_PANEL_EFFORT",
    "ASSISTANT_MAX_TOKENS", "ASSISTANT_FALLBACKS", "ASSISTANT_BURST_RATE",
    "ASSISTANT_HOUR_RATE", "ASSISTANT_PANEL_RATE", "ENQUIRY_BURST_RATE",
    "ENQUIRY_DAY_RATE", "SIGNIN_ADDRESS_RATE", "SIGNIN_ACCOUNT_RATE",
    "ACCESS_TOKEN_MINUTES", "REFRESH_TOKEN_DAYS", "SECURE_HSTS_SECONDS",
)

def value(env, name, default=""):
    result = env.get(name) or default
    if any(c in result for c in "\r\n\x00"):
        raise ValueError(f"{name} must be a single-line value")
    return result

def write(path, content):
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o600)

def render(directory: Path, env, *, bootstrap=False):
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    domain = value(env, "DOMAIN", "agrozanjir.uz")
    if not re.fullmatch(r"(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", domain):
        raise ValueError("DOMAIN must be a hostname")
    write(directory / "compose.env", f"DEPLOY_ROOT={ROOT}\nDOMAIN={domain}\n")
    backup_names = ("RESTIC_REPOSITORY", "RESTIC_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    configured = all(value(env, name) for name in backup_names)
    backup = {name: value(env, name) for name in backup_names} if configured else {}
    if configured:
        if not backup["RESTIC_REPOSITORY"].startswith("s3:"):
            raise ValueError("RESTIC_REPOSITORY must use the s3: backend")
        backup["AWS_DEFAULT_REGION"] = value(env, "AWS_DEFAULT_REGION", "us-east-1")
        write(directory / "backup.json", json.dumps(backup))
    write(directory / "backup-enabled", "true\n" if configured else "false\n")
    if not configured:
        (directory / "backup.json").unlink(missing_ok=True)
        print("Offsite backups disabled: missing or incomplete S3/restic configuration.")
    if bootstrap:
        return
    required = ("DJANGO_SECRET_KEY", "POSTGRES_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD")
    for name in required:
        if not value(env, name):
            raise ValueError(f"Required production secret is missing: {name}")
    if len(value(env, "DJANGO_SECRET_KEY")) < 50:
        raise ValueError("DJANGO_SECRET_KEY must contain at least 50 characters")
    for name in ("POSTGRES_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD"):
        if len(value(env, name)) < 20:
            raise ValueError(f"{name} must contain at least 20 characters")
    for filename, name in (("postgres_admin_password", "POSTGRES_ADMIN_PASSWORD"),
                           ("postgres_app_password", "POSTGRES_PASSWORD"),
                           ("redis_password", "REDIS_PASSWORD")):
        write(directory / filename, value(env, name))
    # Redis configuration's quoted strings accept JSON escapes for these values.
    redis_password = json.dumps(value(env, "REDIS_PASSWORD"), ensure_ascii=True)
    write(directory / "redis.conf", "bind 0.0.0.0\nprotected-mode yes\nappendonly yes\ndir /data\nrequirepass " + redis_password + "\n")
    settings = {name: value(env, name, default) for name, default in BACKEND_DEFAULTS.items()}
    settings.update({
        "DJANGO_SECRET_KEY": value(env, "DJANGO_SECRET_KEY"),
        "ALLOWED_HOSTS": f"{domain},localhost,127.0.0.1",
        "CORS_ALLOWED_ORIGINS": "", "CSRF_TRUSTED_ORIGINS": f"https://{domain}",
        "DATABASE_URL": "postgresql://agrozanjir:" + quote(value(env, "POSTGRES_PASSWORD"), safe="") + "@postgres:5432/agrozanjir",
        "CACHE_URL": "redis://:" + quote(value(env, "REDIS_PASSWORD"), safe="") + "@redis:6379/1",
        "ANTHROPIC_API_KEY": value(env, "ANTHROPIC_API_KEY"),
    })
    for name in OPTIONAL_BACKEND:
        if value(env, name):
            settings[name] = value(env, name)
    write(directory / "backend.env", "".join(f"{name}={v}\n" for name, v in settings.items()))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    try:
        render(args.directory, os.environ, bootstrap=args.bootstrap)
    except ValueError as exc:
        parser.exit(1, f"Configuration error: {exc}\n")
