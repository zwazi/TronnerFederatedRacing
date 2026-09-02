import asyncio
import tempfile
import unittest
from pathlib import Path

from TronnerRacing import (
    CommandSink,
    Player,
    TronnerRacing,
    canonical_game_text_encoding,
    decode_game_text,
    detect_game_text_encoding,
    encode_game_text,
)


class GameEncodingTests(unittest.IsolatedAsyncioTestCase):
    def test_latest_advertised_encoding_and_aliases_are_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            ladderlog = Path(tmp) / "ladderlog.txt"
            ladderlog.write_bytes(
                b"ENCODING utf8\n"
                b"PLAYER_COLORED_NAME old 0xffffffOld\n"
                b"ENCODING latin1\n"
                b"PLAYER_COLORED_NAME nelg 0xaaaaffn\xe9lg\n"
            )

            self.assertEqual(
                detect_game_text_encoding(ladderlog),
                canonical_game_text_encoding("ISO-8859-1"),
            )
            self.assertEqual(
                canonical_game_text_encoding("UTF8"),
                "utf-8",
            )

    def test_encoding_detection_does_not_scan_a_large_ladderlog_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            ladderlog = Path(tmp) / "ladderlog.txt"
            ladderlog.write_bytes(
                b"ENCODING utf8\n"
                + b"PLAYER_GRIDPOS racer 1 2 3 4\n" * 80_000
                + b"ENCODING latin1\n"
            )

            self.assertEqual(
                detect_game_text_encoding(ladderlog),
                canonical_game_text_encoding("ISO-8859-1"),
            )

    def test_latin1_player_name_round_trips_without_replacement(self):
        raw = b"0xaaaaffn\xe9lg (H\xd6NK)"
        encoding = canonical_game_text_encoding("latin1")
        decoded = decode_game_text(raw, encoding, "test event")

        self.assertEqual(decoded, "0xaaaaffnélg (HÖNK)")
        self.assertEqual(
            encode_game_text(decoded, encoding, "test command"),
            raw,
        )
        self.assertNotIn("�", decoded)

    async def test_command_sink_uses_the_game_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "console.in"
            path.touch()
            sink = CommandSink(path, "latin1")

            await sink.send("CONSOLE_MESSAGE nélg")

            self.assertEqual(path.read_bytes(), b"CONSOLE_MESSAGE n\xe9lg\n")

    async def test_unrepresentable_unicode_degrades_without_invalid_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "console.in"
            path.touch()
            sink = CommandSink(path, "latin1")

            await sink.send("CONSOLE_MESSAGE snowman: ☃")

            self.assertEqual(
                path.read_bytes(),
                b"CONSOLE_MESSAGE snowman: ?\n",
            )

    def test_decoded_colored_name_remains_intact_in_player_state(self):
        controller = object.__new__(TronnerRacing)
        controller.game_text_encoding = canonical_game_text_encoding("latin1")
        player = Player("nelg", "Nelg")
        controller.players = {"nelg": player}
        controller.aliases = {"nelg": player}
        raw = b"nelg 0xaaaaffn\xe9lg (HONK)"

        controller._handle_player_colored_name(
            controller._decode_game_bytes(raw, "ladderlog event")
        )

        self.assertEqual(player.colored_name, "0xaaaaffnélg (HONK)")

    def test_runtime_encoding_declaration_updates_input_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "console.in"
            path.touch()
            controller = object.__new__(TronnerRacing)
            controller.game_text_encoding = canonical_game_text_encoding("latin1")
            controller.game_text_encoding_auto = True
            controller.sink = CommandSink(path, "latin1")

            controller._apply_advertised_game_encoding("utf8")

            self.assertEqual(controller.game_text_encoding, "utf-8")
            self.assertEqual(controller.sink.encoding, "utf-8")

    async def test_live_ladderlog_encoding_change_applies_before_next_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            ladderlog = Path(tmp) / "ladderlog.txt"
            ladderlog.touch()
            console = Path(tmp) / "console.in"
            console.touch()
            controller = object.__new__(TronnerRacing)
            controller.config = {"ladderlog": str(ladderlog)}
            controller.stop_event = asyncio.Event()
            controller.game_text_encoding = canonical_game_text_encoding("latin1")
            controller.game_text_encoding_auto = True
            controller.sink = CommandSink(console, "latin1")
            received = []

            async def handle_line(line):
                received.append(line.rstrip("\r\n"))
                if line.startswith("PLAYER_COLORED_NAME"):
                    controller.stop_event.set()

            controller.handle_line = handle_line
            follower = asyncio.create_task(controller.follow_ladderlog())
            await asyncio.sleep(0.06)
            with ladderlog.open("ab") as handle:
                handle.write(
                    b"ENCODING utf8\n"
                    b"PLAYER_COLORED_NAME nelg 0xaaaaffn\xc3\xa9lg\n"
                )
                handle.flush()

            await asyncio.wait_for(follower, timeout=1)

            self.assertEqual(controller.game_text_encoding, "utf-8")
            self.assertEqual(
                received[-1],
                "PLAYER_COLORED_NAME nelg 0xaaaaffnélg",
            )


if __name__ == "__main__":
    unittest.main()
