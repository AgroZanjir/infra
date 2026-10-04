#!/usr/bin/env python3
"""Strict image parsing and atomic, secret-free successful deployment receipts."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

def reference(app, text):
    pattern = rf"ghcr\.io/agrozanjir/{app}:{app}-([a-f0-9]{{40}})-[1-9][0-9]*-[1-9][0-9]*@sha256:[a-f0-9]{{64}}"
    match = re.fullmatch(pattern, text)
    if not match:
        raise ValueError(f"Invalid immutable {app} image reference")
    return match.group(1)

def image(app, path):
    lines = [line.strip() for line in Path(path).read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    key = app.upper() + "_IMAGE="
    if len(lines) != 1 or not lines[0].startswith(key):
        raise ValueError("Image file must contain exactly its expected IMAGE assignment")
    result = lines[0][len(key):]
    reference(app, result)
    return result

def load(path):
    return json.loads(Path(path).read_text()) if Path(path).exists() else {}

def save(path, data):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("image")
    validate.add_argument("app", choices=("backend", "frontend"))
    validate.add_argument("path")
    field = sub.add_parser("field")
    field.add_argument("path")
    field.add_argument("name", choices=("current", "previous"))
    receipt = sub.add_parser("receipt")
    receipt.add_argument("app", choices=("backend", "frontend"))
    receipt.add_argument("path")
    receipt.add_argument("image")
    receipt.add_argument("previous")
    receipt.add_argument("infra_sha")
    receipt.add_argument("run_id")
    receipt.add_argument("attempt")
    args = parser.parse_args()
    try:
        if args.command == "image":
            print(image(args.app, args.path))
        elif args.command == "field":
            print(load(args.path).get(args.name) or "")
        else:
            sha = reference(args.app, args.image)
            if args.previous:
                reference(args.app, args.previous)
            if not re.fullmatch(r"[a-f0-9]{40}", args.infra_sha) or not args.run_id.isdecimal() or not args.attempt.isdecimal():
                raise ValueError("Invalid workflow identity")
            save(args.path, {
                "schema": 1, "app": args.app, "current": args.image,
                "previous": args.previous or None, "release_id": args.image,
                "source_sha": sha, "infra_sha": args.infra_sha,
                "infra_run_id": args.run_id, "infra_run_attempt": args.attempt,
                "deployed_at": datetime.now(timezone.utc).isoformat(),
            })
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"State error: {exc}\n")
