import re
import unittest

from TronnerRacing import (
    COLOR_BORDER,
    COLOR_COMMAND,
    COLOR_DATA,
    COLOR_NAME_HEADER,
    COLOR_RANK_HEADER,
    COLOR_RESET,
    COLOR_TITLE,
    COLOR_TIME_HEADER,
    COLOR_TURNS_HEADER,
    Player,
    TronnerRacing,
    build_help_lines,
    build_leaderboard_table,
    format_final_countdown_rating_message,
    plain_console_text,
    style_console_message,
)


VALID_COLOR_RE = re.compile(r"0x[0-9a-f]{6}")


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class MessagePaletteTests(unittest.IsolatedAsyncioTestCase):
    def test_help_uses_two_left_aligned_columns(self):
        entries = (
            ("/q add [map]", "Queue a map."),
            ("/leaderboard", "Show the current leaderboard."),
            ("/help", "Show commands."),
        )
        lines = build_help_lines(entries)

        command_starts = {
            line.index(command) for line, (command, _) in zip(lines, entries)
        }
        description_starts = {
            line.index(description)
            for line, (_, description) in zip(lines, entries)
        }
        self.assertEqual(command_starts, {0})
        self.assertEqual(len(description_starts), 1)
        self.assertTrue(all(" - " in line for line in lines))

    def test_plain_and_labeled_messages_receive_bright_colors(self):
        plain = style_console_message("Respawning enabled.")
        labeled = style_console_message("Time left: 4 minutes.")

        self.assertTrue(plain.startswith("0x"))
        self.assertTrue(plain.endswith(COLOR_RESET))
        self.assertEqual(plain_console_text(plain), "Respawning enabled.")
        self.assertEqual(plain_console_text(labeled), "Time left: 4 minutes.")
        self.assertGreaterEqual(len(set(VALID_COLOR_RE.findall(labeled))), 3)

    def test_table_uses_distinct_border_header_and_data_colors(self):
        lines, _ = build_leaderboard_table(
            "Map", "Author", [], axes=8, rating=4.25
        )
        header = lines[3]
        data = lines[5]
        status = lines[-2]

        self.assertIn(COLOR_BORDER, lines[0])
        self.assertIn(COLOR_RANK_HEADER, header)
        self.assertIn(COLOR_TIME_HEADER, header)
        self.assertIn(COLOR_TURNS_HEADER, header)
        self.assertIn(COLOR_NAME_HEADER, header)
        self.assertIn(COLOR_DATA, data)
        self.assertNotIn(COLOR_DATA, header)
        self.assertIn("Axes: 8", plain_console_text(status))
        self.assertIn("Rating: 4.25/5", plain_console_text(status))
        status_cells = plain_console_text(status).split("|")[1:3]
        for cell in status_cells:
            left = len(cell) - len(cell.lstrip())
            right = len(cell) - len(cell.rstrip())
            self.assertLessEqual(abs(left - right), 1)
        self.assertEqual(
            len({
                COLOR_BORDER,
                COLOR_RANK_HEADER,
                COLOR_TIME_HEADER,
                COLOR_TURNS_HEADER,
                COLOR_NAME_HEADER,
                COLOR_DATA,
            }),
            6,
        )
        visible_widths = {len(plain_console_text(line)) for line in lines}
        self.assertEqual(len(visible_widths), 1)

    def test_countdown_rating_uses_full_command_and_rating_focus_colors(self):
        styled = style_console_message(
            format_final_countdown_rating_message((4.25, 2))
        )

        self.assertEqual(
            plain_console_text(styled),
            "Current rating: 4.2/5 (2 ratings). "
            "Use /rate # for the current map or /rate [map] # "
            "for a specific map.",
        )
        self.assertIn(
            f"{COLOR_TITLE}Current rating: 4.2/5 (2 ratings).{COLOR_RESET}",
            styled,
        )
        self.assertIn(f"{COLOR_COMMAND}/rate #{COLOR_RESET}", styled)
        self.assertIn(
            f"{COLOR_COMMAND}/rate [map] #{COLOR_RESET}", styled
        )
        self.assertIn(f"{COLOR_RESET} Use ", styled)
        self.assertIn(f"{COLOR_RESET} for the current map or ", styled)
        self.assertIn(f"{COLOR_RESET} for a specific map.", styled)
        self.assertNotIn(f"{COLOR_COMMAND}/rate{COLOR_RESET} #", styled)

    async def test_all_message_routes_use_only_real_color_codes(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        player = Player("racer", "Racer")

        await controller.broadcast("A public racing message.")
        await controller.private(player, "A private racing message.")
        await controller.center_private(player, "Press brake to start")

        self.assertEqual(len(controller.sink.commands), 3)
        for command in controller.sink.commands:
            starts = [match.start() for match in re.finditer(r"0[xX]", command)]
            valid = [match.start() for match in VALID_COLOR_RE.finditer(command)]
            self.assertEqual(starts, valid)


if __name__ == "__main__":
    unittest.main()
