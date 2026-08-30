import subprocess
import tempfile
import unittest
from pathlib import Path

from check_public_tree import scan


class PublicTreeTests(unittest.TestCase):
    def scan_files(self, files: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet", str(root)],
                check=True,
            )
            for name, contents in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")
            return scan(root)

    def test_inert_examples_are_allowed(self):
        failures = self.scan_files(
            {
                "example.txt": (
                    "10.77.0.1 192.0.2.10 127.0.0.1 "
                    "https://example.firebaseio.com"
                )
            }
        )
        self.assertEqual(failures, [])

    def test_runtime_state_and_credentials_are_rejected(self):
        service_account_marker = '{"type":"service_' + 'account"}'
        failures = self.scan_files(
            {
                "logs/server.txt": "runtime data",
                "operator.key": "not-a-real-key",
                "account.json": service_account_marker,
            }
        )
        self.assertTrue(any("runtime directory" in failure for failure in failures))
        self.assertTrue(any("credential extension" in failure for failure in failures))
        self.assertTrue(any("service-account document" in failure for failure in failures))

    def test_operator_addresses_and_paths_are_rejected(self):
        public_address = ".".join(("8", "8", "8", "8"))
        operator_path = "/" + "home/operator/private/config.json"
        failures = self.scan_files(
            {"configuration.txt": f"{public_address}\n{operator_path}\n"}
        )
        self.assertTrue(any("public IPv4 literal" in failure for failure in failures))
        self.assertTrue(any("operator home path" in failure for failure in failures))

    def test_symbolic_links_are_rejected_without_following_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (root / "outside").symlink_to("/etc/passwd")
            failures = scan(root)
            self.assertEqual(failures, ["symbolic links are not allowed: outside"])


if __name__ == "__main__":
    unittest.main()
