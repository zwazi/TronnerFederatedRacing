import unittest
from pathlib import Path
from unittest import mock

from TronnerRacing import (
    MapEntry,
    Player,
    TronnerRacing,
    final_countdown_seconds,
    map_open_play_seconds,
    map_play_seconds,
    plain_console_text,
)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class Store:
    @staticmethod
    def records(_map_key):
        return []

    @staticmethod
    def rating_average(_map_key):
        return 4.25


class MapTimeDisplayTests(unittest.IsolatedAsyncioTestCase):
    def test_fast_maps_never_get_less_than_two_minutes(self):
        records = [type("Record", (), {"best_seconds": 10.0})()]

        self.assertEqual(map_play_seconds(records), 120.0)
        self.assertEqual(map_open_play_seconds(records), 120.0)
        # A contradictory maximum cannot silently bypass the server floor.
        self.assertEqual(map_play_seconds(records, maximum_seconds=60), 120.0)

    def test_the_configured_floor_can_be_raised_but_not_lowered(self):
        records = [type("Record", (), {"best_seconds": 10.0})()]
        controller = object.__new__(TronnerRacing)
        controller.current = object()
        controller.store = type(
            "RecordStore",
            (),
            {"records": staticmethod(lambda _key: records)},
        )()
        controller.config = {
            "map_duration_seconds": 300,
            "minimum_map_duration_seconds": 30,
        }

        with mock.patch("TronnerRacing.map_records_key", return_value="map"):
            self.assertEqual(controller._map_play_seconds(), 120.0)
            controller.config["minimum_map_duration_seconds"] = 180
            self.assertEqual(controller._map_open_play_seconds(), 180.0)

    def test_oddio_gets_normal_play_before_its_separate_last_run(self):
        records = [type("Record", (), {"best_seconds": 218.605})()]

        self.assertEqual(map_play_seconds(records), 300.0)
        self.assertEqual(map_open_play_seconds(records), 300.0)
        self.assertGreater(final_countdown_seconds(records), 300.0)

    async def test_map_time_is_below_the_complete_records_table(self):
        controller = object.__new__(TronnerRacing)
        controller.config = {
            "round_display_delay_seconds": 0,
            "map_duration_seconds": 300,
        }
        controller.round_active = True
        controller.current = MapEntry(
            "test-map",
            "Test Map",
            "Test Author",
            "1",
            "test",
            "test-map.aamap.xml",
            Path("test-map.aamap.xml"),
            (),
            12,
        )
        racer = Player("racer", "Racer")
        controller.players = {racer.log_name: racer}
        controller.store = Store()
        controller.sink = Sink()

        await controller._delayed_round_display()

        self.assertTrue(
            any(
                command.startswith("PLAYER_MESSAGE racer ")
                for command in controller.sink.commands
            )
        )
        final_display = plain_console_text(controller.sink.commands[-1])
        self.assertIn("Map time: 5 minutes", final_display)
        self.assertIn(
            "Axes: 12",
            final_display,
        )
        self.assertIn(
            "Rating: 4.25/5",
            final_display,
        )

        controller.result_message_preferences = {racer.identity_key: False}
        controller.sink.commands.clear()
        await controller._delayed_round_display()
        self.assertEqual(controller.sink.commands, [])


if __name__ == "__main__":
    unittest.main()
