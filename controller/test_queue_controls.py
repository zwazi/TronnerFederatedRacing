import collections
import unittest
from pathlib import Path

from TronnerRacing import (
    MapEntry,
    MapRepository,
    Player,
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

        await controller._command_queue(player, "Sprint 1")
        await controller._command_queue(player, "Sprint 2")
        self.assertEqual(list(controller.queue), [first.key, second.key])

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

    async def test_remove_deletes_only_the_first_repeated_queue_entry(self):
        controller, first, _ = queue_controller()
        player = Player("racer", "Racer")
        controller.queue.extend((first.key, first.key))

        await controller._command_queue(player, "remove Sprint 1")

        self.assertEqual(list(controller.queue), [first.key])


if __name__ == "__main__":
    unittest.main()
