import tempfile
import unittest
from pathlib import Path

from TronnerRacing import (
    HotCommandRegistry,
    Player,
    StateStore,
    StoredIdentity,
    TronnerRacing as Controller,
    plain_console_text,
)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class UserMergeStoreTests(unittest.TestCase):
    def test_merge_moves_history_and_keeps_best_overlapping_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            old = Player("old", "Old", auth_name="Old@forums")
            new = Player("new", "New", auth_name="New@forums")

            store.add_finish("map-a", old, 10.0, 50)
            store.add_finish("map-a", old, 11.0, 40)
            store.add_finish("map-b", old, 8.0, 10)
            store.add_finish("map-c", old, 7.0, None)
            store.add_finish("map-a", new, 9.0, 70)
            store.add_finish("map-b", new, 8.0, 12)

            result = store.merge_users(
                old.identity_key,
                StoredIdentity(new.identity_key, new.record_name, True),
            )

            self.assertEqual(result.records_moved, 3)
            self.assertEqual(result.finishes_moved, 4)
            self.assertEqual(result.overlapping_records, 2)
            self.assertEqual(store.matching_user_identities(old.identity_key), [])

            destination_records = {
                map_key: (seconds, turns, username, authenticated)
                for map_key, seconds, turns, username, authenticated in
                store.connection.execute(
                    "SELECT map_key, best_seconds, best_turns, username, authenticated "
                    "FROM records WHERE identity_key=?",
                    (new.identity_key,),
                )
            }
            self.assertEqual(destination_records["map-a"][:2], (9.0, 70))
            self.assertEqual(destination_records["map-b"][:2], (8.0, 10))
            self.assertEqual(destination_records["map-c"][:2], (7.0, None))
            self.assertTrue(
                all(
                    values[2:] == ("New@forums", 1)
                    for values in destination_records.values()
                )
            )
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM finishes WHERE identity_key=?",
                    (new.identity_key,),
                ).fetchone()[0],
                6,
            )
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM finishes WHERE identity_key=?",
                    (old.identity_key,),
                ).fetchone()[0],
                0,
            )
            store.close()

    def test_plain_name_ambiguity_requires_an_explicit_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            guest = Player("guest", "Same")
            authenticated = Player("auth", "Same", auth_name="Same")
            store.add_finish("map", guest, 10.0, 1)
            store.add_finish("map", authenticated, 9.0, 1)

            self.assertEqual(
                [match.identity_key for match in store.matching_user_identities("same")],
                ["auth:same", "guest:same"],
            )
            self.assertEqual(
                store.matching_user_identities("auth:same")[0].identity_key,
                "auth:same",
            )
            store.close()


class UserMergeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_is_admin_only_and_accepts_quoted_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(Controller)
            controller.sink = Sink()
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.config = {"records_admin_access_level": 1}
            controller.hot_commands = HotCommandRegistry(
                Path(__file__).resolve().with_name("hot_commands")
            )
            controller.players = {}
            controller.aliases = {}
            admin = Player("admin", "Admin")
            old = Player("old", "Old Racer")
            new = Player("new", "New Racer", auth_name="New@forums")
            controller.players = {"admin": admin, "new": new}
            controller.aliases = {"admin": admin, "new racer": new}
            controller.store.add_finish("map", old, 12.0, 5)

            await controller.hot_commands.dispatch(
                controller,
                "/merge_users",
                admin,
                20,
                '"Old Racer" "New Racer"',
            )
            self.assertEqual(
                len(controller.store.matching_user_identities(old.identity_key)), 1
            )
            self.assertTrue(
                any(
                    "Only an Owner or Admin may merge users." in plain_console_text(line)
                    for line in controller.sink.commands
                )
            )

            await controller.hot_commands.dispatch(
                controller,
                "/merge_users",
                admin,
                1,
                '"Old Racer" "New Racer"',
            )
            self.assertEqual(
                controller.store.matching_user_identities(old.identity_key), []
            )
            self.assertEqual(
                controller.store.records("map")[0].identity_key,
                new.identity_key,
            )
            self.assertTrue(
                any(
                    "merged Old Racer into New@forums" in plain_console_text(line)
                    for line in controller.sink.commands
                )
            )
            controller.store.close()


if __name__ == "__main__":
    unittest.main()
