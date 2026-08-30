import tempfile
import unittest
from pathlib import Path

from TronnerRacing import HotCommandRegistry, Player


def module_source(version):
    return f'''\
async def handle(controller, player, access_level, arguments):
    controller.calls.append(({version!r}, access_level, arguments))

COMMANDS = {{
    "/example": {{
        "handler": handle,
        "access_setting": "records_admin_access_level",
        "access_denied": "Denied.",
        "help_command": "/example [value]",
        "help_description": "Example admin command.",
    }}
}}
'''


class ControllerStub:
    def __init__(self):
        self.config = {"records_admin_access_level": 1}
        self.calls = []
        self.private_messages = []

    async def private(self, _player, message):
        self.private_messages.append(message)


class HotCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_module_is_used_on_the_next_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            module = directory / "example.py"
            module.write_text(module_source("first"), encoding="utf-8")
            registry = HotCommandRegistry(directory)
            controller = ControllerStub()
            player = Player("admin", "Admin")

            self.assertTrue(
                await registry.dispatch(
                    controller, "/example", player, 1, "before"
                )
            )
            module.write_text(module_source("second"), encoding="utf-8")
            self.assertTrue(
                await registry.dispatch(
                    controller, "/example", player, 1, "after"
                )
            )

            self.assertEqual(
                controller.calls,
                [("first", 1, "before"), ("second", 1, "after")],
            )
            self.assertEqual(
                registry.help_entries(controller.config, 1),
                [("/example [value]", "Example admin command.")],
            )

    async def test_invalid_update_retains_last_known_good_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            module = directory / "example.py"
            module.write_text(module_source("working"), encoding="utf-8")
            registry = HotCommandRegistry(directory)
            controller = ControllerStub()
            player = Player("admin", "Admin")

            await registry.dispatch(controller, "/example", player, 1, "one")
            module.write_text("this is not valid Python !!!\n", encoding="utf-8")
            await registry.dispatch(controller, "/example", player, 1, "two")

            self.assertEqual(
                controller.calls,
                [("working", 1, "one"), ("working", 1, "two")],
            )
            self.assertIn("SyntaxError", registry.last_error)

    async def test_permission_is_enforced_before_module_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "example.py").write_text(
                module_source("working"), encoding="utf-8"
            )
            registry = HotCommandRegistry(directory)
            controller = ControllerStub()
            player = Player("guest", "Guest")

            self.assertTrue(
                await registry.dispatch(
                    controller, "/example", player, 20, "blocked"
                )
            )
            self.assertEqual(controller.calls, [])
            self.assertEqual(controller.private_messages, ["Denied."])


if __name__ == "__main__":
    unittest.main()
