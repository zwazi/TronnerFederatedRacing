import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from TronnerRacing import (
    Player,
    StateStore,
    TronnerRacing,
    format_finish_message,
    plain_console_text,
)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class RequestedBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def test_initial_finish_omits_best_and_split(self):
        message = format_finish_message(
            "0xff0000Racer",
            12.345,
            1,
            12.345,
            1,
            None,
            42,
            42,
            None,
        )

        self.assertEqual(
            plain_console_text(message),
            "Racer - Finish: 12.345, Turns: 42, Rank: 1",
        )
        self.assertNotIn("Best:", message)
        self.assertNotIn("Split:", message)

    async def test_pending_skip_vote_includes_command_guidance(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        controller.skip_votes = set()
        controller.current = SimpleNamespace(key="map")
        controller.round_active = True
        controller.transitioning = False
        controller.final_countdown_active = False
        controller.final_countdown_announcement = None
        racers = [Player(f"racer{index}", f"Racer {index}") for index in range(3)]
        controller.players = {player.log_name: player for player in racers}

        await controller._command_skip(racers[0])

        self.assertIn(
            "CONSOLE_MESSAGE Skip vote: 1/2 required. "
            "Type /skip to go to the next map",
            [plain_console_text(command) for command in controller.sink.commands],
        )

    async def test_passed_skip_starts_countdown_and_blocks_respawns(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        controller.skip_votes = set()
        controller.current = SimpleNamespace(key="map")
        controller.round_active = True
        controller.transitioning = False
        controller.final_countdown_active = False
        controller.final_countdown_end_epoch = 123.0
        controller.final_countdown_map_key = None
        controller.final_countdown_announcement = None
        racer = Player("racer", "Racer")
        controller.players = {racer.log_name: racer}
        writes = {}
        controller.store = SimpleNamespace(
            set_json=lambda key, value: writes.__setitem__(key, value)
        )

        await controller._command_skip(racer)

        self.assertTrue(controller.final_countdown_active)
        self.assertIsNone(controller.final_countdown_end_epoch)
        self.assertEqual(controller.final_countdown_map_key, "map")
        self.assertEqual(controller.final_countdown_announcement, "Skip vote passed.")
        self.assertEqual(
            writes,
            {
                "final_countdown_active": True,
                "final_countdown_end_epoch": None,
                "final_countdown_map_key": "map",
            },
        )
        self.assertNotIn("CONSOLE_MESSAGE Skip vote passed.", controller.sink.commands)

    async def test_end_requires_admin_and_requests_existing_countdown(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        controller.config = {"map_admin_access_level": 1}
        controller.current = SimpleNamespace(key="map")
        controller.round_active = True
        controller.transitioning = False
        controller.final_countdown_active = False
        controller.final_countdown_announcement = None
        controller.deadline_epoch = time.time() + 300
        writes = {}
        controller.store = SimpleNamespace(
            set_json=lambda key, value: writes.__setitem__(key, value)
        )
        admin = Player("admin", "Admin")

        await controller._command_end(admin, 20)
        self.assertIsNone(controller.final_countdown_announcement)
        self.assertIn(
            'PLAYER_MESSAGE admin "Only an Owner or Admin may start the '
            'end-of-map timer."',
            [plain_console_text(command) for command in controller.sink.commands],
        )

        before = time.time()
        await controller._command_end(admin, 1)
        self.assertIsNone(controller.final_countdown_announcement)
        self.assertGreaterEqual(controller.deadline_epoch, before)
        self.assertLessEqual(controller.deadline_epoch, time.time())
        self.assertEqual(writes["deadline_epoch"], controller.deadline_epoch)

    async def test_manual_end_uses_normal_timer_announcement(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(TronnerRacing)
            controller.sink = Sink()
            controller.store = StateStore(Path(tmp) / "countdown.sqlite3")
            controller.current = SimpleNamespace(key="manual-map")
            controller.transitioning = False
            controller.final_countdown_active = False
            controller.final_countdown_end_epoch = None
            controller.final_countdown_map_key = None
            controller.final_countdown_announcement = None
            controller.finalists = set()
            controller.respawn_tasks = {}
            controller.freeze_tasks = {}
            controller.players = {}
            controller.federation_remote_players = {}
            controller.federation_role = "off"

            reasons = []

            async def activate(reason):
                reasons.append(reason)
                controller.final_countdown_active = False

            controller.activate_next_map = activate
            try:
                await controller._run_final_countdown()
                self.assertIn(
                    "CONSOLE_MESSAGE Map time expired. "
                    "Respawning is disabled. Final countdown: 90 seconds. "
                    "Use /rate # for the current map or /rate [map] # "
                    "for a specific map.",
                    [
                        plain_console_text(command)
                        for command in controller.sink.commands
                    ],
                )
                self.assertEqual(
                    reasons, ["all racers finished final countdown"]
                )
            finally:
                controller.store.close()


if __name__ == "__main__":
    unittest.main()
