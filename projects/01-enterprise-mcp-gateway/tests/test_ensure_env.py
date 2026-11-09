"""`make up` creates/backfills .env via scripts/ensure-env.sh; its self-test runs on temp files only."""
import pathlib
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[3]


class EnsureEnvTests(unittest.TestCase):
    def test_selftest(self):
        proc = subprocess.run(["sh", "projects/01-enterprise-mcp-gateway/tests/ensure_env_selftest.sh"],
                              cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("self-test: OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
