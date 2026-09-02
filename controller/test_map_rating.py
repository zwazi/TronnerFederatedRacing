import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from TronnerRacing import (
    MapEntry,
    Player,
    StateStore,
    TronnerRacing,
    plain_console_text,
)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


def current_map() -> MapEntry:
    return MapEntry(
        "Tester/maps/Race-v1.aamap.xml",
        "Race",
        "Tester",
        "v1",
        "maps",
        "Tester/maps/Race-v1.aamap.xml",
        Path("Race-v1.aamap.xml"),
        (),
        4,
    )


class MapRatingStoreTests(unittest.TestCase):
    def test_set_undo_revoke_and_average_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            player = Player("racer", "Racer")
            store = StateStore(path)

            self.assertIsNone(store.rating_average("map"))
            self.assertEqual(store.set_rating("map", player, 4), (None, True))
            self.assertEqual(store.rating_average("map"), 4.0)
            self.assertEqual(store.rating_summary("map"), (4.0, 1))
            self.assertEqual(store.rating_summaries(), {"map": (4.0, 1)})
            second = Player("second", "Second")
            self.assertEqual(store.set_rating("map", second, 2), (None, True))
            self.assertEqual(store.rating_summaries(), {"map": (3.0, 2)})
            self.assertEqual(store.rating_summary("map"), (3.0, 2))
            self.assertEqual(store.revoke_rating("map", second.identity_key), 2)
            self.assertEqual(store.set_rating("map", player, 5), (4, True))
            self.assertEqual(store.undo_rating("map", player.identity_key), (5, 4))
            self.assertEqual(store.rating_for("map", player.identity_key), 4)
            self.assertIsNone(store.undo_rating("map", player.identity_key))
            store.close()

            store = StateStore(path)
            self.assertEqual(store.rating_for("map", player.identity_key), 4)
            self.assertEqual(store.revoke_rating("map", player.identity_key), 4)
            self.assertIsNone(store.rating_average("map"))
            self.assertIsNone(store.rating_summary("map"))
            self.assertEqual(store.set_rating("map", player, 3), (None, True))
            self.assertEqual(store.undo_rating("map", player.identity_key), (3, None))
            self.assertIsNone(store.rating_for("map", player.identity_key))
            store.close()


class MapRatingCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_announces_and_undo_revoke_apply_to_current_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(TronnerRacing)
            controller.current = current_map()
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.sink = Sink()
            player = Player("racer", "Racer")

            await controller._command_rate(player, "4")
            self.assertEqual(
                plain_console_text(controller.sink.commands[-1]),
                "CONSOLE_MESSAGE Racer rated Race 4/5. "
                "Use /rate to submit your own rating.",
            )
            await controller._command_rate(player, "5")
            await controller._command_rate(player, "undo")
            self.assertEqual(
                controller.store.rating_for(
                    controller.current.rating_key, player.identity_key
                ),
                4,
            )
            await controller._command_rate(player, "revoke")
            self.assertIsNone(
                controller.store.rating_for(
                    controller.current.rating_key, player.identity_key
                )
            )
            controller.store.close()

    async def test_rate_can_target_a_specific_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(TronnerRacing)
            controller.current = current_map()
            target = MapEntry(
                "Builder/maps/Orbit-v2.aamap.xml",
                "Orbit",
                "Builder",
                "v2",
                "maps",
                "Builder/maps/Orbit-v2.aamap.xml",
                Path("Orbit-v2.aamap.xml"),
                (),
                4,
            )
            controller.repository = SimpleNamespace(
                search=lambda query: [target]
                if query.casefold() == "orbit"
                else []
            )
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.sink = Sink()
            player = Player("racer", "Racer")

            await controller._command_rate(player, "Orbit 5")

            self.assertEqual(
                controller.store.rating_for(target.rating_key, player.identity_key),
                5,
            )
            self.assertIsNone(
                controller.store.rating_for(
                    controller.current.rating_key, player.identity_key
                )
            )
            self.assertIn(
                "Racer rated Orbit 5/5",
                plain_console_text(controller.sink.commands[-1]),
            )
            controller.store.close()

    async def test_rate_rejects_invalid_values_and_missing_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(TronnerRacing)
            controller.current = current_map()
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.sink = Sink()
            player = Player("racer", "Racer")

            await controller._command_rate(player, "4.5")
            self.assertIn(
                "Usage: /rate [1-5]",
                plain_console_text(controller.sink.commands[-1]),
            )
            controller.current = None
            await controller._command_rate(player, "5")
            self.assertIn(
                "No current map is available to rate.",
                plain_console_text(controller.sink.commands[-1]),
            )
            controller.store.close()


if __name__ == "__main__":
    unittest.main()
