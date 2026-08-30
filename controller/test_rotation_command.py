import unittest
from pathlib import Path

from TronnerRacing import (
    COLOR_CURRENT_MAP,
    MapEntry,
    Player,
    TronnerRacing,
    build_rotation_columns,
    plain_console_text,
)


def entry(number, name):
    key = f"Author/maps/{name}-{number}.aamap.xml"
    return MapEntry(key, name, "Author", f"v{number}", "maps", key, Path(key), ())


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class Repository:
    def __init__(self, entries):
        self.catalog = {item.key: item for item in entries}

    def display_name(self, item):
        return item.name


class RotationCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_rotation_columns_have_two_blocks_of_three_fields(self):
        lines = build_rotation_columns(
            [
                (name, "Author", f"v{index}", name == "Juliet")
                for index, name in enumerate(
                    (
                        "Alpha",
                        "Bravo",
                        "Charlie",
                        "Delta",
                        "Juliet",
                        "Kilo",
                        "Lima",
                        "Mike",
                        "Zulu",
                    ),
                    1,
                )
            ]
        )

        self.assertEqual(len(lines), 6)
        header_blocks = lines[0].split("   |   ")
        self.assertEqual(len(header_blocks), 2)
        for block in header_blocks:
            self.assertIn("Map name", block)
            self.assertIn("Author", block)
            self.assertIn("Version", block)

        visible_lines = [plain_console_text(line) for line in lines[1:]]
        for expected, line in zip(
            (
                ("Alpha", "Kilo"),
                ("Bravo", "Lima"),
                ("Charlie", "Mike"),
                ("Delta", "Zulu"),
                ("Juliet",),
            ),
            visible_lines,
        ):
            for name in expected:
                self.assertIn(name, line)
        self.assertIn(COLOR_CURRENT_MAP, lines[5])

    async def test_rotation_is_alphabetical_with_current_highlight(self):
        entries = [
            entry(index, name)
            for index, name in enumerate(
                ("Zulu", "Alpha", "Juliet", "Bravo", "Kilo", "Charlie", "Lima", "Delta", "Mike"),
                1,
            )
        ]
        controller = object.__new__(TronnerRacing)
        controller.repository = Repository(entries)
        controller.current = entries[2]
        controller.sink = Sink()
        player = Player("racer", "Racer")

        await controller._command_rotation(player)

        commands = controller.sink.commands
        self.assertEqual(len(commands), 1)
        output = commands[0]
        visible_output = plain_console_text(output)
        self.assertIn("Map rotation (9)", visible_output)
        self.assertEqual(visible_output.count("Map name"), 2)
        self.assertEqual(visible_output.count("Author"), 11)
        self.assertEqual(visible_output.count("Version"), 2)
        self.assertLess(visible_output.index("Alpha"), visible_output.index("Bravo"))
        self.assertLess(visible_output.index("Bravo"), visible_output.index("Charlie"))
        self.assertIn(COLOR_CURRENT_MAP, output)
        self.assertIn(f"{COLOR_CURRENT_MAP}Juliet", output)


if __name__ == "__main__":
    unittest.main()
