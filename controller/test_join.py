import asyncio
import unittest
from pathlib import Path

from TronnerRacing import MapEntry, Player, SpawnPoint, TronnerRacing


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


def arrival_controller():
    controller = object.__new__(TronnerRacing)
    controller.current = MapEntry(
        "map",
        "Map",
        "Author",
        "v1",
        "maps",
        "map",
        Path("map"),
        (SpawnPoint(3, 4, 0, 1),),
    )
    controller.config = {"freeze_seconds": 30, "freeze_tick_seconds": 1}
    controller.sink = Sink()
    controller.players = {}
    controller.aliases = {}
    controller.respawn_tasks = {}
    controller.freeze_tasks = {}
    controller.center_clear_tasks = {}
    controller.spawn_preferences = {}
    controller.online_snapshot_misses = {}
    controller.command_windows = {}
    controller.command_warning_times = {}
    controller.finalists = set()
    controller.round_active = True
    controller.final_countdown_active = False
    controller.transitioning = False
    controller.last_game_time = None
    controller.last_game_monotonic = None
    return controller


async def finish_spawn(controller, player):
    await controller.respawn_tasks[id(player)]
    freeze = controller.freeze_tasks.pop(id(player), None)
    if freeze:
        freeze.cancel()
        await asyncio.gather(freeze, return_exceptions=True)


class JoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_stale_online_snapshot_does_not_cancel_new_arrival(self):
        controller = arrival_controller()

        await controller._handle_player_arrival(
            "racer 127.0.0.1 Racer", False, force_racing=True
        )
        player = controller.players["racer"]
        await controller.respawn_tasks[id(player)]

        controller._bootstrap_players_from_lines([], authoritative=True)

        self.assertTrue(player.connected)
        self.assertTrue(player.active)
        self.assertTrue(player.pending_respawn)
        self.assertIn(id(player), controller.freeze_tasks)

        controller._bootstrap_players_from_lines(
            ["racer 1 25 0 20 Racer"], authoritative=True
        )
        self.assertNotIn(id(player), controller.online_snapshot_misses)

        freeze = controller.freeze_tasks.pop(id(player))
        freeze.cancel()
        await asyncio.gather(freeze, return_exceptions=True)

    async def test_two_missing_online_snapshots_confirm_disconnect(self):
        controller = arrival_controller()
        player = Player("racer", "Racer", connected=True, active=True, alive=True)
        controller.players = {"racer": player}
        controller.aliases = {"racer": player}

        controller._bootstrap_players_from_lines([], authoritative=True)
        self.assertTrue(player.connected)

        controller._bootstrap_players_from_lines([], authoritative=True)
        self.assertFalse(player.connected)
        self.assertFalse(player.active)
        self.assertFalse(player.alive)

    async def test_grid_arrival_starts_without_join_command(self):
        controller = arrival_controller()

        await controller._handle_player_arrival(
            "racer 127.0.0.1 Racer", True
        )
        player = controller.players["racer"]
        await finish_spawn(controller, player)

        self.assertTrue(player.active)
        self.assertTrue(player.respawn_enabled)
        self.assertIn(
            "RESPAWN_PLAYER racer false 3 4 0 1", controller.sink.commands
        )

    async def test_initial_spectator_state_races_until_explicit_spec(self):
        controller = arrival_controller()

        await controller._handle_player_arrival(
            "racer 127.0.0.1 Racer", False, force_racing=True
        )
        player = controller.players["racer"]
        await finish_spawn(controller, player)

        self.assertTrue(player.forced_racing)
        controller._handle_online_player("racer 1 0 0 0 20 0 0.1")
        self.assertTrue(player.active)

        controller._handle_player_entered("racer 127.0.0.1 Racer", False)
        await asyncio.sleep(0)
        self.assertFalse(player.forced_racing)
        self.assertFalse(player.active)
        self.assertFalse(player.pending_respawn)

    async def test_join_command_returns_spectator_to_racing(self):
        controller = arrival_controller()
        player = Player(
            "racer", "Racer", connected=True, active=False, alive=False
        )
        controller.players = {"racer": player}
        controller.aliases = {"racer": player}

        await controller._command_respawn(player, kill_first=False)

        self.assertTrue(player.forced_racing)
        self.assertTrue(player.active)
        self.assertIn(
            "RESPAWN_PLAYER racer false 3 4 0 1", controller.sink.commands
        )


if __name__ == "__main__":
    unittest.main()
