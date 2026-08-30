import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from TronnerRacing import (
    MapRepository,
    build_leaderboard_table,
    format_finish_message,
    plain_console_text,
    race_time_decimals,
)


class MapPrecisionTests(unittest.TestCase):
    def test_map_setting_is_loaded_into_metadata(self):
        xml = b'''<Resource type="aamap" name="Precision" version="v1" author="Tester" category="maps">
<Map version="0.2.8"><Settings><Setting name="RACE_TIME_DECIMALS" value="8"/></Settings>
<World><Field><Spawn x="0" y="0" xdir="1" ydir="0"/></Field></World></Map></Resource>'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "repository"
            checkout.mkdir()
            (checkout / "precision.aamap.xml").write_bytes(xml)
            repository = MapRepository(
                {
                    "repository_git_url": "unused",
                    "repository_checkout": str(checkout),
                    "public_dir": str(root / "public"),
                    "resource_cache_dir": str(root / "cache"),
                    "map_override_dir": str(root / "overrides"),
                    "map_revision_dir": str(root / "revisions"),
                    "dtd_source_dir": str(root / "dtd"),
                }
            )
            repository.scan()
            entry = next(iter(repository.catalog.values()))
            self.assertEqual(entry.time_decimals, 8)

    def test_map_metadata_selects_additional_decimals(self):
        high_precision = SimpleNamespace(time_decimals=8)
        other = SimpleNamespace(key="author/maps/Other-v1.aamap.xml")

        self.assertEqual(race_time_decimals(high_precision), 8)
        self.assertEqual(race_time_decimals(other), 3)

    def test_finish_message_uses_map_precision(self):
        message = format_finish_message(
            "Racer",
            5.3041172,
            1,
            5.3036861,
            1,
            5.3036861,
            1,
            1,
            1,
            time_decimals=8,
        )

        visible = plain_console_text(message)
        self.assertIn("Finish: 5.30411720", visible)
        self.assertIn("Best: 5.30368610", visible)
        self.assertIn("Split: +0.00043110", visible)

    def test_leaderboard_uses_map_precision(self):
        record = SimpleNamespace(
            identity_key="racer",
            username="Racer",
            best_seconds=5.3036861,
            best_turns=1,
        )

        lines, _ = build_leaderboard_table(
            "Precision Test",
            "Player",
            [record],
            time_decimals=8,
        )

        self.assertTrue(
            any("5.30368610" in plain_console_text(line) for line in lines)
        )


if __name__ == "__main__":
    unittest.main()
