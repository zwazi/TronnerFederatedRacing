import unittest

from TronnerRacing import Player, TronnerRacing, plain_console_text


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


def controller_with(player: Player) -> TronnerRacing:
    controller = object.__new__(TronnerRacing)
    controller.config = {"afk_timeout_seconds": 60}
    controller.sink = Sink()
    controller.players = {player.log_name: player}
    controller.extend_votes = set()
    controller.skip_votes = set()

    async def resolve_votes():
        return None

    controller._resolve_votes_after_eligibility_change = resolve_votes
    return controller


class SpectatorAfkTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_spectator_is_not_marked_or_announced_afk(self):
        spectator = Player(
            "spectator",
            "Spectator",
            connected=True,
            active=False,
            respawn_enabled=False,
            last_activity_monotonic=100.0,
        )
        controller = controller_with(spectator)

        await controller._check_afk_players(now=200.0)

        self.assertFalse(spectator.afk)
        self.assertEqual(controller.sink.commands, [])

    async def test_spectator_afk_state_clears_without_announcement(self):
        spectator = Player(
            "spectator",
            "Spectator",
            connected=True,
            active=False,
            respawn_enabled=False,
            afk=True,
        )
        controller = controller_with(spectator)

        await controller._record_player_activity(spectator, 200.0)

        self.assertFalse(spectator.afk)
        self.assertEqual(controller.sink.commands, [])

    async def test_idle_active_racer_still_announces_afk(self):
        racer = Player(
            "racer",
            "Racer",
            connected=True,
            active=True,
            alive=True,
            respawn_enabled=True,
            last_activity_monotonic=100.0,
        )
        controller = controller_with(racer)

        await controller._check_afk_players(now=200.0)

        self.assertTrue(racer.afk)
        messages = [plain_console_text(item) for item in controller.sink.commands]
        self.assertTrue(any("Racer is now AFK" in item for item in messages))

    async def test_idle_dead_racer_is_not_checked_or_announced_afk(self):
        racer = Player(
            "racer",
            "Racer",
            connected=True,
            active=True,
            alive=False,
            respawn_enabled=True,
            last_activity_monotonic=100.0,
        )
        controller = controller_with(racer)

        await controller._check_afk_players(now=200.0)
        await controller._set_player_afk(racer)

        self.assertFalse(racer.afk)
        self.assertEqual(controller.sink.commands, [])


if __name__ == "__main__":
    unittest.main()
