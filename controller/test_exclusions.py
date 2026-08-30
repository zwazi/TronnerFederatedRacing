import asyncio
import collections
import unittest
from pathlib import Path

from TronnerRacing import (
    MapEntry,
    Player,
    TronnerRacing,
    build_compact_columns,
    plain_console_text,
)


def entry(key: str, name: str, author: str, version: str) -> MapEntry:
    return MapEntry(key, name, author, version, "maps", key, Path(key), ())


class Store:
    def __init__(self):
        self.values = {}

    def set_json(self, key, value):
        self.values[key] = value


class Sink:
    def __init__(self):
        self.commands = []
        self.controller = None
        self.active_key = None

    async def send(self, *commands):
        self.commands.extend(commands)
        if "GET_CURRENT_MAP" in commands and self.controller and self.active_key:
            self.controller.transition_observed_key = self.active_key


class Repository:
    def __init__(self, entries):
        self.all_entries = {item.key: item for item in entries}
        self.catalog = dict(self.all_entries)
        self.excluded_keys = set()
        self.source_to_key = {item.source_path: item.key for item in entries}
        self.scan_count = 0

    def display_name(self, item):
        return item.name

    def scan(self):
        self.scan_count += 1
        self.catalog = {
            key: item
            for key, item in self.all_entries.items()
            if key not in self.excluded_keys
        }


class ExclusionTests(unittest.IsolatedAsyncioTestCase):
    def test_exclusion_columns_are_compact_and_left_aligned(self):
        items = ("A", "BBBB", "CC", "DDDD", "EE", "F", "GGGGG", "H", "II")

        lines = build_compact_columns(items)

        self.assertEqual(
            lines,
            [
                "A     DDDD  F      H",
                "BBBB  EE    GGGGG  II",
                "CC",
            ],
        )
        self.assertEqual(lines[0].index("DDDD"), lines[1].index("EE"))
        self.assertEqual(lines[0].index("F"), lines[1].index("GGGGG"))
        self.assertEqual(lines[0].index("H"), lines[1].index("II"))

    async def test_failed_map_is_announced_excluded_and_advanced(self):
        bad = entry("smart/maps/sample-v1.aamap.xml", "sample", "smart", "v1")
        good = entry("Tronner/maps/Good-v1.aamap.xml", "Good", "Tronner", "v1")
        controller = object.__new__(TronnerRacing)
        controller.config = {
            "map_transition_timeout_seconds": 0.05,
            "map_transition_probe_seconds": 0.05,
            "map_transition_failure_confirmations": 2,
        }
        controller.repository = Repository((bad, good))
        controller.store = Store()
        controller.excluded_map_keys = set()
        controller.rotation = collections.deque((good.key,))
        controller.queue = collections.deque()
        controller.cycle_played = {bad.key}
        controller.sink = Sink()
        controller.sink.controller = controller
        controller.sink.active_key = good.key
        controller.transitioning = False
        controller.transition_target_key = None
        controller.transition_map_confirmed = False
        controller.transition_observed_key = None
        controller._transition_watchdog_task = None

        messages = []
        advances = []

        async def broadcast(message):
            messages.append(message)

        async def activate(reason):
            advances.append(reason)

        controller.broadcast = broadcast
        controller.activate_next_map = activate

        controller._begin_map_transition(bad.key)
        watchdog = controller._transition_watchdog_task
        await asyncio.wait_for(watchdog, timeout=1)

        self.assertIn(bad.key, controller.excluded_map_keys)
        self.assertNotIn(bad.key, controller.repository.catalog)
        self.assertEqual(
            controller.store.values["excluded_map_keys"],
            [bad.key],
        )
        self.assertFalse(controller.transitioning)
        self.assertEqual(advances, ["previous map failed to load"])
        self.assertIn("Failed to load sample by smart", messages[0])

    async def test_list_and_remove_exclusion_support_numbered_duplicates(self):
        first = entry("Alpha/maps/Sprint-v1.aamap.xml", "Sprint", "Alpha", "v1")
        second = entry("Zulu/maps/Sprint-v2.aamap.xml", "Sprint", "Zulu", "v2")
        controller = object.__new__(TronnerRacing)
        controller.config = {"map_admin_access_level": 1}
        controller.repository = Repository((first, second))
        controller.excluded_map_keys = {first.key, second.key}
        controller.repository.excluded_keys = controller.excluded_map_keys
        controller.repository.scan()
        controller.store = Store()
        controller.rotation = collections.deque()
        controller.queue = collections.deque()
        controller.cycle_played = set()
        controller.map_lock = asyncio.Lock()
        controller.sink = Sink()
        player = Player("admin", "Admin")

        await controller._command_exclusion_list(player)
        output = "\n".join(
            plain_console_text(command) for command in controller.sink.commands
        )
        self.assertIn("Excluded maps (2)", output)
        self.assertIn("Sprint 1 by Alpha [v1]", output)
        self.assertIn("Sprint 2 by Zulu [v2]", output)

        await controller._command_remove_exclusion(player, 1, "Sprint 1")

        self.assertNotIn(first.key, controller.excluded_map_keys)
        self.assertIn(second.key, controller.excluded_map_keys)
        self.assertIn(first.key, controller.repository.catalog)
        self.assertIn(first.key, controller.rotation)
        self.assertEqual(
            controller.store.values["excluded_map_keys"],
            [second.key],
        )


if __name__ == "__main__":
    unittest.main()
