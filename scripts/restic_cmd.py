#!/usr/bin/env python3
"""Pass JSON configuration directly to restic, never through a sourced env file."""
import json
import os
from pathlib import Path
import subprocess
import sys

config = Path(os.environ.get("BACKUP_CONFIG", "/opt/agrozanjir/runtime/backup.json"))
if not config.exists():
    print("Offsite backups disabled: missing or incomplete S3/restic configuration.")
    sys.exit(0)
settings = json.loads(config.read_text())
required = ("RESTIC_REPOSITORY", "RESTIC_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
if not all(settings.get(key) for key in required):
    print("Offsite backups disabled: missing or incomplete S3/restic configuration.")
    sys.exit(0)
env = os.environ | {key: value for key, value in settings.items() if key in (*required, "AWS_DEFAULT_REGION")}
arguments = sys.argv[1:]
if arguments == ["init-if-needed"]:
    result = subprocess.run(["restic", "snapshots", "--json"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode == 0:
        sys.exit(0)
    # init is safe on an existing repository: it refuses to overwrite it.
    arguments = ["init"]
sys.exit(subprocess.run(["restic", *arguments], env=env).returncode)
