import unittest
from pathlib import Path

from TronnerRacing import (
    MapEntry,
    Player,
    Record,
    TronnerRacing,
    plain_console_text,
)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class Store:
    def __init__(self):
        self.items = [
            Record(
                f"guest:racer{rank}",
                f"Racer {rank}",
                float(rank),
                False,
                rank * 10,
            )
            for rank in range(1, 13)
        ]

    def records(self, _map_key):
        return self.items

    @staticmethod
    def rating_average(_map_key):
        return 4.5


class LeaderboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_top_ten_are_sent_only_to_requester(self):
        controller = object.__new__(TronnerRacing)
        controller.current = MapEntry(
            "test-map",
            "Test Map",
            "Test Author",
            "1",
            "test",
            "test-map.aamap.xml",
            Path("test-map.aamap.xml"),
            (),
            8,
        )
        controller.store = Store()
        controller.sink = Sink()
        requester = Player("requester", "Requester")

        await controller._command_leaderboard(requester)

        self.assertTrue(controller.sink.commands)
        self.assertTrue(
            all(
                command.startswith("PLAYER_MESSAGE requester ")
                for command in controller.sink.commands
            )
        )
        output = "\n".join(controller.sink.commands)
        self.assertIn("Racer 1 ", output)
        self.assertIn("Racer 10", output)
        self.assertNotIn("Racer 11", output)
        self.assertNotIn("Racer 12", output)
        self.assertNotIn("CONSOLE_MESSAGE", output)
        plain_output = plain_console_text(output)
        self.assertIn("Axes: 8", plain_output)
        self.assertIn("Rating: 4.50/5", plain_output)


if __name__ == "__main__":
    unittest.main()
