import collections
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from TronnerRacing import (
    MapEntry,
    MapRepository,
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


class Store:
    def __init__(self):
        self.values = {}

    def set_json(self, key, value):
        self.values[key] = value

    def map_ranks_for_player(self, map_keys, identity_key):
        requested = set(map_keys)
        return {
            key: rank
            for key, rank in self.values.get("ranks", {}).items()
            if key in requested
        }


def map_entry(key, author, version):
    return MapEntry(
        key,
        "Sprint",
        author,
        version,
        "race",
        key,
        Path(key),
        (),
    )


def queue_controller():
    first = map_entry("Alpha/race/Sprint-1.aamap.xml", "Alpha", "1")
    second = map_entry("Zulu/race/Sprint-2.aamap.xml", "Zulu", "2")
    repository = object.__new__(MapRepository)
    repository.catalog = {first.key: first, second.key: second}

    controller = object.__new__(TronnerRacing)
    controller.repository = repository
    controller.queue = collections.deque()
    controller.rotation = collections.deque()
    controller.cycle_played = set()
    controller.store = Store()
    controller.sink = Sink()
    controller.current = None
    return controller, first, second


class QueueControlTests(unittest.IsolatedAsyncioTestCase):
    def test_duplicate_names_receive_numbered_selectors(self):
        controller, first, second = queue_controller()

        self.assertEqual(controller.repository.display_name(first), "Sprint 1")
        self.assertEqual(controller.repository.display_name(second), "Sprint 2")
        self.assertEqual(controller.repository.search("Sprint 1"), [first])
        self.assertEqual(controller.repository.search("Sprint 2"), [second])
        self.assertEqual(
            set(controller.repository.search("Sprint")),
            {first, second},
        )

    async def test_queue_remove_and_clear_support_duplicate_names(self):
        controller, first, second = queue_controller()
        player = Player("racer", "Racer")

        await controller._command_queue(player, "add Sprint 1")
        await controller._command_queue(player, "add Sprint 2")
        self.assertEqual(list(controller.queue), [first.key, second.key])
        self.assertEqual(
            controller.queue_attribution[first.key]["queuedBy"], "Racer"
        )

        await controller._command_queue(player, "remove Sprint 1")
        self.assertEqual(list(controller.queue), [second.key])

        await controller._command_queue(player, "clear")
        self.assertEqual(list(controller.queue), [])
        self.assertEqual(controller.store.values["queue"], [])

        output = "\n".join(
            plain_console_text(command) for command in controller.sink.commands
        )
        self.assertIn("queued Sprint 1 by Alpha (position 1)", output)
        self.assertIn("queued Sprint 2 by Zulu (position 2)", output)
        self.assertIn("removed Sprint 1 by Alpha", output)
        self.assertIn("cleared 1 map from the queue", output)

    async def test_public_rotation_preview_marks_manual_and_automatic_maps(self):
        controller, first, second = queue_controller()
        controller.current_size_factor = None
        controller.pending_size_change = {}
        player = Player("racer", "Racer", auth_name="racer@tronner")

        await controller._command_queue(player, "add Sprint 1")
        controller.rotation.append(second.key)

        preview = controller._dashboard_upcoming_rotation()

        self.assertEqual([item["name"] for item in preview], ["Sprint", "Sprint"])
        self.assertEqual([item["author"] for item in preview], ["Alpha", "Zulu"])
        self.assertTrue(preview[0]["queued"])
        self.assertEqual(preview[0]["queuedBy"], "racer@tronner")
        self.assertEqual(preview[0]["queuedVia"], "server")
        self.assertFalse(preview[1]["queued"])

    async def test_remove_deletes_only_the_first_repeated_queue_entry(self):
        controller, first, _ = queue_controller()
        player = Player("racer", "Racer")
        controller.queue.extend((first.key, first.key))

        await controller._command_queue(player, "remove Sprint 1")

        self.assertEqual(list(controller.queue), [first.key])

    async def test_old_add_format_shows_migration_help_without_queueing(self):
        controller, first, _ = queue_controller()
        player = Player("racer", "Racer")

        await controller._command_queue(player, "Sprint 1")

        self.assertEqual(list(controller.queue), [])
        output = "\n".join(
            plain_console_text(command) for command in controller.sink.commands
        )
        self.assertIn("format changed: use /q add [map name]", output)
        self.assertIn("/q lowest", output)

    async def test_lowest_prioritizes_an_unranked_map(self):
        controller, first, second = queue_controller()
        third = MapEntry(
            "Other/race/Long-1.aamap.xml",
            "Long",
            "Other",
            "1",
            "race",
            "Other/race/Long-1.aamap.xml",
            Path("Other/race/Long-1.aamap.xml"),
            (),
        )
        controller.repository.catalog[third.key] = third
        controller.store.values["ranks"] = {
            first.key: 12,
            second.key: 20,
        }
        player = Player("racer", "Racer")

        await controller._command_queue(player, "lowest")

        self.assertEqual(list(controller.queue), [third.key])

    async def test_lowest_randomly_selects_among_equal_worst_ranks(self):
        controller, first, second = queue_controller()
        controller.store.values["ranks"] = {first.key: 9, second.key: 9}
        player = Player("racer", "Racer")

        with mock.patch.object(random, "choice", return_value=second) as choice:
            await controller._command_queue(player, "lowest")

        choice.assert_called_once_with([first, second])
        self.assertEqual(list(controller.queue), [second.key])

    def test_state_store_returns_exact_rank_for_each_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "rank.sqlite3")
            try:
                players = [Player(f"p{index}", f"P{index}") for index in range(3)]
                for seconds, player in zip((10.0, 20.0, 30.0), players):
                    store.add_finish("map-a", player, seconds)
                store.add_finish("map-b", players[1], 5.0)

                self.assertEqual(
                    store.map_ranks_for_player(
                        ["map-a", "map-b", "map-unranked"],
                        players[1].identity_key,
                    ),
                    {"map-a": 2, "map-b": 1},
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
