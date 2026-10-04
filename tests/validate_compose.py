"""Validate bootstrap base and each app model using disposable synthetic configuration."""
import os
from pathlib import Path
import subprocess
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_safety import ENV, REPO, ref, render


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        render(root / "runtime", ENV)
        compose_env = root / "runtime/compose.env"
        compose_env.write_text(f"DEPLOY_ROOT={root.as_posix()}\nDOMAIN=agrozanjir.test\n")
        command = ["docker", "compose", "--project-name", "agrozanjir-validation", "--env-file", str(compose_env),
                   "-f", str(REPO / "compose.yaml")]
        subprocess.run(command + ["config", "--quiet"], check=True)
        for app in ("backend", "frontend"):
            image = root / f"{app}.env"
            image.write_text(app.upper() + "_IMAGE=" + ref(app) + "\n")
            subprocess.run(command + ["--env-file", str(image), "-f", str(REPO / "apps" / app / "compose.yaml"),
                                      "config", "--quiet"], check=True)
    print("Bootstrap base and both app Compose models are valid.")


if __name__ == "__main__":
    main()
