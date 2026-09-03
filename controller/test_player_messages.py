import tempfile
import unittest
from pathlib import Path

from TronnerRacing import (
    FINAL_COUNTDOWN_CENTER_PADDING,
    PLAYER_MESSAGE_LIMIT,
    Player,
    StateStore,
    StoredIdentity,
    TronnerRacing,
    final_countdown_center_command,
    plain_console_text,
)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class FinalCountdownDisplayTests(unittest.TestCase):
    def test_countdown_is_offset_and_fades_green_yellow_red(self):
        padding = " " * FINAL_COUNTDOWN_CENTER_PADDING

        self.assertEqual(
            final_countdown_center_command(59, 59),
            f"CENTER_MESSAGE 0x00ff0059{padding}0xffffff ",
        )
        self.assertEqual(
            final_countdown_center_command(30, 59),
            f"CENTER_MESSAGE 0xffff0030{padding}0xffffff ",
        )
        self.assertEqual(
            final_countdown_center_command(1, 59),
            f"CENTER_MESSAGE 0xff00001{padding}0xffffff ",
        )
        self.assertEqual(
            final_countdown_center_command(0, 59),
            f"CENTER_MESSAGE 0xff00000{padding}0xffffff ",
        )

    def test_short_countdown_still_has_green_start_and_red_zero(self):
        self.assertIn("0x00ff001", final_countdown_center_command(1, 1))
        self.assertIn("0xff00000", final_countdown_center_command(0, 1))


class SavedPlayerMessageStoreTests(unittest.TestCase):
    def test_message_survives_store_reopen_until_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            recipient = StoredIdentity("auth:racer@forums", "Racer@forums", True)
            sender = StoredIdentity("auth:admin@forums", "Admin@forums", True)
            store = StateStore(path)
            saved = store.save_player_message(
                recipient,
                sender,
                "Remember the tournament at 8.",
                created_at=1_700_000_000,
            )
            store.close()

            store = StateStore(path)
            self.assertEqual(
                store.pending_player_messages(recipient.identity_key), [saved]
            )
            self.assertTrue(
                store.delete_player_message(saved.id, recipient.identity_key)
            )
            self.assertEqual(store.pending_player_messages(recipient.identity_key), [])
            store.close()

    def test_rejects_guest_recipients_and_oversized_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            sender = StoredIdentity("auth:admin@forums", "Admin@forums", True)
            with self.assertRaisesRegex(ValueError, "authenticated recipient"):
                store.save_player_message(
                    StoredIdentity("guest:racer", "Racer", False),
                    sender,
                    "Hello",
                )
            with self.assertRaisesRegex(ValueError, "at most"):
                store.save_player_message(
                    StoredIdentity("auth:racer@forums", "Racer@forums", True),
                    sender,
                    "x" * (PLAYER_MESSAGE_LIMIT + 1),
                )
            store.close()


class SavedPlayerMessageCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_queues_message_and_login_delivers_it_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(TronnerRacing)
            controller.config = {"records_admin_access_level": 1}
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.sink = Sink()
            controller.current = None
            controller.start_preferences = {}
            controller.spawn_preferences = {}
            controller.players = {}
            controller.aliases = {}
            controller._publish_player_audit = lambda *args, **kwargs: None
            admin = Player("admin", "Admin", auth_name="Admin@forums")
            racer = Player("racer", "Racer", auth_name="Racer@forums")
            controller.players = {"admin": admin, "racer": racer}
            controller.aliases = {"admin": admin, "racer": racer}

            await controller._command_message(
                admin,
                1,
                "racer Please review the new racing rule.",
            )

            self.assertEqual(
                len(controller.store.pending_player_messages(racer.identity_key)),
                1,
            )
            self.assertFalse(
                any(
                    command.startswith("PLAYER_MESSAGE racer ")
                    for command in controller.sink.commands
                )
            )
            controller._handle_player_logout("racer")
            controller.sink.commands.clear()

            await controller.handle_line("PLAYER_LOGIN racer Racer@forums")

            output = "\n".join(
                plain_console_text(command) for command in controller.sink.commands
            )
            self.assertIn("PLAYER_MESSAGE racer", output)
            self.assertIn("Saved message from Admin@forums", output)
            self.assertIn("Please review the new racing rule.", output)
            self.assertEqual(
                controller.store.pending_player_messages(racer.identity_key),
                [],
            )

            controller.sink.commands.clear()
            controller._handle_player_logout("racer")
            await controller.handle_line("PLAYER_LOGIN racer Racer@forums")
            self.assertEqual(controller.sink.commands, [])
            controller.store.close()

    async def test_message_command_is_admin_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(TronnerRacing)
            controller.config = {"records_admin_access_level": 1}
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.sink = Sink()
            controller.players = {}
            controller.aliases = {}
            controller._publish_player_audit = lambda *args, **kwargs: None
            sender = Player("sender", "Sender", auth_name="Sender@forums")
            recipient = Player("racer", "Racer", auth_name="Racer@forums")
            controller.players = {"sender": sender, "racer": recipient}

            await controller._command_message(sender, 20, "racer Secret")

            self.assertEqual(
                controller.store.pending_player_messages(recipient.identity_key),
                [],
            )
            self.assertIn(
                "Administrator access is required for /message.",
                "\n".join(
                    plain_console_text(command)
                    for command in controller.sink.commands
                ),
            )
            controller.store.close()


if __name__ == "__main__":
    unittest.main()
