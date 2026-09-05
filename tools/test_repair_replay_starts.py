import sqlite3
import tempfile
import unittest
from pathlib import Path

import repair_replay_starts as repair


class ReplayStartRepairTests(unittest.TestCase):
    @staticmethod
    def create_database(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE replay_maps(id INTEGER PRIMARY KEY, resource_key TEXT);
            CREATE TABLE replay_runs(
                id INTEGER PRIMARY KEY,
                map_ref INTEGER,
                spawn_game_time REAL,
                release_offset_us INTEGER,
                start_x REAL,
                start_y REAL,
                start_xdir REAL,
                start_ydir REAL,
                start_speed REAL,
                initial_turns INTEGER,
                input_data BLOB,
                recorded_at REAL,
                ended_at REAL,
                outcome INTEGER
            );
            INSERT INTO replay_maps VALUES(1, 'Author/maps/Race-v4.aamap.xml');
            """
        )
        return connection

    def test_release_is_authoritative_and_terminal_state_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            ladderlog = Path(directory) / "ladderlog.txt"
            ladderlog.write_text(
                "\n".join(
                    (
                        "CURRENT_MAP 0 1 Author/maps/Race-v3.aamap.xml",
                        "CYCLE_REPLAY_BEGIN 7 racer 10 -8 -136 -1 0 20 1 s1-test 0",
                        "CYCLE_REPLAY_INPUT 7 10 B1",
                        "CYCLE_REPLAY_STATE 7 brake_hold 10 -8 -136 -1 0 0 1 0",
                        "CYCLE_REPLAY_INPUT 7 10.5 B0",
                        "CYCLE_REPLAY_STATE 7 release 10.5 -8 -136 -1 0 20 1 0",
                        "CYCLE_REPLAY_INPUT 7 11 R",
                        "CYCLE_REPLAY_STATE 7 death 20 43 -106 0 -1 66 85 2500",
                        "CYCLE_REPLAY_END 7 racer 20 ADMIN_KILL",
                    )
                ),
                encoding="utf-8",
            )
            captures = repair.parse_ladderlog(ladderlog)
            self.assertEqual(len(captures), 1)
            self.assertEqual(captures[0].start, (-8, -136, -1, 0, 20, 1))
            self.assertEqual(captures[0].release_offset_us, 500_000)

            database = Path(directory) / "state.sqlite3"
            connection = self.create_database(database)
            events = [(0, 3), (500_000, 2), (1_000_000, 1)]
            connection.execute(
                "INSERT INTO replay_runs VALUES(1,1,10,500000,43,-106,0,-1,66,85,"
                "?,1000,1010,1)",
                (sqlite3.Binary(repair.encode_replay_inputs(events)),),
            )
            plan = repair.build_repair_plan(connection, captures)
            self.assertEqual(len(plan.repairs), 1)
            self.assertEqual(plan.finished_repairs, 1)
            repair.apply_repairs(connection, plan)
            self.assertEqual(
                connection.execute(
                    "SELECT start_x,start_y,start_xdir,start_ydir,start_speed,"
                    "initial_turns FROM replay_runs WHERE id=1"
                ).fetchone(),
                (-8.0, -136.0, -1.0, 0.0, 20.0, 1),
            )
            connection.close()

    def test_ambiguous_starts_are_not_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            connection = self.create_database(database)
            connection.execute(
                "INSERT INTO replay_runs VALUES(1,1,0,NULL,99,99,1,0,20,1,"
                "X'',100,101,0)"
            )
            captures = [
                repair.LogCapture(
                    "1", "Author/maps/Race-v3.aamap.xml", 0,
                    (10, 20, 1, 0, 20, 1), duration_us=1_000_000,
                ),
                repair.LogCapture(
                    "2", "Author/maps/Race-v3.aamap.xml", 0,
                    (30, 40, -1, 0, 20, 1), duration_us=1_000_000,
                ),
            ]
            plan = repair.build_repair_plan(connection, captures)
            self.assertEqual(plan.ambiguous_runs, 1)
            self.assertFalse(plan.repairs)
            connection.close()

    def test_backup_is_integrity_checked_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            connection = self.create_database(database)
            backup = Path(directory) / "backup.sqlite3"
            repair.backup_database(connection, backup)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                repair.backup_database(connection, backup)
            connection.close()


if __name__ == "__main__":
    unittest.main()
