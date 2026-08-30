import asyncio
import unittest
from pathlib import Path

from TronnerRacing import (
    MapEntry,
    Player,
    SpawnPoint,
    TronnerRacing,
    plain_console_text,
)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


def spawn_controller():
    controller = object.__new__(TronnerRacing)
    controller.current = MapEntry(
        "map",
        "Map",
        "Author",
        "v1",
        "maps",
        "map",
        Path("map"),
        (
            SpawnPoint(1, 2, 1, 0),
            SpawnPoint(3, 4, 0, 1),
            SpawnPoint(5, 6, -1, 0),
        ),
    )
    controller.config = {}
    controller.sink = Sink()
    controller.freeze_tasks = {}
    controller.final_countdown_active = False
    controller.transitioning = False
    controller.spawn_preferences = {}
    controller._save_spawn_preferences = lambda: None
    return controller


async def cancel_freeze(controller, player):
    task = controller.freeze_tasks.pop(id(player), None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    player.pending_respawn = False


class SetSpawnTests(unittest.IsolatedAsyncioTestCase):
    async def test_numbered_preference_is_fixed_across_respawns(self):
        controller = spawn_controller()
        player = Player("racer", "Racer")

        await controller._command_setspawn(player, "2")
        await controller._respawn_player(player)
        await cancel_freeze(controller, player)
        await controller._respawn_player(player)
        await cancel_freeze(controller, player)

        respawns = [
            command
            for command in controller.sink.commands
            if command.startswith("RESPAWN_PLAYER")
        ]
        self.assertEqual(
            respawns,
            [
                "RESPAWN_PLAYER racer false 3 4 0 1",
                "RESPAWN_PLAYER racer false 3 4 0 1",
            ],
        )

    async def test_no_argument_saves_most_recent_spawn(self):
        controller = spawn_controller()
        player = Player("racer", "Racer")
        controller.players = {"racer": player}
        controller.aliases = {"racer": player}

        controller._handle_cycle_created("racer 5 6 -1 0 12.5")

        await controller._command_setspawn(player, "")

        self.assertEqual(
            controller.spawn_preferences["logical:author/maps/map"][player.identity_key],
            3,
        )
        self.assertIn(
            'PLAYER_MESSAGE racer "Spawn #3 saved for Map. It will be used for every respawn on this map."',
            [plain_console_text(command) for command in controller.sink.commands],
        )

    async def test_no_argument_requires_a_recent_spawn(self):
        controller = spawn_controller()
        player = Player("racer", "Racer")

        await controller._command_setspawn(player, "")

        self.assertNotIn("map", controller.spawn_preferences)
        self.assertIn(
            'PLAYER_MESSAGE racer "No recent spawn is available. Use /setspawn followed by a spawn number."',
            [plain_console_text(command) for command in controller.sink.commands],
        )

    async def test_zero_removes_saved_spawn_and_restores_rotation(self):
        controller = spawn_controller()
        player = Player("racer", "Racer", last_spawn_index=1, spawn_cursor=1)
        controller.spawn_preferences = {"map": {player.identity_key: 2}}

        await controller._command_setspawn(player, "0")

        self.assertNotIn("map", controller.spawn_preferences)
        self.assertEqual(player.spawn_cursor, 2)
        self.assertIn(
            'PLAYER_MESSAGE racer "Saved spawn removed for Map."',
            [plain_console_text(command) for command in controller.sink.commands],
        )


if __name__ == "__main__":
    unittest.main()
