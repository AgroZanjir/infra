"""Bootstrap transport contract, without SSH access or changes to the host."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class SSHPolicyTests(unittest.TestCase):
    def test_root_key_login_remains_available_and_password_login_is_disabled(self):
        script = (REPO / "scripts/bootstrap.sh").read_text()
        config = re.search(r"<<'CONFIG'\n(.*?)\nCONFIG", script, re.DOTALL).group(1)
        settings = dict(line.split(maxsplit=1) for line in config.splitlines())
        self.assertEqual(settings["PermitRootLogin"], "prohibit-password")
        self.assertEqual(settings["PubkeyAuthentication"], "yes")
        self.assertEqual(settings["PasswordAuthentication"], "no")
        self.assertEqual(settings["KbdInteractiveAuthentication"], "no")


@unittest.skipUnless(shutil.which("bash"), "Media group checks require Bash")
class MediaGroupTests(unittest.TestCase):
    def test_bootstrap_error_reports_location_and_preserves_exit_status(self):
        header = (REPO / "scripts/bootstrap.sh").read_text().split('\nsource ', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "bootstrap-error.sh"
            script.write_text(header + '\n(exit 7)\n')
            result = subprocess.run([shutil.which("bash"), script.as_posix()],
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("bootstrap-error.sh:", result.stderr)
        self.assertIn("(exit 7)", result.stderr)

    def lookup(self, status, entry=""):
        script = (REPO / "scripts/bootstrap.sh").read_text()
        start = script.index("if media_entry=$(getent group 10001); then")
        end = script.index('\ninstall -d', start)
        harness = '''set -Eeuo pipefail
username=deploy
getent() { printf '%s' "$MOCK_ENTRY"; return "$MOCK_STATUS"; }
groupadd() { printf 'groupadd %s\\n' "$*"; }
usermod() { printf 'usermod %s\\n' "$*"; }
'''
        return subprocess.run([shutil.which("bash"), "-c", harness + script[start:end]],
                              env=os.environ | {"MOCK_STATUS": str(status), "MOCK_ENTRY": entry},
                              capture_output=True, text=True, timeout=10)

    def test_missing_group_is_created_under_strict_shell_options(self):
        result = self.lookup(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("groupadd --gid 10001 agrozanjir-data", result.stdout)
        self.assertIn("usermod -aG agrozanjir-data deploy", result.stdout)

    def test_existing_group_is_reused(self):
        result = self.lookup(0, "existing-data:x:10001:\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("groupadd", result.stdout)
        self.assertIn("usermod -aG existing-data deploy", result.stdout)

    def test_lookup_errors_do_not_create_a_group(self):
        result = self.lookup(1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Media group lookup failed", result.stderr)
        self.assertNotIn("groupadd", result.stdout)
        self.assertNotIn("usermod", result.stdout)


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Linux SSH transport test")
class BootstrapTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.bin = base / "bin"
        self.bin.mkdir()
        self.log = base / "ssh.log"
        self.archive = base / "bundle.tar.gz"
        ssh = self.bin / "ssh"
        ssh.write_text('''#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >>"$MOCK_LOG"
command=${!#}
if [[ "$command" = 'sudo -n true' && "${MOCK_SUDO_FAIL:-}" = 1 ]]; then exit 1; fi
if [[ "$command" = "tar -xzf"* ]]; then cat >"$MOCK_ARCHIVE"; fi
if [[ "$command" = "docker info"* && "${MOCK_DOCKER_FAIL:-}" = 1 ]]; then exit 1; fi
''')
        ssh.chmod(0o755)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("VPS_", "RESTIC_", "AWS_", "GHCR_"))}
        self.env.update({"PATH": str(self.bin) + ":" + os.environ["PATH"],
                         "RUNNER_TEMP": str(base), "MOCK_LOG": str(self.log),
                         "MOCK_ARCHIVE": str(self.archive), "VPS_HOST": "vps.example.test",
                         "VPS_USERNAME": "deploy", "VPS_SSH_KEY": "synthetic-private-key",
                         "VPS_SSH_KNOWN_HOSTS": "synthetic-host-key", "VPS_SSH_PORT": "2222",
                         "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"})

    def tearDown(self):
        self.temp.cleanup()

    def bootstrap(self, **extra):
        return subprocess.run(["bash", "scripts/remote.sh", "bootstrap"], cwd=REPO,
                              env=self.env | extra, capture_output=True, text=True, timeout=30)

    def test_same_manual_account_is_used_and_no_login_keys_are_uploaded(self):
        result = self.bootstrap(VPS_BOOTSTRAP_USERNAME="root")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertTrue(calls[0].endswith("deploy@vps.example.test sudo -n true"))
        self.assertTrue(all("deploy@vps.example.test" in call for call in calls))
        self.assertTrue(all("-p 2222" in call for call in calls))
        self.assertTrue(any("sudo -n bash '/tmp/agrozanjir-123-1/scripts/bootstrap.sh' "
                            "'/tmp/agrozanjir-123-1' 'deploy' '2222'" in call for call in calls))
        self.assertTrue(any('docker info --format "{{.ServerVersion}}"' in call for call in calls))
        self.assertTrue(calls[-1].endswith("rm -rf -- '/tmp/agrozanjir-123-1'"))
        with tarfile.open(self.archive) as archive:
            files = archive.getnames()
            self.assertIn("runtime/backup-enabled", files)
            self.assertFalse(any(Path(name).name in {"deploy.pub", "key", "authorized_keys"}
                                 for name in files))
        self.assertFalse(any("finalize-ssh" in call for call in calls))

    def test_missing_sudo_fails_before_upload_or_server_changes(self):
        result = self.bootstrap(MOCK_SUDO_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manually provisioned", result.stderr)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertFalse(self.archive.exists())

    def test_missing_docker_access_fails_and_removes_staging(self):
        result = self.bootstrap(MOCK_DOCKER_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.log.read_text().splitlines()[-1].endswith(
            "rm -rf -- '/tmp/agrozanjir-123-1'"))

    def test_workflow_cannot_connect_as_root(self):
        result = self.bootstrap(VPS_USERNAME="root")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())


@unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0,
                     "Bootstrap preconditions need a disposable root test container")
class BootstrapAccountTests(unittest.TestCase):
    def test_missing_account_is_rejected_before_installing_packages(self):
        result = subprocess.run(["bash", str(REPO / "scripts/bootstrap.sh"), "/tmp/unused",
                                 "agrozanjir-test-missing-user", "22"],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Create the deployment user", result.stderr)


if __name__ == "__main__":
    unittest.main()
