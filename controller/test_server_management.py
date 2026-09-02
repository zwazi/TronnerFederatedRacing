import asyncio
import collections
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from TronnerRacing import Player, TronnerRacing


class FakeSink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class ServerManagementTest(unittest.IsolatedAsyncioTestCase):
    def controller(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = FakeSink()
        controller.players = {}
        controller.aliases = {}
        return controller

    def test_status_uses_the_repository_catalog_version(self):
        controller = self.controller()
        controller.started_at_epoch = time.time() - 10
        controller.queue = collections.deque()
        controller.rotation = collections.deque()
        controller.cycle_played = set()
        controller.repository = SimpleNamespace(catalog={}, firebase_catalog_version=14)
        controller.store = SimpleNamespace(path=Path("/tmp/tronner-management-test/state.sqlite3"))
        controller.config = {"live_dashboard": {"local_region": "A"}}
        controller.round_active = False
        controller.transitioning = False
        controller.final_countdown_active = False
        controller.server_restart_active = False
        controller.controller_reload_state = {}
        controller.respawns_paused = False
        controller.deadline_epoch = None
        controller.round_started_epoch = None
        controller.current = None
        controller.excluded_map_keys = set()
        controller.last_game_monotonic = None

        status = controller._server_management_status()

        self.assertEqual(status["catalogVersion"], 14)
        self.assertEqual(status["region"], "A")
        self.assertEqual(status["playerCount"], 0)

    async def test_runtime_options_are_allowlisted_and_range_checked(self):
        controller = self.controller()
        command = {
            "type": "set_engine_option",
            "requestedBy": "admin-id",
            "requestedName": "Grid admin",
            "option": "CYCLE_SPEED",
            "value": 42.5,
        }

        result, details = await controller._execute_server_management_command(command)

        self.assertEqual(controller.sink.commands, ["CYCLE_SPEED 42.5"])
        self.assertIn("until the server is restarted", result)
        self.assertEqual(details, {"option": "CYCLE_SPEED", "value": 42.5})

        command["option"] = "INCLUDE"
        command["value"] = 1
        with self.assertRaisesRegex(ValueError, "not available"):
            await controller._execute_server_management_command(command)

        command["option"] = "MAX_CLIENTS"
        command["value"] = 1000
        with self.assertRaisesRegex(ValueError, "between 1 and 64"):
            await controller._execute_server_management_command(command)

        self.assertEqual(controller.sink.commands, ["CYCLE_SPEED 42.5"])

    async def test_raw_console_command_is_one_bounded_line(self):
        controller = self.controller()
        result, details = await controller._execute_server_management_command({
            "type": "console_command",
            "requestedBy": "admin-id",
            "requestedName": "Grid admin",
            "message": "CYCLE_SPEED 30",
        })
        self.assertEqual(result, "Server console command sent.")
        self.assertEqual(details, {"command": "CYCLE_SPEED 30"})
        self.assertEqual(controller.sink.commands, ["CYCLE_SPEED 30"])

        with self.assertRaisesRegex(ValueError, "one line"):
            await controller._execute_server_management_command({
                "type": "console_command",
                "requestedBy": "admin-id",
                "requestedName": "Grid admin",
                "message": "CYCLE_SPEED 30\nQUIT",
            })

    async def test_restart_server_starts_normal_countdown(self):
        controller = self.controller()
        controller.request_server_restart = lambda requested_by: 37.2
        result, details = await controller._execute_server_management_command({
            "type": "restart_server",
            "requestedBy": "admin-id",
            "requestedName": "Grid admin",
        })
        self.assertEqual(
            result, "Server restart countdown started for 38 seconds."
        )
        self.assertEqual(details, {"countdownSeconds": 38})

    async def test_moderation_targets_only_connected_snapshot_players(self):
        controller = self.controller()
        player = Player("player-7", "Racer Seven")
        controller.players[player.log_name.casefold()] = player
        controller.aliases[player.log_name.casefold()] = player

        result, _details = await controller._execute_server_management_command({
            "type": "kick",
            "requestedBy": "admin-id",
            "requestedName": "Grid admin",
            "target": "player-7",
            "reason": "Repeated blocking\nINCLUDE secrets.cfg",
        })

        self.assertEqual(result, "Kicked Racer Seven.")
        self.assertEqual(
            controller.sink.commands,
            ['KICK "player-7" Repeated blocking INCLUDE secrets.cfg'],
        )

        with self.assertRaisesRegex(ValueError, "no longer connected"):
            await controller._execute_server_management_command({
                "type": "kill",
                "requestedBy": "admin-id",
                "requestedName": "Grid admin",
                "target": "not-connected",
            })

        self.assertEqual(len(controller.sink.commands), 1)

    async def test_announcement_targets_the_local_server(self):
        controller = self.controller()
        controller.broadcast = AsyncMock()

        result, details = await controller._execute_server_management_command({
            "type": "announce",
            "requestedBy": "admin-id",
            "requestedName": "Grid admin",
            "message": "Server maintenance soon.",
            "scope": "local",
        })

        self.assertEqual(result, "Announcement delivered.")
        self.assertEqual(details, {"scope": "local"})
        controller.broadcast.assert_awaited_once_with("Server maintenance soon.")
        self.assertTrue(controller.live_dashboard_authority)

    async def test_console_stream_is_temporary_and_replays_only_a_bounded_tail(self):
        controller = self.controller()
        controller.server_console_entries = collections.deque(
            ({"sequence": index, "at": index, "message": f"line {index}"}
             for index in range(1, 151)),
            maxlen=250,
        )
        controller.server_console_sequence = 150
        controller.server_console_last_published_sequence = 0
        controller.server_console_stream_until_monotonic = 0.0

        result, details = await controller._execute_server_management_command({
            "type": "start_console_stream",
            "requestedBy": "admin-id",
            "requestedName": "Grid admin",
        })

        self.assertIn("90 seconds", result)
        self.assertEqual(details, {"streamSeconds": 90})
        self.assertEqual(controller.server_console_last_published_sequence, 50)
        self.assertGreater(
            controller.server_console_stream_until_monotonic,
            time.monotonic(),
        )

    def test_console_output_redacts_secret_bearing_lines(self):
        self.assertEqual(
            TronnerRacing._sanitize_server_console_line(
                "[2026/08/30-12:00:00] API_KEY=do-not-publish"
            ),
            "[sensitive console output withheld]",
        )
        self.assertEqual(
            TronnerRacing._sanitize_server_console_line(
                "[2026/08/30-12:00:00] [0] Racer entered the game."
            ),
            "[0] Racer entered the game.",
        )

    async def test_console_follower_enables_engine_log_and_reads_output(self):
        controller = self.controller()
        controller.config = {"live_dashboard": {"management_enabled": True}}
        controller.live_dashboard_chat = object()
        controller.stop_event = asyncio.Event()
        controller.server_console_entries = collections.deque(maxlen=250)
        controller.server_console_sequence = 0
        controller.server_console_available = False

        with tempfile.TemporaryDirectory() as directory:
            controller.server_console_path = Path(directory) / "consolelog.txt"
            controller.server_console_path.write_bytes(
                b"[2026/08/30-12:00:00] [0] New Match\n"
            )
            task = asyncio.create_task(controller.follow_server_console())
            for _attempt in range(50):
                if controller.server_console_entries:
                    break
                await asyncio.sleep(0.01)
            controller.stop_event.set()
            await asyncio.wait_for(task, timeout=1.0)

        self.assertEqual(controller.sink.commands, ["CONSOLE_LOG 1"])
        self.assertTrue(controller.server_console_available)
        self.assertEqual(
            list(controller.server_console_entries)[0]["message"],
            "[0] New Match",
        )


if __name__ == "__main__":
    unittest.main()
