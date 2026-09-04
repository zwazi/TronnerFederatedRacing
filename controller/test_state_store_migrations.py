import sqlite3
import tempfile
import unittest
from pathlib import Path

from TronnerRacing import ReplayCapture, StateStore


class StateStoreMigrationTests(unittest.TestCase):
    @staticmethod
    def finished_replay(
        resource_key: str,
        map_identifier: str,
        revision_identifier: str,
    ) -> ReplayCapture:
        capture = ReplayCapture(
            token=f"token-{map_identifier}",
            player_log_name="racer",
            identity_key="auth:racer",
            username="Racer",
            authenticated=True,
            map_identifier=map_identifier,
            revision_identifier=revision_identifier,
            resource_key=resource_key,
            started_at=1000.0,
            spawn_game_time=10.0,
            x=1.0,
            y=2.0,
            xdir=1.0,
            ydir=0.0,
            speed=30.0,
            initial_turns=0,
            size_factor=None,
            start_mode="immediate",
            checkpoint_spawn=False,
            storage_path=f"_revisions/tester/{revision_identifier}/map.aamap.xml",
        )
        capture.outcome = "finish"
        capture.finish_seconds = 12.5
        capture.finish_turns = 4
        return capture

    def test_finish_history_backfill_runs_once_and_has_a_supporting_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = StateStore(path)
            store.close()

            connection = sqlite3.connect(path)
            connection.execute(
                "DELETE FROM metadata WHERE key=?",
                (StateStore.FINISH_HISTORY_BACKFILL_KEY,),
            )
            connection.execute(
                "INSERT INTO records(map_key, identity_key, username, authenticated, "
                "best_seconds, best_turns, achieved_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                ("map", "auth:racer", "racer", 1, 12.5, 8, 1000.0),
            )
            connection.commit()
            connection.close()

            migrated = StateStore(path)
            finish = migrated.connection.execute(
                "SELECT seconds, turns FROM finishes WHERE map_key=? AND identity_key=?",
                ("map", "auth:racer"),
            ).fetchone()
            marker = migrated.get_json(StateStore.FINISH_HISTORY_BACKFILL_KEY, False)
            indexes = {
                row[1]
                for row in migrated.connection.execute("PRAGMA index_list(finishes)")
            }
            migrated.close()

            self.assertEqual(finish, (12.5, 8))
            self.assertTrue(marker)
            self.assertIn("finishes_by_map_identity_time", indexes)

            reopened = StateStore(path)
            count = reopened.connection.execute(
                "SELECT COUNT(*) FROM finishes WHERE map_key=? AND identity_key=?",
                ("map", "auth:racer"),
            ).fetchone()[0]
            reopened.close()
            self.assertEqual(count, 1)

    def test_replay_map_rekey_is_idempotent_and_rewinds_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            store.connection.execute(
                "INSERT INTO replay_maps(map_identifier, revision_identifier, "
                "resource_key) VALUES(?, ?, ?)",
                ("map-a", "rev-a", "record-a"),
            )
            for map_key in ("record-a", "record-b"):
                store.connection.execute(
                    "INSERT INTO records(map_key, identity_key, username, "
                    "authenticated, best_seconds, achieved_at) "
                    "VALUES(?, 'auth:racer', 'Racer', 1, 12.5, 1000)",
                    (map_key,),
                )
            store.connection.commit()
            first_run = store.add_replay(
                self.finished_replay("resource-a", "map-a", "rev-a"),
                1012.5,
            )
            second_run = store.add_replay(
                self.finished_replay("resource-b", "map-b", "rev-b"),
                1012.5,
            )
            store.set_json("live_dashboard_replay_cursor_nyc1", second_run + 10)

            result = store.rekey_replay_maps(
                {"resource-a": "record-a", "resource-b": "record-b"},
                "nyc1",
            )

            self.assertEqual(result.map_rows, 2)
            self.assertEqual(result.replay_runs, 2)
            self.assertEqual(result.finished_runs, 2)
            self.assertEqual(result.records_marked, 2)
            self.assertEqual(result.earliest_finished_run_id, first_run)
            self.assertEqual(result.replay_cursor, first_run - 1)
            self.assertEqual(
                store.get_json("live_dashboard_replay_cursor_nyc1", None),
                first_run - 1,
            )
            replay_rows = store.connection.execute(
                "SELECT replay_runs.id, replay_maps.resource_key, "
                "replay_maps.record_key FROM replay_runs "
                "JOIN replay_maps ON replay_maps.id=replay_runs.map_ref "
                "ORDER BY replay_runs.id"
            ).fetchall()
            self.assertEqual(
                replay_rows,
                [
                    (first_run, "resource-a", "record-a"),
                    (second_run, "resource-b", "record-b"),
                ],
            )
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM replay_maps WHERE resource_key LIKE "
                    "'resource-%'"
                ).fetchone()[0],
                2,
            )
            history = store.dashboard_player_map_history(
                "auth:racer", "record-a"
            )
            self.assertEqual(history[0]["mapResourcePath"], "resource-a")
            self.assertEqual(
                history[0]["mapStoragePath"],
                "_revisions/tester/rev-a/map.aamap.xml",
            )
            self.assertEqual(
                store.dashboard_replay_payload(first_run)["mapKey"],
                "resource-a",
            )
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM records WHERE replay_available=1"
                ).fetchone()[0],
                2,
            )

            repeated = store.rekey_replay_maps(
                {"resource-a": "record-a", "resource-b": "record-b"},
                "nyc1",
            )
            self.assertEqual(repeated.map_rows, 0)
            self.assertEqual(repeated.replay_cursor, first_run - 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
