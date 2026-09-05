import unittest
from types import SimpleNamespace
from unittest import mock

from TronnerRacing import Player, TronnerRacing, plain_console_text


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class JsonStore:
    def __init__(self):
        self.values = {}

    def set_json(self, key, value):
        self.values[key] = value


def countdown_controller(players):
    controller = object.__new__(TronnerRacing)
    controller.sink = Sink()
    controller.store = JsonStore()
    controller.config = {"extend_seconds": 300}
    controller.current = SimpleNamespace(key="map")
    controller.round_active = True
    controller.transitioning = False
    controller.deadline_epoch = 900.0
    controller.extend_votes = set()
    controller.skip_votes = set()
    controller.extend_vote_generation = 0
    controller.skip_vote_generation = 0
    controller.players = {player.log_name: player for player in players}
    controller.final_countdown_active = True
    controller.final_countdown_end_epoch = 1050.0
    controller.final_countdown_map_key = "map"
    controller.final_countdown_announcement = None
    controller.finalists = {id(player) for player in players if not player.is_ai}
    controller.final_countdown_route_model = object()
    controller.final_countdown_route_map_key = "map"
    controller.final_countdown_route_building = False
    controller.final_countdown_route_prepared = True
    controller.final_countdown_progress_states = {1: object()}
    controller.final_countdown_duration_seconds = 90.0
    controller.final_countdown_acceleration_capability = object()
    controller.final_countdown_acceleration_identifier = "settings"
    controller.respawn_tasks = {}
    return controller


class CountdownExtendVoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_passed_extend_cancels_countdown_and_restores_respawns(self):
        racers = [
            Player("one", "One", owner_id=1),
            Player("two", "Two", owner_id=2),
            Player("ghost", "#1 - One Ghost", owner_id=0, is_ai=False),
        ]
        controller = countdown_controller(racers)
        resumed = []
        controller._schedule_startup_respawns = lambda: resumed.append(True) or 1

        with mock.patch("TronnerRacing.time.time", return_value=1000.0):
            await controller._command_extend(racers[0])

            self.assertTrue(controller.final_countdown_active)
            pending = [plain_console_text(item) for item in controller.sink.commands]
            self.assertIn("CONSOLE_MESSAGE Extend vote: 1/2 required.", pending)

            await controller._command_extend(racers[1])

        self.assertFalse(controller.final_countdown_active)
        self.assertIsNone(controller.final_countdown_end_epoch)
        self.assertIsNone(controller.final_countdown_map_key)
        self.assertEqual(controller.deadline_epoch, 1300.0)
        self.assertEqual(controller.store.values["deadline_epoch"], 1300.0)
        self.assertFalse(controller.store.values["final_countdown_active"])
        self.assertEqual(resumed, [True])
        self.assertIn("CENTER_MESSAGE 0xffffff ", controller.sink.commands)
        messages = [plain_console_text(item) for item in controller.sink.commands]
        self.assertIn(
            "CONSOLE_MESSAGE Extend vote passed. Final countdown cancelled; "
            "map extended by 5 minutes.",
            messages,
        )

    def test_server_owned_and_ai_players_never_count_as_voters(self):
        human = Player("human", "Human", owner_id=4)
        owner_zero = Player("ghost", "Ghost", owner_id=0, is_ai=False)
        classified_ai = Player("bot", "Bot", owner_id=None, is_ai=True)
        controller = countdown_controller([human, owner_zero, classified_ai])

        self.assertEqual(controller.eligible_voters(), [human])

    def test_online_snapshot_keeps_owner_zero_classified_as_ai(self):
        controller = object.__new__(TronnerRacing)
        controller.players = {}
        controller.aliases = {}
        controller.online_snapshot_misses = {}

        controller._handle_online_player("ghost 0 4 13 15 0 0 0 team")

        ghost = controller.player_for("ghost")
        self.assertIsNotNone(ghost)
        self.assertEqual(ghost.owner_id, 0)
        self.assertTrue(ghost.is_ai)

    def test_only_extend_vote_can_be_restored_during_countdown(self):
        racer = Player("racer", "Racer", owner_id=1)
        controller = countdown_controller([racer])
        racer.suspended_votes = {"extend": 0, "skip": 0}

        restored = controller._restore_player_votes(racer)

        self.assertTrue(restored)
        self.assertEqual(controller.extend_votes, {racer.identity_key})
        self.assertEqual(controller.skip_votes, set())


if __name__ == "__main__":
    unittest.main()
