"""Publish successful state or request a rollback through the normal image trigger."""
import argparse
import json
import hashlib
import os
import subprocess
from pathlib import Path

from github_api import GitHub, desired_line, read_desired, validate_receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("publish", "rollback", "assert-desired"):
        sub = commands.add_parser(command)
        sub.add_argument("--app", choices=("backend", "frontend"), required=True)
        if command == "publish":
            sub.add_argument("--receipt", required=True)
        elif command == "assert-desired":
            sub.add_argument("--image", required=True)
            sub.add_argument("--compose-file")
    commands.add_parser("assert-infra")
    args = parser.parse_args()
    client = GitHub(os.environ["GH_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    if args.command == "assert-infra":
        tree = client.request(client.base + "/git/trees/main?recursive=1")
        if tree.get("truncated"):
            raise RuntimeError("Cannot validate a truncated infra tree")
        def managed(path):
            return path in ("compose.yaml", "Caddyfile") or path.startswith(("scripts/", "postgres/"))
        expected = {item["path"]: item["sha"] for item in tree["tree"] if item["type"] == "blob" and managed(item["path"])}
        actual = {}
        tracked = subprocess.check_output(["git", "ls-files", "-z"], text=True).split("\0")
        for name in filter(managed, tracked):
            data = Path(name).read_bytes()
            actual[name] = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if actual != expected:
            raise RuntimeError("Shared infrastructure was superseded; run deploy-infra from current main")
        print("Shared infrastructure matches current main.")
        return
    desired = read_desired(client, args.app)
    if args.command == "assert-desired":
        if desired != args.image:
            raise RuntimeError("This image has been superseded in infra/main; refusing a stale deployment")
        if args.compose_file and Path(args.compose_file).read_text() != client.file(f"apps/{args.app}/compose.yaml")[0]:
            raise RuntimeError("This app's Compose fragment was superseded; refusing a stale deployment")
        print("Desired image is current.")
    elif args.command == "publish":
        receipt = validate_receipt(json.loads(Path(args.receipt).read_text()), args.app)
        for key, env in (("infra_sha", "GITHUB_SHA"), ("infra_run_id", "GITHUB_RUN_ID"), ("infra_run_attempt", "GITHUB_RUN_ATTEMPT")):
            if str(receipt[key]) != os.environ[env]:
                raise ValueError("Receipt does not match this infra workflow attempt")
        # A newer desired commit can queue while this deployment finishes. The actual
        # successful state must still be published; it is independent of desired state.
        commit = client.update_file(f"apps/{args.app}/deployed.json", json.dumps(receipt, indent=2) + "\n",
                                    f"state({args.app}): deployed {receipt['source_sha']}")
        print(f"Recorded successful deployment at {commit}.")
    else:
        state = validate_receipt(json.loads(client.file(f"apps/{args.app}/deployed.json")[0]), args.app)
        if desired == state["current"] and not state["previous"]:
            raise RuntimeError("No previous successful release exists for this application")
        # After an automatic recovery, restore Git's desired state to the image that
        # is already working. Otherwise roll back one successful release.
        target = state["current"] if desired != state["current"] else state["previous"]
        commit = client.update_file(f"apps/{args.app}/image.env", desired_line(args.app, target),
                                    f"rollback({args.app}): request retained release", expected=desired_line(args.app, desired))
        print(f"Rollback requested at {commit}; deploy-{args.app} will perform the deployment.")


if __name__ == "__main__":
    main()
