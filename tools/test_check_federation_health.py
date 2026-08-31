import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from check_federation_health import catalog_summary, latest_current_map


class FederationHealthToolTests(unittest.TestCase):
    def test_latest_map_reads_only_bounded_tail_of_large_log(self):
        with tempfile.TemporaryDirectory() as directory:
            ladderlog = Path(directory) / "ladderlog.txt"
            with ladderlog.open("wb") as output:
                output.seek(8 * 1024 * 1024)
                output.write(
                    b"\nCURRENT_MAP 0 1 Tester/maps/Race-v1.aamap.xml\n"
                )
            self.assertEqual(
                latest_current_map(ladderlog),
                "Tester/maps/Race-v1.aamap.xml",
            )

    def test_catalog_summary_hashes_only_effective_maps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog" / "current"
            public = root / "public"
            catalog.mkdir(parents=True)
            public.mkdir()
            first = b"first map"
            second = b"second map"
            first_key = "Tester/maps/First-v1.aamap.xml"
            second_key = "Tester/maps/Second-v1.aamap.xml"
            (public / first_key).parent.mkdir(parents=True)
            (public / first_key).write_bytes(first)
            (catalog / ".catalog.json").write_text(
                json.dumps(
                    {
                        "generation": "generation-a",
                        "catalogVersion": 7,
                        "maps": [
                            {
                                "status": "active",
                                "resourcePath": first_key,
                                "sha256": hashlib.sha256(first).hexdigest(),
                            },
                            {
                                "status": "active",
                                "resourcePath": second_key,
                                "sha256": hashlib.sha256(second).hexdigest(),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            database = root / "state.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?)",
                ("excluded_map_keys", json.dumps([second_key])),
            )
            connection.commit()
            connection.close()

            summary, failures = catalog_summary(
                {
                    "repository_source": "firebase",
                    "firebase_catalog_dir": str(root / "catalog"),
                    "database": str(database),
                    "public_dir": str(public),
                }
            )

            self.assertEqual(failures, [])
            self.assertEqual(summary["catalog_active_maps"], 2)
            self.assertEqual(summary["catalog_exclusions"], 1)
            self.assertEqual(summary["catalog_effective_maps"], 1)
            self.assertEqual(summary["catalog_public_missing"], 0)


if __name__ == "__main__":
    unittest.main()
