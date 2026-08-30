import re
import unittest

from TronnerRacing import (
    Player,
    brighten_console_colors,
    format_finish_message,
    normalize_console_colors,
    plain_console_text,
)


COLOR_START_RE = re.compile(r"0[xX]")
VALID_COLOR_RE = re.compile(r"0x[0-9a-f]{6}")


class ColorCodeTests(unittest.TestCase):
    def assert_all_colors_are_canonical(self, text: str) -> None:
        starts = list(COLOR_START_RE.finditer(text))
        valid = list(VALID_COLOR_RE.finditer(text))
        self.assertEqual(
            [match.start() for match in starts],
            [match.start() for match in valid],
        )

    def test_dynamic_player_colors_are_canonicalized(self) -> None:
        normalized = normalize_console_colors(
            "0XAA00FfRacer0xRESETT and 0x00FF00Guest"
        )
        self.assertEqual(
            normalized,
            "0xaa00ffRacer0xffffff and 0x00ff00Guest",
        )
        self.assert_all_colors_are_canonical(normalized)

    def test_plain_text_removes_normalized_colors(self) -> None:
        self.assertEqual(
            plain_console_text("0XAA00FfRacer0xRESETT"),
            "Racer",
        )

    def test_dark_player_colors_are_lifted_without_losing_canonical_form(self):
        lifted = brighten_console_colors("0x000011Racer 0xff0000Guest")
        self.assert_all_colors_are_canonical(lifted)
        for token in VALID_COLOR_RE.findall(lifted):
            channels = [int(token[index:index + 2], 16) for index in (2, 4, 6)]
            self.assertGreaterEqual(sum(channels), 500)

        player = Player("racer", "Racer", colored_name="0X000011Racer")
        self.assertEqual(plain_console_text(player.colored_display_name), "Racer")
        self.assertNotIn("0x000011", player.colored_display_name)

    def test_finish_message_contains_only_canonical_colors(self) -> None:
        message = format_finish_message(
            "0XAA00FfRacer",
            9.5,
            1,
            9.5,
            1,
            10.0,
            20,
            20,
            22,
        )
        self.assert_all_colors_are_canonical(message)
        self.assertIn("Racer0xffffff", message)
        self.assertIn("0x7dff9b-0.500", message)
        for token in VALID_COLOR_RE.findall(message):
            channels = [int(token[index:index + 2], 16) for index in (2, 4, 6)]
            self.assertGreaterEqual(sum(channels), 500)


if __name__ == "__main__":
    unittest.main()
