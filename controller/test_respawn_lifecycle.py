import asyncio
import unittest

from TronnerRacing import Player, TronnerRacing


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


def lifecycle_controller(player):
    controller = object.__new__(TronnerRacing)
    controller.config = {}
    controller.sink = Sink()
    controller.players = {player.log_name.casefold(): player}
    controller.aliases = {player.log_name.casefold(): player}
    controller.online_snapshot_misses = {}
    controller.respawn_tasks = {}
    controller.freeze_tasks = {}
    controller.center_clear_tasks = {}
    controller.finalists = set()
    controller.round_active = True
    controller.transitioning = False
    controller.final_countdown_active = False
    return controller


class RespawnLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_alive_file_cannot_cancel_scheduled_respawn(self):
        player = Player(
            "racer",
            "Racer",
            connected=True,
            active=True,
            alive=False,
        )
        controller = lifecycle_controller(player)
        respawned = []

        async def respawn(candidate):
            respawned.append(candidate)

        controller._respawn_player = respawn
        controller._schedule_respawn(player, delay_seconds=0)
        task = controller.respawn_tasks[id(player)]

        controller._bootstrap_players_from_lines(
            ["racer 1 25 0 20 Racer"], authoritative=True
        )

        self.assertFalse(player.alive)
        await task
        self.assertEqual(respawned, [player])

    async def test_alive_file_recovers_a_disconnected_player(self):
        player = Player(
            "racer",
            "Racer",
            connected=False,
            active=False,
            alive=False,
        )
        controller = lifecycle_controller(player)

        controller._bootstrap_players_from_lines(
            ["racer 1 25 0 20 Racer"], authoritative=True
        )

        self.assertTrue(player.connected)
        self.assertTrue(player.alive)

    async def test_team_menu_spectator_disables_until_menu_reentry(self):
        player = Player(
            "racer",
            "Racer",
            connected=True,
            active=True,
            alive=True,
            forced_racing=True,
        )
        controller = lifecycle_controller(player)
        respawn_task = asyncio.create_task(asyncio.sleep(60))
        freeze_task = asyncio.create_task(asyncio.sleep(60))
        controller.respawn_tasks[id(player)] = respawn_task
        controller.freeze_tasks[id(player)] = freeze_task

        await controller.handle_line("PLAYER_JOINS_SPECTATORS racer Racer\n")
        await asyncio.sleep(0)

        self.assertFalse(player.active)
        self.assertFalse(player.alive)
        self.assertFalse(player.forced_racing)
        self.assertFalse(player.respawn_enabled)
        self.assertNotIn(id(player), controller.respawn_tasks)
        self.assertNotIn(id(player), controller.freeze_tasks)
        self.assertTrue(respawn_task.cancelled())
        self.assertTrue(freeze_task.cancelled())

        controller._bootstrap_players_from_lines(
            ["racer 1 25 0 20 Racer"], authoritative=True
        )
        self.assertFalse(player.alive)

        scheduled = []

        def schedule(candidate, delay_seconds=None):
            scheduled.append((candidate, delay_seconds))

        controller._schedule_respawn = schedule
        await controller.handle_line("PLAYER_LEAVES_SPECTATORS racer Racer\n")

        self.assertTrue(player.active)
        self.assertTrue(player.respawn_enabled)
        self.assertEqual(player.display_name, "Racer")
        self.assertEqual(scheduled, [(player, 0.0)])


if __name__ == "__main__":
    unittest.main()
