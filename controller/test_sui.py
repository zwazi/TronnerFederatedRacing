import unittest

from TronnerRacing import Player, TronnerRacing as Controller


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class SuiAliasTests(unittest.IsolatedAsyncioTestCase):
    async def test_sui_dispatches_exactly_like_respawn(self):
        controller = object.__new__(Controller)
        player = Player("racer", "Racer")
        controller.players = {"racer": player}
        controller.aliases = {"racer": player}
        controller.config = {
            "command_rate_window_seconds": 5,
            "command_rate_maximum": 4,
        }
        controller.command_windows = {}
        controller.command_warning_times = {}
        controller.hot_commands = None
        controller.sink = Sink()
        calls = []

        async def respawn(target, kill_first):
            calls.append((target, kill_first))

        controller._command_respawn = respawn

        await controller._handle_command("/sui racer 127.0.0.1 20")
        await controller._handle_command("/respawn racer 127.0.0.1 20")

        self.assertEqual(calls, [(player, True), (player, True)])


if __name__ == "__main__":
    unittest.main()
