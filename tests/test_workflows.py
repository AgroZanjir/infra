"""Require validation to gate every workflow that can mutate the server."""
from pathlib import Path
import re
import unittest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def jobs(source):
    body = source.split("\njobs:\n", 1)[1]
    return dict(re.findall(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)",
                           body, re.MULTILINE | re.DOTALL))


class ValidationGateTests(unittest.TestCase):
    def test_all_server_jobs_require_validation_in_the_same_run(self):
        for filename, server_job in (("deploy-backend.yml", "deploy"),
                                     ("deploy-frontend.yml", "deploy"),
                                     ("deploy-infra.yml", "deploy"),
                                     ("server-bootstrap.yml", "bootstrap")):
            with self.subTest(workflow=filename):
                workflow_jobs = jobs((WORKFLOWS / filename).read_text())
                self.assertEqual(set(workflow_jobs), {"validate", server_job})
                validation = workflow_jobs["validate"]
                self.assertIn("uses: ./.github/workflows/validate.yml", validation)
                self.assertNotIn("secrets:", validation)
                mutation = workflow_jobs[server_job]
                self.assertRegex(mutation, r"(?m)^    needs: validate$")
                condition = re.search(r"(?m)^    if: (.+)$", mutation).group(1)
                self.assertEqual(condition, "github.ref == 'refs/heads/main'")
                self.assertNotIn("continue-on-error:", mutation)

    def test_validation_is_reusable_and_standalone_only_for_pull_requests(self):
        source = (WORKFLOWS / "validate.yml").read_text()
        events = source.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(re.findall(r"(?m)^  ([\w_]+):", events),
                         ["pull_request", "workflow_call"])
        validation = jobs(source)["validate"]
        for check in ("python3 -m unittest discover -s tests -v", "bash -n",
                      "shellcheck", "python3 tests/validate_compose.py"):
            self.assertIn(check, validation)
        self.assertNotIn("continue-on-error:", validation)


if __name__ == "__main__":
    unittest.main()
