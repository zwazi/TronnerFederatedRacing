import sqlite3
import tempfile
import unittest
from pathlib import Path

from TronnerRacing import StateStore


class StateStoreMigrationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
