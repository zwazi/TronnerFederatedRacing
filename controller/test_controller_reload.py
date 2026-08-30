import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from TronnerRacing import Player, StateStore, TronnerRacing, plain_console_text


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class BlockingScoreSink(Sink):
    def __init__(self):
        super().__init__()
        self.score_started = asyncio.Event()
        self.release_score = asyncio.Event()

    async def send(self, *commands):
        self.commands.extend(commands)
        if any(command.startswith("ADD_SCORE_PLAYER ") for command in commands):
            self.score_started.set()
            await self.release_score.wait()


class ControllerReloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_countdown_finish_is_retained_until_record_and_message_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(TronnerRacing)
            controller.sink = BlockingScoreSink()
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            original_map = SimpleNamespace(key="original-map")
            controller.current = original_map
            controller.config = {"maximum_record_seconds": 7200}
            player = Player(
                "racer",
                "Racer",
                connected=True,
                active=True,
                alive=True,
                attempt_started_game=10.0,
            )
            controller.players = {"racer": player}
            controller.aliases = {"racer": player}
            controller.finalists = {id(player)}
            controller.finishes_in_progress = set()

            task = asyncio.create_task(
                controller._handle_winzone(
                    "1 finish 0 0 racer 0 0 1 0 22.345"
                )
            )
            await controller.sink.score_started.wait()

            self.assertFalse(player.alive)
            self.assertEqual(controller._alive_finalists(), [player])
            # Even an unrelated transition cannot reattribute an accepted
            # finish after its winzone event has captured the original map.
            controller.current = SimpleNamespace(key="next-map")
            controller.sink.release_score.set()
            await task

            self.assertEqual(
                len(controller.store.records(original_map.key)), 1
            )
            self.assertEqual(controller.store.records("next-map"), [])
            self.assertTrue(
                any(
                    "Finish: 12.345" in plain_console_text(command)
                    for command in controller.sink.commands
                )
            )
            self.assertNotIn(id(player), controller.finishes_in_progress)
            controller.store.close()

    async def test_held_finalist_can_press_brake_during_countdown(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        player = Player(
            "racer",
            "Racer",
            connected=True,
            active=True,
            alive=True,
            pending_respawn=True,
            respawn_created_game=11.0,
            pending_start_mode="countdown",
        )
        controller.players = {"racer": player}
        controller.aliases = {"racer": player}
        controller.final_countdown_active = True
        controller.transitioning = False
        controller.finalists = {id(player)}
        controller.respawns_paused = False
        shown = []

        async def show_go(candidate):
            shown.append(candidate)

        controller._show_go = show_go
        await controller._handle_cycle_released("racer 12.5")

        self.assertEqual(player.attempt_started_game, 12.5)
        self.assertFalse(player.pending_respawn)
        self.assertEqual(shown, [player])

    async def test_reload_drain_waits_for_active_run_and_freezes_map_timer(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        stored = {}
        controller.store = SimpleNamespace(
            set_json=lambda key, value: stored.__setitem__(key, value)
        )
        controller.config = {}
        controller.current = SimpleNamespace(key="map")
        controller.deadline_epoch = time.time() + 120
        controller.final_countdown_active = False
        controller.final_countdown_end_epoch = None
        controller.players = {}
        player = Player("racer", "Racer", alive=True)
        controller.players["racer"] = player
        controller.respawn_tasks = {}
        controller.freeze_tasks = {}
        controller.center_clear_tasks = {}
        controller.finishes_in_progress = set()
        controller.stop_event = asyncio.Event()
        announcements = []

        async def broadcast(message, **options):
            announcements.append((message, options))

        controller.broadcast = broadcast

        task = asyncio.create_task(
            controller._drain_for_controller_reload("Admin")
        )
        await asyncio.sleep(0)

        self.assertTrue(controller.respawns_paused)
        self.assertTrue(controller.controller_reload_draining)
        self.assertFalse(controller.stop_event.is_set())
        self.assertTrue(stored["controller_reload"]["pending"])
        self.assertEqual(stored["controller_reload"]["map_key"], "map")

        player.alive = False
        await task
        self.assertTrue(controller.stop_event.is_set())
        self.assertEqual(len(announcements), 2)
        self.assertTrue(all(item[1].get("federate") is False for item in announcements))

    async def test_reload_resume_restores_remaining_time_and_respawns_players(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        stored = {}
        controller.store = SimpleNamespace(
            set_json=lambda key, value: stored.__setitem__(key, value)
        )
        controller.config = {"controller_reload_resume_grace_seconds": 5}
        controller.current = SimpleNamespace(key="map")
        controller.deadline_epoch = None
        controller.final_countdown_active = False
        controller.final_countdown_end_epoch = None
        controller.round_active = True
        controller.transitioning = False
        controller.respawn_tasks = {}
        player = Player("racer", "Racer", alive=False)
        controller.players = {"racer": player}
        controller.controller_reload_state = {
            "pending": True,
            "map_key": "map",
            "deadline_remaining": 90,
            "final_countdown_remaining": None,
            "resume_identity_keys": [player.identity_key],
        }
        controller.respawns_paused = True
        controller.controller_reload_draining = False
        scheduled = []
        announcements = []

        def schedule(candidate, delay_seconds=None):
            scheduled.append((candidate, delay_seconds))

        async def broadcast(message, **options):
            announcements.append((message, options))

        controller._schedule_respawn = schedule
        controller.broadcast = broadcast
        before = time.time()
        await controller._resume_controller_reload()

        self.assertFalse(controller.respawns_paused)
        self.assertGreaterEqual(controller.deadline_epoch, before + 90)
        self.assertEqual(scheduled, [(player, 0.1)])
        self.assertEqual(stored["controller_reload"], {})
        self.assertEqual(len(announcements), 1)
        self.assertIs(announcements[0][1].get("federate"), False)

    async def test_ordinary_mid_round_startup_recovers_dead_racers(self):
        controller = object.__new__(TronnerRacing)
        controller.controller_reload_state = {}
        controller.respawns_paused = False
        controller.current = SimpleNamespace(key="map")
        controller.round_active = True
        controller.transitioning = False
        controller.final_countdown_active = False
        controller.respawn_tasks = {}
        racer = Player("racer", "Racer", alive=False)
        spectator = Player(
            "spectator",
            "Spectator",
            active=False,
            alive=False,
            respawn_enabled=False,
        )
        alive = Player("alive", "Alive", alive=True)
        ai = Player("ai", "AI", alive=False, is_ai=True)
        controller.players = {
            player.log_name: player
            for player in (racer, spectator, alive, ai)
        }
        scheduled = []

        def schedule(candidate, delay_seconds=None):
            scheduled.append((candidate, delay_seconds))

        controller._schedule_respawn = schedule
        await controller._resume_controller_reload()

        self.assertEqual(scheduled, [(racer, 0.1)])

    def test_startup_recovery_is_blocked_during_final_countdown(self):
        controller = object.__new__(TronnerRacing)
        controller.current = SimpleNamespace(key="map")
        controller.round_active = True
        controller.transitioning = False
        controller.final_countdown_active = True
        controller.respawns_paused = False
        controller.respawn_tasks = {}
        racer = Player("racer", "Racer", alive=False)
        controller.players = {"racer": racer}
        scheduled = []
        controller._schedule_respawn = lambda *args, **kwargs: scheduled.append(
            (args, kwargs)
        )

        self.assertEqual(controller._schedule_startup_respawns(), 0)
        self.assertEqual(scheduled, [])

    async def test_map_advance_is_deferred_while_reload_is_draining(self):
        controller = object.__new__(TronnerRacing)
        controller.controller_reload_draining = True
        # No map/repository/lock setup is intentional: returning before those
        # accesses proves the empty handoff cannot reset the map.
        await controller.activate_next_map("empty arena")


if __name__ == "__main__":
    unittest.main()
