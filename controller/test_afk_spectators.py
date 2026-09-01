import unittest
from unittest import mock

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
    controller.aliases = {player.log_name.casefold(): player}
    controller.extend_votes = set()
    controller.skip_votes = set()
    controller.round_active = False
    controller.transitioning = False
    controller.final_countdown_active = False
    controller.respawns_paused = False
    controller.respawn_tasks = {}

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

    async def test_idle_dead_racer_is_checked_and_announced_afk(self):
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

        self.assertTrue(racer.afk)
        messages = [plain_console_text(item) for item in controller.sink.commands]
        self.assertTrue(any("Racer is now AFK" in item for item in messages))

    async def test_focus_or_native_idle_reset_does_not_clear_afk(self):
        racer = Player(
            "racer",
            "Racer",
            connected=True,
            active=True,
            alive=True,
            respawn_enabled=True,
            afk=True,
            last_activity_monotonic=50.0,
            last_activity_position=(0.0, 0.0),
            activity_cycle_alive=True,
            activity_snapshot_seen=True,
        )
        controller = controller_with(racer)

        with mock.patch("TronnerRacing.time.monotonic", return_value=100.0):
            await controller._handle_player_activity_snapshot("racer 0 1 0 0")

        self.assertTrue(racer.afk)
        self.assertEqual(controller.sink.commands, [])

    async def test_afk_clears_after_sustained_controlled_motion(self):
        racer = Player(
            "racer",
            "Racer",
            connected=True,
            active=True,
            alive=True,
            respawn_enabled=True,
            afk=True,
            last_activity_monotonic=50.0,
            last_activity_position=(0.0, 0.0),
            activity_cycle_alive=True,
            activity_snapshot_seen=True,
        )
        controller = controller_with(racer)
        controller.config.update(
            {
                "afk_recovery_motion_seconds": 3,
                "afk_recovery_distance": 4,
                "afk_recovery_turn_degrees": 15,
            }
        )

        samples = [
            (100.0, 1.0, 0.0),
            (101.0, 2.0, 0.0),
            (102.0, 3.0, 1.0),
            (103.0, 4.0, 2.0),
        ]
        for now, x, y in samples:
            with mock.patch("TronnerRacing.time.monotonic", return_value=now):
                await controller._handle_player_activity_snapshot(
                    f"racer 0 1 {x} {y}"
                )

        self.assertFalse(racer.afk)
        messages = [plain_console_text(item) for item in controller.sink.commands]
        self.assertTrue(any("Racer is no longer AFK" in item for item in messages))

    async def test_motion_confined_to_small_area_becomes_afk(self):
        racer = Player(
            "racer",
            "Racer",
            connected=True,
            active=True,
            alive=True,
            respawn_enabled=True,
            last_activity_monotonic=99.0,
            last_activity_position=(0.0, 0.0),
            activity_cycle_alive=True,
            activity_snapshot_seen=True,
        )
        controller = controller_with(racer)
        controller.config.update(
            {
                "afk_confinement_seconds": 4,
                "afk_confinement_maximum_span": 3,
            }
        )

        for now, x, y in [
            (100.0, 1.0, 0.0),
            (102.0, 0.0, 1.0),
            (104.0, -1.0, 0.0),
        ]:
            with mock.patch("TronnerRacing.time.monotonic", return_value=now):
                await controller._handle_player_activity_snapshot(
                    f"racer 0 1 {x} {y}"
                )

        self.assertTrue(racer.afk)

    async def test_repeated_straight_line_deaths_become_afk(self):
        racer = Player(
            "racer",
            "Racer",
            connected=True,
            active=True,
            alive=True,
            respawn_enabled=True,
        )
        controller = controller_with(racer)
        controller.config.update(
            {
                "afk_straight_death_limit": 3,
                "afk_straight_minimum_distance": 5,
            }
        )

        for attempt in range(3):
            racer.alive = True
            racer.activity_run_samples.extend(
                [
                    (float(attempt * 10), 0.0, 0.0),
                    (float(attempt * 10 + 1), 8.0, 0.0),
                ]
            )
            with mock.patch(
                "TronnerRacing.time.monotonic", return_value=attempt * 10 + 2
            ):
                await controller._handle_cycle_destroyed("racer")

        self.assertTrue(racer.afk)


if __name__ == "__main__":
    unittest.main()
