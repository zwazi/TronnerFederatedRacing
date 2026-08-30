import asyncio
import unittest
from pathlib import Path

from TronnerRacing import MapEntry, Player, SpawnPoint, TronnerRacing


def controller_with(*players):
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
    controller.config = {
        "respawn_delay_seconds": 2.0,
        "empty_arena_respawn_delay_seconds": 0.1,
    }
    controller.players = {player.log_name: player for player in players}
    controller.aliases = {
        player.log_name.casefold(): player for player in players
    }
    controller.round_active = True
    controller.transitioning = False
    controller.final_countdown_active = False
    controller.respawn_tasks = {}
    controller.freeze_tasks = {}
    scheduled = []

    def schedule(player, delay_seconds=None):
        scheduled.append((player, delay_seconds))

    controller._schedule_respawn = schedule
    return controller, scheduled


class SoloRespawnTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_alive_racer_uses_fast_empty_arena_respawn(self):
        player = Player("solo", "Solo", alive=True)
        controller, scheduled = controller_with(player)

        await controller._handle_cycle_destroyed("solo 1 2 1 0 Solo 10 DEATHZONE")

        self.assertFalse(player.alive)
        self.assertEqual(scheduled, [(player, 0.1)])

    async def test_normal_delay_is_retained_while_another_racer_is_alive(self):
        player = Player("first", "First", alive=True)
        other = Player("second", "Second", alive=True)
        controller, scheduled = controller_with(player, other)

        await controller._handle_cycle_destroyed("first 1 2 1 0 First 10 DEATHZONE")

        self.assertEqual(scheduled, [(player, None)])

    async def test_destroyed_held_spawn_is_rescheduled(self):
        player = Player(
            "solo",
            "Solo",
            alive=True,
            generation=2,
            pending_respawn=True,
            respawn_created_game=12.5,
        )
        controller, scheduled = controller_with(player)
        freeze = asyncio.create_task(asyncio.sleep(60))
        controller.freeze_tasks[id(player)] = freeze

        await controller._handle_cycle_destroyed("solo 3 4 0 1 Solo 12.6 DEATHZONE")
        await asyncio.sleep(0)

        self.assertFalse(player.pending_respawn)
        self.assertIsNone(player.respawn_created_game)
        self.assertEqual(player.generation, 3)
        self.assertTrue(freeze.cancelled())
        self.assertEqual(scheduled, [(player, 0.1)])


if __name__ == "__main__":
    unittest.main()
