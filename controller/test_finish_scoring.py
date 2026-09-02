import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from TronnerRacing import (
    Player,
    StateStore,
    TronnerRacing as Controller,
    plain_console_text,
)


class FinishScoringTests(unittest.IsolatedAsyncioTestCase):
    class Sink:
        def __init__(self):
            self.commands = []

        async def send(self, *commands):
            self.commands.extend(commands)

    async def test_each_valid_attempt_awards_exactly_one_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(Controller)
            controller.sink = self.Sink()
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.current = SimpleNamespace(key="map")
            player = Player("racer", "Racer", alive=True, attempt_started_game=1.0)
            controller.players = {"racer": player}
            controller.aliases = {"racer": player}
            controller.config = {"maximum_record_seconds": 7200}
            controller.finalists = set()

            await controller._handle_winzone(
                "1 finish 0 0 racer 0 0 1 0 11.125"
            )
            # The same zone reports every tick until the kill command lands;
            # those duplicate events must not score or enqueue another kill.
            await controller._handle_winzone(
                "1 finish 0 0 racer 0 0 1 0 11.135"
            )
            self.assertEqual(
                controller.sink.commands.count("ADD_SCORE_PLAYER racer 1"), 1
            )
            self.assertEqual(
                controller.sink.commands.count("KILL_SILENT racer"), 1
            )
            self.assertTrue(
                any(command.startswith("PLAYER_MESSAGE racer ") for command in controller.sink.commands)
            )
            self.assertFalse(
                any(command.startswith("CONSOLE_MESSAGE ") and "Finish:" in command
                    for command in controller.sink.commands)
            )

            player.alive = True
            player.attempt_started_game = 20.0
            await controller._handle_winzone(
                "1 finish 0 0 racer 0 0 1 0 31.500"
            )

            awards = [
                command
                for command in controller.sink.commands
                if command == "ADD_SCORE_PLAYER racer 1"
            ]
            self.assertEqual(len(awards), 2)
            finishes = controller.store.connection.execute(
                "SELECT COUNT(*) FROM finishes"
            ).fetchone()[0]
            self.assertEqual(finishes, 2)
            controller.store.close()

    async def test_results_command_hides_and_restores_finish_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(Controller)
            controller.sink = self.Sink()
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            racer = Player("racer", "Racer")
            viewer = Player("viewer", "Viewer")
            controller.players = {"racer": racer, "viewer": viewer}
            controller.result_message_preferences = {}

            await controller._command_results(viewer)
            self.assertFalse(
                controller.result_message_preferences[viewer.identity_key]
            )
            controller.sink.commands.clear()
            await controller.result_message("Finish: 12.345, Rank: 1")
            self.assertTrue(any(command.startswith("PLAYER_MESSAGE racer ")
                                for command in controller.sink.commands))
            self.assertFalse(any(command.startswith("PLAYER_MESSAGE viewer ")
                                 for command in controller.sink.commands))

            await controller._command_results(viewer)
            self.assertTrue(
                controller.result_message_preferences[viewer.identity_key]
            )
            controller.store.close()

    async def test_no_cp_time_is_displayed_but_never_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(Controller)
            controller.sink = self.Sink()
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.current = SimpleNamespace(key="map", checkpoint_ids=())
            controller.config = {"maximum_record_seconds": 7200}
            controller.finalists = set()
            controller.finishes_in_progress = set()

            other = Player("other", "Other")
            controller.store.add_finish("map", other, 12.0, 5)
            player = Player(
                "racer",
                "Racer",
                alive=True,
                attempt_started_game=10.0,
                checkpoint_respawn_used=True,
                no_cp_elapsed=5.0,
                no_cp_segment_started_game=30.0,
            )
            controller.players = {"racer": player}
            controller.aliases = {"racer": player}
            published = []
            controller._publish_dashboard_finish_activity = (
                lambda _player, **payload: published.append(payload)
            )

            await controller._handle_winzone(
                "1 finish 0 0 racer 0 0 1 0 turns=9 40"
            )

            message = next(
                plain_console_text(command)
                for command in controller.sink.commands
                if "No-CP:" in plain_console_text(command)
            )
            self.assertIn("Finish: 30.000, Turns: 9, Rank: 2", message)
            self.assertIn("No-CP: 15.000, Rank: 2, Turns: 9", message)
            self.assertEqual(published[0]["rank"], 2)
            self.assertIsNone(published[0]["pb_rank"])
            self.assertEqual(published[0]["no_cp_rank"], 2)
            stored = controller.store.connection.execute(
                "SELECT best_seconds FROM records WHERE identity_key = ?",
                (player.identity_key,),
            ).fetchone()
            self.assertEqual(stored, (30.0,))
            finish_times = controller.store.connection.execute(
                "SELECT seconds FROM finishes WHERE identity_key = ?",
                (player.identity_key,),
            ).fetchall()
            self.assertEqual(finish_times, [(30.0,)])
            controller.store.close()


if __name__ == "__main__":
    unittest.main()
