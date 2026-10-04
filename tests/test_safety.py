"""Production configuration and real deployment-script failure paths, with fake Docker."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
from render_env import render
from server_state import image, reference

ENV = {"DOMAIN": "agrozanjir.test", "DJANGO_SECRET_KEY": "test-only-" + "abcdef123456" * 5,
       "POSTGRES_ADMIN_PASSWORD": "admin-test-only-1234567890",
       "POSTGRES_PASSWORD": "app-test-only-$#@:'1234567890",
       "REDIS_PASSWORD": "redis-test-only-$#@:'1234567890"}


def ref(app="backend", letter="a", run=123):
    return f"ghcr.io/agrozanjir/{app}:{app}-{letter * 40}-{run}-1@sha256:{letter * 64}"


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_or_partial_backup_does_not_block_bootstrap_or_deploy(self):
        for bootstrap in (True, False):
            with contextlib.redirect_stdout(io.StringIO()):
                render(self.directory, ENV | {"RESTIC_PASSWORD": "partial"}, bootstrap=bootstrap)
            self.assertFalse((self.directory / "backup.json").exists())
            self.assertEqual((self.directory / "backup-enabled").read_text(), "false\n")

    def test_bootstrap_needs_no_application_secrets(self):
        render(self.directory, {}, bootstrap=True)
        self.assertFalse((self.directory / "backend.env").exists())

    def test_secrets_preserved_as_raw_values_and_urls_encoded(self):
        render(self.directory, ENV)
        settings = dict(line.split("=", 1) for line in (self.directory / "backend.env").read_text().splitlines())
        self.assertEqual(settings["DJANGO_SECRET_KEY"], ENV["DJANGO_SECRET_KEY"])
        self.assertIn("%24%23%40%3A%27", settings["DATABASE_URL"])
        self.assertEqual(settings["CORS_ALLOWED_ORIGINS"], "")
        self.assertEqual((self.directory / "postgres_app_password").read_text(), ENV["POSTGRES_PASSWORD"])

    def test_newlines_missing_secrets_and_invalid_domains_fail(self):
        for env in (ENV | {"DJANGO_SECRET_KEY": "injected\nDEBUG=True"},
                    ENV | {"POSTGRES_PASSWORD": ""}, ENV | {"DOMAIN": "bad;hostname"}):
            with self.assertRaises(ValueError):
                render(self.directory, env)

    def test_complete_backup_is_private_and_allowlisted(self):
        backup = {"RESTIC_REPOSITORY": "s3:https://s3.example.test/bucket/prefix", "RESTIC_PASSWORD": "test-only",
                  "AWS_ACCESS_KEY_ID": "test-only", "AWS_SECRET_ACCESS_KEY": "test-only"}
        render(self.directory, ENV | backup | {"UNRELATED_SECRET": "must-not-appear"})
        stored = json.loads((self.directory / "backup.json").read_text())
        self.assertEqual(set(stored), set(backup) | {"AWS_DEFAULT_REGION"})

    def test_image_parser_rejects_mutable_or_wrong_application(self):
        path = self.directory / "image.env"
        path.write_text("BACKEND_IMAGE=" + ref() + "\n")
        self.assertEqual(image("backend", path), ref())
        for invalid in (ref().split("@")[0], ref("frontend"), ref().replace("-123-1", "-0-1")):
            with self.assertRaises(ValueError):
                reference("backend", invalid)
        path.write_text("BACKEND_IMAGE=" + ref() + "\nOTHER=oops\n")
        with self.assertRaises(ValueError):
            image("backend", path)


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Linux deployment script test")
class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root, self.stage, self.bin = (base / name for name in ("root", "stage", "bin"))
        self.bin.mkdir()
        shutil.copytree(REPO, self.stage, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        self.root.mkdir()
        shutil.copytree(self.stage / "scripts", self.root / "scripts")
        (self.root / "state").mkdir()
        (self.root / "runtime").mkdir()
        for filename in ("compose.yaml", "Caddyfile"):
            shutil.copyfile(self.stage / filename, self.root / filename)
        for app in ("backend", "frontend"):
            (self.root / "apps" / app).mkdir(parents=True)
            shutil.copyfile(self.stage / "apps" / app / "compose.yaml", self.root / "apps" / app / "compose.yaml")
        render(self.stage / "runtime", ENV)
        for script in (self.root / "scripts").glob("*.sh"):
            script.chmod(0o750)
        self.log = base / "docker.log"
        docker = self.bin / "docker"
        docker.write_text('#!/bin/bash\nset -eu\nprintf "%s\\n" "$*" >>"$MOCK_LOG"\n'
                          'if [[ -n "${MOCK_FAIL_MARKER:-}" && "$*" == *"up -d --no-deps"* && ! -f "$MOCK_FAIL_MARKER" ]]; then\n'
                          ' touch "$MOCK_FAIL_MARKER"; exit 42\nfi\n')
        docker.chmod(0o755)
        curl = self.bin / "curl"
        curl.write_text("#!/bin/bash\nexit 0\n")
        curl.chmod(0o755)
        self.env = os.environ | {"DEPLOY_ROOT": str(self.root), "PATH": str(self.bin) + ":" + os.environ["PATH"],
                                 "MOCK_LOG": str(self.log)}

    def tearDown(self):
        self.temp.cleanup()

    def deploy(self, app="backend", candidate=None, fail=False):
        candidate = candidate or ref(app)
        (self.stage / "apps" / app / "image.env").write_text(app.upper() + "_IMAGE=" + candidate + "\n")
        env = self.env | ({"MOCK_FAIL_MARKER": str(self.root / "failed-once")} if fail else {})
        return subprocess.run(["bash", str(self.stage / "scripts/deploy.sh"), app, str(self.stage), "c" * 40, "456", "1"],
                              env=env, capture_output=True, text=True, timeout=30)

    def state(self, app="backend"):
        return json.loads((self.root / "state" / f"{app}.json").read_text())

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_first_success_second_success_and_reapply_keep_retained_pair(self):
        self.assert_ok(self.deploy())
        self.assertIsNone(self.state()["previous"])
        newer = ref(letter="b", run=124)
        self.assert_ok(self.deploy(candidate=newer))
        self.assertEqual((self.state()["current"], self.state()["previous"]), (newer, ref()))
        self.assert_ok(self.deploy(candidate=newer))
        self.assertEqual(self.state()["previous"], ref())

    def test_failed_update_restores_image_without_publishing_success(self):
        self.assert_ok(self.deploy())
        original = self.state()
        result = self.deploy(candidate=ref(letter="b", run=124), fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state(), original)
        self.assertIn(ref(), (self.root / "apps/backend/image.env").read_text())
        self.assertIn("Previous application image restored", result.stderr)

    def test_failed_first_release_stops_app_without_receipt(self):
        result = self.deploy(fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "state/backend.json").exists())
        self.assertIn("stop backend", self.log.read_text())

    def test_cloudflare_challenge_reports_safe_headers_and_does_not_publish_success(self):
        curl = self.bin / "curl"
        curl.write_text('#!/bin/bash\nwhile [[ $# -gt 0 ]]; do\n'
                        ' if [[ "$1" == --dump-header ]]; then\n'
                        '  printf "HTTP/2 403\\nserver: cloudflare\\ncf-ray: test-ray\\ncf-mitigated: challenge\\nSet-Cookie: private-cookie\\n" >"$2"\n'
                        '  shift\n fi\n shift\ndone\nexit 22\n')
        for app in ("backend", "frontend"):
            with self.subTest(app=app):
                result = self.deploy(app)
                self.assertEqual(result.returncode, 22)
                self.assertIn("cf-ray: test-ray", result.stderr)
                self.assertIn("Cloudflare challenged the public probe", result.stderr)
                self.assertNotIn("private-cookie", result.stderr)
                self.assertFalse((self.root / "state" / f"{app}.json").exists())
                self.assertIn(f"stop {app}", self.log.read_text())

    def test_frontend_cannot_overwrite_shared_config_or_backend_state(self):
        self.assert_ok(self.deploy())
        old = self.state()
        (self.stage / "Caddyfile").write_text("stale shared configuration")
        self.assert_ok(self.deploy("frontend"))
        self.assertEqual(self.state(), old)
        self.assertNotIn("stale", (self.root / "Caddyfile").read_text())
        self.assertEqual(self.state("frontend")["current"], ref("frontend"))

    def test_configured_backup_failure_blocks_replacement(self):
        self.assert_ok(self.deploy())
        original = self.state()
        (self.stage / "runtime/backup.json").write_text(json.dumps({"RESTIC_REPOSITORY": "s3:https://s3.test/bucket",
            "RESTIC_PASSWORD": "test", "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}))
        restic = self.bin / "restic"
        restic.write_text("#!/bin/bash\nexit 41\n")
        restic.chmod(0o755)
        # A real pg_dump emits bytes; this mock must reach the configured restic failure.
        docker = self.bin / "docker"
        docker.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >>"$MOCK_LOG"\n[[ "$*" != *pg_dump* ]] || echo dump\nexit 0\n')
        self.log.write_text("")
        result = self.deploy(candidate=ref(letter="b", run=124))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state(), original)
        self.assertNotIn("pull backend", self.log.read_text())

    def test_failed_shared_update_restores_previous_config_and_scripts(self):
        original = (self.root / "Caddyfile").read_text()
        (self.stage / "Caddyfile").write_text("candidate shared configuration")
        docker = self.bin / "docker"
        marker = self.root / "shared-failed-once"
        docker.write_text('#!/bin/bash\nset -eu\nprintf "%s\\n" "$*" >>"$MOCK_LOG"\n'
            'if [[ "$*" == *"up -d --wait"* && ! -f "$MOCK_FAIL_MARKER" ]]; then\n'
            ' touch "$MOCK_FAIL_MARKER"; exit 42\nfi\n')
        result = subprocess.run(["bash", str(self.stage / "scripts/deploy.sh"), "infra", str(self.stage), "c" * 40, "456", "1"],
            env=self.env | {"MOCK_FAIL_MARKER": str(marker)}, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "Caddyfile").read_text(), original)
        self.assertIn("restoring previous configuration", result.stderr)


if __name__ == "__main__":
    unittest.main()
