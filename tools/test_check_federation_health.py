import tempfile
import unittest
from pathlib import Path

from check_federation_health import latest_current_map


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


if __name__ == "__main__":
    unittest.main()
