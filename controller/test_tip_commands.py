import tempfile
import unittest
from pathlib import Path

from TronnerRacing import (
    COLOR_COMMAND,
    COLOR_RESET,
    HotCommandRegistry,
    Player,
    StateStore,
    TronnerRacing as Controller,
    load_custom_helpful_messages,
    plain_console_text,
    style_tip_message,
)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class TipStyleTests(unittest.TestCase):
    def test_tip_is_white_and_double_quoted_contents_are_highlighted(self):
        styled = style_tip_message('Type "/q add Map Name" and then say "ready".')

        self.assertTrue(styled.startswith(COLOR_RESET))
        self.assertIn(f'"{COLOR_COMMAND}/q add Map Name{COLOR_RESET}"', styled)
        self.assertIn(f'"{COLOR_COMMAND}ready{COLOR_RESET}"', styled)
        self.assertEqual(
            plain_console_text(styled),
            'Type "/q add Map Name" and then say "ready".',
        )

    def test_custom_tip_loader_validates_and_sorts_stable_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.set_json(
                "custom_helpful_messages",
                {
                    "tips": [
                        {"id": 9, "message": "Ninth."},
                        {"id": "bad", "message": "Ignored."},
                        {"id": 2, "message": "Second."},
                    ]
                },
            )

            self.assertEqual(
                load_custom_helpful_messages(store),
                ["Second.", "Ninth."],
            )
            store.close()


class TipCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_can_add_list_and_remove_stably_numbered_tips(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(Controller)
            controller.config = {"records_admin_access_level": 1}
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.sink = Sink()
            controller.hot_commands = HotCommandRegistry(
                Path(__file__).resolve().with_name("hot_commands")
            )
            admin = Player("admin", "Admin")

            await controller.hot_commands.dispatch(
                controller,
                "/tip",
                admin,
                20,
                'add Try "/q add Map Name" next.',
            )
            self.assertEqual(load_custom_helpful_messages(controller.store), [])

            await controller.hot_commands.dispatch(
                controller,
                "/tip",
                admin,
                1,
                'add Try "/q add Map Name" next.',
            )
            await controller.hot_commands.dispatch(
                controller,
                "/tip",
                admin,
                1,
                "add Remember to rate this map.",
            )
            await controller.hot_commands.dispatch(
                controller, "/tip", admin, 1, "list"
            )
            await controller.hot_commands.dispatch(
                controller, "/tip", admin, 1, "remove 1"
            )

            state = controller.store.get_json("custom_helpful_messages", {})
            self.assertEqual(state["next_id"], 3)
            self.assertEqual(
                state["tips"],
                [{"id": 2, "message": "Remember to rate this map."}],
            )
            output = "\n".join(
                plain_console_text(command) for command in controller.sink.commands
            )
            self.assertIn("Only an Owner or Admin may manage tips.", output)
            self.assertIn('Tip #1 added: Try \\"/q add Map Name\\" next.', output)
            self.assertIn("#2 - Remember to rate this map.", output)
            self.assertIn('Tip #1 removed: Try \\"/q add Map Name\\" next.', output)
            controller.store.close()


if __name__ == "__main__":
    unittest.main()
