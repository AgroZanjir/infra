"""Shared PostgreSQL backup commands stay scoped to the application database."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import database_dump
from render_env import render
from test_safety import ENV


class DatabaseCommandTests(unittest.TestCase):
    def test_dump_and_restore_select_application_database_and_restore_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with contextlib.redirect_stdout(io.StringIO()):
                render(root, ENV | {"DB_NAME": "custom_db", "DB_USER": "custom_user",
                                    "DB_CONTAINER": "shared-postgres"})
            config = root / "database.json"
            for restore in (False, True):
                with self.subTest(restore=restore), patch.object(sys, "argv", [
                    "database_dump.py", str(config), *(["--restore"] if restore else [])
                ]), patch.object(database_dump.subprocess, "run") as run:
                    database_dump.main()
                    args = run.call_args.args[0]
                    self.assertEqual(args[:6], ["docker", "exec", "-i", "--user", "postgres", "shared-postgres"])
                    self.assertIn("--dbname=custom_db", args)
                    self.assertEqual(args[6], "pg_restore" if restore else "pg_dump")
                    if restore:
                        self.assertIn("--role=custom_user", args)
                        self.assertIn("--exit-on-error", args)
                    self.assertTrue(run.call_args.kwargs["check"])
