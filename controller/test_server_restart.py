import asyncio
import unittest

from TronnerRacing import Player, TronnerRacing, server_restart_center_command


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class ServerRestartTests(unittest.IsolatedAsyncioTestCase):
    def test_center_countdown_is_red_and_left_positioned(self):
        self.assertEqual(
            server_restart_center_command(12),
            "CENTER_MESSAGE 0xff000012                        0xffffff ",
        )

    async def test_countdown_preserves_active_runs_until_server_exit(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        controller.players = {
            "racer": Player("racer", "Racer", alive=True),
        }
        controller.respawn_tasks = {}
        controller.server_restart_active = True
        controller.respawns_paused = True
        controller._server_restart_task = None
        controller.stop_event = asyncio.Event()
        controller.round_active = True
        controller._clear_all_votes = lambda: None
        controller._set_round_started_map = lambda key: None

        await controller._run_server_restart_countdown(0.01, "Administrator")

        self.assertEqual(
            controller.sink.commands[0],
            "CONSOLE_MESSAGE 0xff0000SERVER RESTARTING IN 1 SECONDS",
        )
        self.assertIn(server_restart_center_command(0), controller.sink.commands)
        self.assertEqual(controller.sink.commands[-1], "QUIT")
        self.assertFalse(any(command == "KILL_SILENT racer"
                             for command in controller.sink.commands))
        self.assertTrue(controller.stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
