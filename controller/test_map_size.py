import asyncio
import collections
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

from TronnerRacing import Player, TronnerRacing


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class Store:
    def __init__(self):
        self.values = {}
        self.reset_keys = []
        self.reset_results = {}

    def set_json(self, key, value):
        self.values[key] = value

    def reset_map(self, key):
        self.reset_keys.append(key)
        return self.reset_results.get(key, (0, 0))


class Repository:
    def __init__(self, revision, *extra_entries):
        self.firebase = object()
        self.catalog = {
            entry.key: entry for entry in (revision, *extra_entries)
        }
        self.revision = revision
        self.requested_factor = None
        self.cached = []

    def create_size_revision(self, _entry, size_factor):
        self.requested_factor = size_factor
        return self.revision

    def cache_for_server(self, entry):
        self.cached.append(entry.key)

    @staticmethod
    def display_name(entry):
        return entry.name


def map_entry(key, version, records_key=None):
    return SimpleNamespace(
        key=key,
        name="Race",
        author="Tester",
        version=version,
        records_key=records_key or key,
    )


class MapSizeCommandTests(unittest.IsolatedAsyncioTestCase):
    def controller(self):
        old = map_entry("Tester/maps/Race-v1.aamap.xml", "v1")
        revision = map_entry("Tester/maps/Race-v2.aamap.xml", "v2")
        other = map_entry("Tester/maps/Other-v1.aamap.xml", "v1")
        repository = Repository(revision, other)
        controller = TronnerRacing.__new__(TronnerRacing)
        controller.config = {
            "size_admin_access_level": 1,
            "default_size_factor": 0,
            "final_countdown_grief_detection_enabled": True,
        }
        controller.current = old
        controller.current_size_factor = 2.0
        controller.repository = repository
        controller.map_lock = asyncio.Lock()
        controller.rotation = collections.deque([revision.key, other.key])
        controller.queue = collections.deque([old.key, revision.key, other.key])
        controller.cycle_played = set()
        controller.pending_size_change = {}
        controller.store = Store()
        controller.sink = Sink()
        controller.transitioning = False
        controller.final_countdown_active = False
        controller.final_countdown_end_epoch = None
        controller.final_countdown_map_key = None
        controller.final_countdown_announcement = None
        controller._round_is_active = Mock(return_value=True)
        controller._reconcile_rotation = Mock()
        controller.private = AsyncMock()
        controller.broadcast = AsyncMock()
        return controller, old, revision, other

    async def test_size_schedules_revision_and_arms_final_countdown(self):
        controller, old, revision, other = self.controller()

        await controller._command_size(Player("owner", "Owner"), 0, "+1")

        self.assertEqual(controller.repository.requested_factor, 3.0)
        self.assertEqual(controller.repository.cached, [old.key, revision.key])
        self.assertIs(controller.current, old)
        self.assertEqual(controller.current_size_factor, 2.0)
        self.assertEqual(controller.sink.commands, [])
        self.assertNotIn("KILL_ALL", controller.sink.commands)
        self.assertTrue(controller.final_countdown_active)
        self.assertIsNone(controller.final_countdown_end_epoch)
        self.assertEqual(controller.final_countdown_map_key, old.key)
        self.assertEqual(
            list(controller.queue),
            [revision.key, other.key],
        )
        self.assertEqual(controller.store.reset_keys, [])
        self.assertEqual(
            controller.pending_size_change,
            {
                "source_map_key": old.key,
                "target_map_key": revision.key,
                "source_records_key": old.key,
                "target_records_key": revision.key,
            },
        )
        self.assertEqual(
            controller.store.values["pending_size_change"],
            controller.pending_size_change,
        )
        controller.broadcast.assert_awaited_once()
        self.assertIn(
            "will load after the final countdown",
            controller.broadcast.await_args.args[0],
        )

    async def test_size_is_rejected_during_an_existing_countdown(self):
        controller, old, _revision, _other = self.controller()
        controller.final_countdown_active = True

        await controller._command_size(Player("owner", "Owner"), 0, "+1")

        self.assertIsNone(controller.repository.requested_factor)
        self.assertIs(controller.current, old)
        controller.private.assert_awaited_once_with(
            ANY,
            "The end-of-map timer is already active.",
        )


class PendingSizeTransitionTests(unittest.TestCase):
    def test_pending_revision_is_protected_as_the_next_map(self):
        old = map_entry("Tester/maps/Race-v1.aamap.xml", "v1")
        revision = map_entry("Tester/maps/Race-v2.aamap.xml", "v2")
        other = map_entry("Tester/maps/Other-v1.aamap.xml", "v1")
        controller = TronnerRacing.__new__(TronnerRacing)
        controller.current = old
        controller.repository = Repository(revision, other)
        controller.queue = collections.deque([other.key])
        controller.rotation = collections.deque([other.key, revision.key])
        controller.cycle_played = set()
        controller.store = Store()
        controller.pending_size_change = {
            "target_map_key": revision.key,
        }

        selected = controller._take_next()

        self.assertIs(selected, revision)
        self.assertEqual(list(controller.queue), [other.key])
        self.assertEqual(list(controller.rotation), [other.key])
        self.assertIn(revision.key, controller.cycle_played)

    def test_records_reset_only_when_pending_revision_activates(self):
        revision = map_entry("Tester/maps/Race-v2.aamap.xml", "v2", "new-records")
        controller = TronnerRacing.__new__(TronnerRacing)
        controller.store = Store()
        controller.store.reset_results = {
            "old-records": (2, 5),
            "new-records": (1, 3),
        }
        controller.pending_size_change = {
            "target_map_key": revision.key,
            "source_records_key": "old-records",
            "target_records_key": "new-records",
        }

        result = controller._consume_pending_size_change(revision)

        self.assertEqual(result, (3, 8))
        self.assertEqual(
            controller.store.reset_keys,
            ["old-records", "new-records"],
        )
        self.assertEqual(controller.pending_size_change, {})
        self.assertEqual(controller.store.values["pending_size_change"], {})


if __name__ == "__main__":
    unittest.main()
