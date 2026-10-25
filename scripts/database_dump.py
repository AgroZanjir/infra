"""Back up or restore only the application database in shared PostgreSQL."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    database = json.loads(Path(sys.argv[1]).read_text())
    # Local PostgreSQL administrator access stays inside the shared container.
    command = ["docker", "exec", "-i", "--user", "postgres", database["container"]]
    if len(sys.argv) == 3 and sys.argv[2] == "--restore":
        command += ["pg_restore", "-U", "postgres", "--dbname=" + database["name"],
                    "--role=" + database["user"], "--clean", "--if-exists", "--no-owner",
                    "--exit-on-error"]
    elif len(sys.argv) == 2:
        command += ["pg_dump", "-U", "postgres", "--dbname=" + database["name"], "--format=custom"]
    else:
        raise ValueError("Expected database config and optional --restore")
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
