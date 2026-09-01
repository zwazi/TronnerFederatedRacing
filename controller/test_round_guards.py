import asyncio
import collections
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from TronnerRacing import MapEntry, Player, StateStore, TronnerRacing as Controller


class RoundGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_skip_defers_to_warn_first_progress_guard(self):
        class Sink:
            def __init__(self):
                self.commands = []

            async def send(self, *commands):
                self.commands.extend(commands)

        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(Controller)
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.current = SimpleNamespace(key="current")
            controller.round_active = True
            controller.transitioning = False
            controller.final_countdown_active = False
            controller.final_countdown_end_epoch = None
            controller.final_countdown_map_key = None
            controller.final_countdown_announcement = None
            controller.skip_votes = set()
            controller.config = {"final_countdown_idle_seconds": 10}
            controller.sink = Sink()
            player = Player("racer", "Racer", connected=True, active=True)
            controller.players = {"racer": player}

            await controller._command_skip(player)

            self.assertTrue(controller.final_countdown_active)
            self.assertEqual(controller.sink.commands, [])
            controller.store.close()

    async def test_end_defers_to_warn_first_progress_guard(self):
        class Sink:
            def __init__(self):
                self.commands = []

            async def send(self, *commands):
                self.commands.extend(commands)

        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(Controller)
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.current = SimpleNamespace(key="current")
            controller.round_active = True
            controller.transitioning = False
            controller.final_countdown_active = False
            controller.final_countdown_announcement = None
            controller.config = {
                "map_admin_access_level": 1,
                "final_countdown_idle_seconds": 10,
            }
            controller.sink = Sink()
            player = Player("admin", "Admin", connected=True, active=True)

            await controller._command_end(player, access_level=0)

            self.assertEqual(controller.sink.commands, [])
            self.assertIsNotNone(controller.deadline_epoch)
            self.assertEqual(
                controller.store.get_json("deadline_epoch", None),
                controller.deadline_epoch,
            )
            controller.store.close()

    async def test_spectator_waits_for_team_before_first_spawn(self):
        class Sink:
            def __init__(self):
                self.commands = []

            async def send(self, *commands):
                self.commands.extend(commands)

        controller = object.__new__(Controller)
        controller.players = {}
        controller.aliases = {}
        controller.online_snapshot_misses = {}
        controller.finalists = set()
        controller.respawn_tasks = {}
        controller.freeze_tasks = {}
        controller.center_clear_tasks = {}
        controller.round_active = True
        controller.transitioning = False
        controller.final_countdown_active = False
        controller.sink = Sink()
        scheduled = []

        def schedule(player, delay_seconds=None):
            scheduled.append((player, delay_seconds))

        controller._schedule_respawn = schedule

        await controller._handle_player_arrival(
            "spectator 192.0.2.1 Spectator", False
        )
        await asyncio.sleep(0)
        player = controller.players["spectator"]
        self.assertFalse(player.active)
        self.assertFalse(player.forced_racing)
        self.assertEqual(scheduled, [])

        await controller._handle_player_arrival(
            "spectator 192.0.2.1 Spectator", True
        )
        self.assertTrue(player.active)
        self.assertEqual(scheduled, [(player, 0.0)])

    async def test_zero_delay_leaderboard_waits_for_map_confirmation(self):
        class Sink:
            def __init__(self):
                self.commands = []

            async def send(self, *commands):
                self.commands.extend(commands)

        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(Controller)
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.current = MapEntry(
                "next", "Next", "Author", "v1", "maps", "next", Path("next"), ()
            )
            controller.config = {"map_duration_seconds": 300}
            controller.transitioning = True
            controller.transition_map_confirmed = False
            controller.round_active = False
            controller.players = {}
            controller.sink = Sink()

            task = asyncio.create_task(
                controller._delayed_round_display(
                    delay_seconds=0,
                    allow_intermission=True,
                    expected_map_key="next",
                )
            )
            await asyncio.sleep(0.03)
            self.assertFalse(task.done())

            controller.transition_map_confirmed = True
            self.assertTrue(await asyncio.wait_for(task, timeout=0.2))
            self.assertTrue(
                any("Map: Next | Author: Author" in command for command in controller.sink.commands)
            )
            controller.store.close()

    async def test_repeated_round_started_advances_instead_of_replaying_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(Controller)
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.current = SimpleNamespace(key="current")
            controller.round_started_map_key = "current"
            controller.round_active = True
            controller.transitioning = False
            reasons = []

            async def activate(reason):
                reasons.append(reason)

            controller.activate_next_map = activate
            await controller.handle_line("ROUND_STARTED 2026-08-27 00:00:00 UTC")

            self.assertFalse(controller.round_active)
            self.assertEqual(reasons, ["native repeated the active map"])
            controller.store.close()


class RotationGuardTests(unittest.TestCase):
    def test_current_map_is_discarded_from_queue_and_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(Controller)
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.repository = SimpleNamespace(
                catalog={key: SimpleNamespace(key=key) for key in ("a", "b", "c")}
            )
            controller.current = SimpleNamespace(key="c")
            controller.queue = collections.deque(["c", "a"])
            controller.rotation = collections.deque(["c", "b"])
            controller.cycle_played = {"c"}

            self.assertEqual(controller._take_next().key, "a")
            self.assertNotIn("c", controller.queue)

            controller.queue.clear()
            controller.rotation = collections.deque(["c"])
            self.assertIn(controller._take_next().key, {"a", "b"})
            controller.store.close()

    def test_only_current_map_available_refuses_to_repeat_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(Controller)
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.repository = SimpleNamespace(
                catalog={"only": SimpleNamespace(key="only")}
            )
            controller.current = SimpleNamespace(key="only")
            controller.queue = collections.deque(["only"])
            controller.rotation = collections.deque(["only"])
            controller.cycle_played = {"only"}

            self.assertIsNone(controller._take_next())
            controller.store.close()


if __name__ == "__main__":
    unittest.main()
