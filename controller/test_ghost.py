import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from TronnerRacing import (
    GHOST_PLAN_FILENAME_RE,
    Player,
    Record,
    ReplayCapture,
    StateStore,
    TronnerRacing as Controller,
    plain_console_text,
)


class GhostTests(unittest.IsolatedAsyncioTestCase):
    class Sink:
        def __init__(self):
            self.commands = []

        async def send(self, *commands):
            self.commands.extend(commands)

    @staticmethod
    def add_recorded_finish(store: StateStore) -> tuple[Player, Record, int]:
        player = Player("racer", "Racer", auth_name="Racer")
        record, _improved, _old_time, _old_turns = store.add_finish(
            "record-map", player, 12.5, 4
        )
        store.add_replay_settings("physics-1", 1, [])
        capture = ReplayCapture(
            token="run-1",
            player_log_name=player.log_name,
            identity_key=player.identity_key,
            username=player.record_name,
            authenticated=True,
            map_identifier="Tester/maps/Race-v1.aamap.xml",
            revision_identifier="revision-1",
            resource_key="resource-revision-1",
            record_key="record-map",
            started_at=1000.0,
            spawn_game_time=10.0,
            x=1.25,
            y=-2.5,
            xdir=1.0,
            ydir=0.0,
            speed=30.0,
            initial_turns=0,
            size_factor=1.0,
            start_mode="countdown",
            checkpoint_spawn=False,
            settings_identifier="physics-1",
            release_offset_us=1_000_000,
            events=[
                (500_000, 0),
                (1_000_000, 2),
                (1_250_000, 0),
                (2_000_000, 3),
            ],
        )
        capture.outcome = "finish"
        capture.finish_seconds = 12.5
        capture.finish_turns = 4
        capture.personal_best = True
        run_id = store.add_replay(capture, 1012.5)
        return player, record, run_id

    def test_exact_record_replay_is_normalized_to_race_release(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            _player, record, run_id = self.add_recorded_finish(store)

            replay = store.ghost_replay_for_record(
                "resource-revision-1", "record-map", record
            )

            self.assertIsNotNone(replay)
            self.assertEqual(replay.run_id, run_id)
            self.assertEqual(replay.events, ((0, 2), (250_000, 0), (1_000_000, 3)))
            self.assertEqual(replay.settings_identifier, "physics-1")
            self.assertIsNone(
                store.ghost_replay_for_record(
                    "different-resource-revision", "record-map", record
                )
            )
            store.close()

    async def test_world_record_command_writes_private_one_shot_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "state.sqlite3")
            player, _record, run_id = self.add_recorded_finish(store)
            controller = object.__new__(Controller)
            controller.sink = self.Sink()
            controller.store = store
            controller.current = SimpleNamespace(
                key="resource-revision-1",
                records_key="record-map",
                time_decimals=3,
            )
            controller.current_size_factor = 1.0
            controller.active_replay_settings_identifier = "physics-1"
            controller.round_active = True
            controller.transitioning = False
            controller.config = {"ghost_plan_dir": str(root / "ghosts")}

            await controller._command_ghost(player, "wr")

            load = next(
                command
                for command in controller.sink.commands
                if command.startswith("GHOST_LOAD ")
            )
            filename = load.rsplit(" ", 1)[1]
            path = root / "ghosts" / filename
            self.assertRegex(filename, GHOST_PLAN_FILENAME_RE)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            lines = path.read_text(encoding="ascii").splitlines()
            self.assertEqual(lines[0], "TRONNER_GHOST 1")
            self.assertEqual(lines[1], f"RUN {run_id}")
            self.assertEqual(lines[2], f"NAME {'Ghost WR'.encode().hex()}")
            self.assertIn("DURATION_US 12500000", lines)
            self.assertIn("EVENT_COUNT 3", lines)
            self.assertIn("EVENT 250000 0", lines)
            confirmation = " ".join(
                plain_console_text(command)
                for command in controller.sink.commands
                if command.startswith("PLAYER_MESSAGE ")
            )
            self.assertIn("private ghost", confirmation)
            self.assertIn("next attempt", confirmation)

            controller.sink.commands.clear()
            await controller._command_ghost(player, "")
            self.assertTrue(
                any(
                    "Selected PB" in plain_console_text(command)
                    for command in controller.sink.commands
                )
            )
            store.close()

    async def test_ghost_rejects_different_physics_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root / "state.sqlite3")
            player, _record, _run_id = self.add_recorded_finish(store)
            controller = object.__new__(Controller)
            controller.sink = self.Sink()
            controller.store = store
            controller.current = SimpleNamespace(
                key="resource-revision-1",
                records_key="record-map",
                time_decimals=3,
            )
            controller.current_size_factor = 1.0
            controller.active_replay_settings_identifier = "physics-2"
            controller.round_active = True
            controller.transitioning = False
            controller.config = {"ghost_plan_dir": str(root / "ghosts")}

            await controller._command_ghost(player, "pb")
            self.assertFalse(
                any(command.startswith("GHOST_LOAD ") for command in controller.sink.commands)
            )
            self.assertTrue(
                any(
                    "different server physics" in plain_console_text(command)
                    for command in controller.sink.commands
                )
            )

            controller.sink.commands.clear()
            await controller._command_ghost(player, "off")
            self.assertIn("GHOST_CLEAR racer", controller.sink.commands)
            store.close()

    def test_rank_and_name_selectors_are_unambiguous(self):
        records = [
            Record("auth:alice", "Alice", 10.0, True),
            Record("auth:bob", "Bob", 11.0, True),
            Record("auth:bobby", "Bobby", 12.0, True),
        ]
        player = Player("alice", "Alice", auth_name="Alice")

        record, rank, label = Controller._ghost_record_for_selector(
            records, player, "rank 2"
        )
        self.assertEqual((record.username, rank, label), ("Bob", 2, "rank 2"))
        record, rank, label = Controller._ghost_record_for_selector(
            records, player, "Alice"
        )
        self.assertEqual((record.username, rank, label), ("Alice", 1, "Alice"))
        record, rank, message = Controller._ghost_record_for_selector(
            records, player, "Bo"
        )
        self.assertIsNone(record)
        self.assertIsNone(rank)
        self.assertIn("ambiguous", message)


if __name__ == "__main__":
    unittest.main()
