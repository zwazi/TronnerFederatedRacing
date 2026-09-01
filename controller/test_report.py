import collections
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from TronnerRacing import (
    Player,
    TronnerRacing,
    plain_console_text,
    send_resend_report,
)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class Response:
    def __init__(self, status=200, body=b'{"success": true}'):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getcode(self):
        return self.status

    def read(self, maximum):
        return self.body[:maximum]


class ReportTests(unittest.IsolatedAsyncioTestCase):
    def test_resend_request_contains_complete_email_and_requires_id(self):
        with mock.patch(
            "TronnerRacing.urllib.request.urlopen",
            return_value=Response(body=b'{"id": "email-id"}'),
        ) as urlopen:
            send_resend_report(
                "test-key",
                "owner@example.com",
                "Reports <onboarding@resend.dev>",
                "Report subject",
                "Full report body",
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.resend.com/emails")
        self.assertEqual(request.headers["Authorization"], "Bearer test-key")
        self.assertEqual(
            json.loads(request.data),
            {
                "from": "Reports <onboarding@resend.dev>",
                "to": ["owner@example.com"],
                "subject": "Report subject",
                "text": "Full report body",
            },
        )

        with mock.patch(
            "TronnerRacing.urllib.request.urlopen",
            return_value=Response(body=b'{"message": "rejected"}'),
        ):
            with self.assertRaises(RuntimeError):
                send_resend_report(
                    "test-key",
                    "owner@example.com",
                    "Reports <onboarding@resend.dev>",
                    "Report subject",
                    "Full report body",
                )

    async def test_report_includes_both_identities_and_confirms_privately(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            key_path = Path(temporary_directory) / "resend-key"
            key_path.write_text("test-key\n", encoding="utf-8")
            controller = object.__new__(TronnerRacing)
            controller.sink = Sink()
            controller.config = {
                "resend_api_key_file": str(key_path),
                "report_recipient": "owner@example.com",
                "report_sender": "Reports <onboarding@resend.dev>",
                "report_timezone": "America/Phoenix",
            }
            controller.report_last_sent = {}
            controller.report_success_epochs = collections.deque()
            saved = {}
            controller.store = SimpleNamespace(
                set_json=lambda key, value: saved.__setitem__(key, value)
            )
            controller.current = SimpleNamespace(name="Glacier")
            controller._display_map_name = lambda entry: entry.name
            player = Player(
                "racer@forums",
                "Display Racer",
                auth_name="racer@forums",
            )

            with mock.patch(
                "TronnerRacing.send_resend_report"
            ) as send, mock.patch(
                "TronnerRacing.asyncio.to_thread",
                new=mock.AsyncMock(
                    side_effect=lambda function, *args: function(*args)
                ),
            ):
                await controller._command_report(
                    player, "A player is griefing.", access_level=1
                )

            send.assert_called_once()
            api_key, recipient, sender, subject, body = send.call_args.args[:5]
            self.assertEqual(api_key, "test-key")
            self.assertEqual(recipient, "owner@example.com")
            self.assertEqual(sender, "Reports <onboarding@resend.dev>")
            self.assertIn("Display Racer", subject)
            self.assertIn("racer@forums", subject)
            self.assertIn("Display username: Display Racer", body)
            self.assertIn("Authenticated username: racer@forums", body)
            self.assertIn("Current map: Glacier", body)
            self.assertIn("Report:\nA player is griefing.", body)
            self.assertEqual(len(saved["report_success_epochs"]), 1)
            self.assertIn(
                'PLAYER_MESSAGE racer@forums "Your report was sent. Thank you."',
                [plain_console_text(command) for command in controller.sink.commands],
            )

    async def test_report_validates_message(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        controller.config = {"report_maximum_characters": 5}
        controller.report_last_sent = {}
        controller.report_success_epochs = collections.deque()
        controller.store = SimpleNamespace(set_json=lambda key, value: None)
        controller.current = None
        player = Player("racer", "Racer")

        await controller._command_report(player, "", access_level=20)
        await controller._command_report(player, "123456", access_level=20)

        messages = [plain_console_text(command) for command in controller.sink.commands]
        self.assertIn('PLAYER_MESSAGE racer "Usage: /report [message]"', messages)
        self.assertIn(
            'PLAYER_MESSAGE racer "Reports may be at most 5 characters."',
            messages,
        )

    async def test_admins_receive_the_shorter_report_cooldown(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        controller.config = {
            "report_recipient": "owner@example.com",
            "report_sender": "Reports <onboarding@resend.dev>",
            "report_cooldown_seconds": 300,
            "report_admin_access_level": 1,
            "report_admin_cooldown_seconds": 30,
        }
        controller.report_success_epochs = collections.deque()
        controller.store = SimpleNamespace(set_json=lambda key, value: None)
        controller.current = None
        controller._report_api_key = lambda: "test-key"
        player = Player("racer", "Racer")

        controller.report_last_sent = {player.identity_key: time.monotonic()}
        await controller._command_report(
            player, "Admin follow-up", access_level=1
        )
        admin_message = plain_console_text(controller.sink.commands[-1])

        controller.sink.commands.clear()
        await controller._command_report(
            player, "Player follow-up", access_level=20
        )
        player_message = plain_console_text(controller.sink.commands[-1])

        self.assertIn("wait 30 seconds", admin_message)
        self.assertIn("wait 300 seconds", player_message)

    async def test_suggest_reuses_resend_configuration_and_quota(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        controller.config = {
            "report_recipient": "owner@example.com",
            "report_sender": "Reports <onboarding@resend.dev>",
            "report_timezone": "America/Phoenix",
        }
        controller.report_last_sent = {}
        controller.report_success_epochs = collections.deque()
        controller.store = SimpleNamespace(set_json=lambda key, value: None)
        controller.current = SimpleNamespace(name="Glacier")
        controller._display_map_name = lambda entry: entry.name
        controller._report_api_key = lambda: "test-key"
        player = Player("racer", "Display Racer", auth_name="racer@forums")

        with mock.patch("TronnerRacing.send_resend_report") as send, mock.patch(
            "TronnerRacing.asyncio.to_thread",
            new=mock.AsyncMock(
                side_effect=lambda function, *args: function(*args)
            ),
        ):
            await controller._command_suggest(
                player, "Add a ghost racing mode.", access_level=20
            )

        send.assert_called_once()
        _, recipient, sender, subject, body = send.call_args.args[:5]
        self.assertEqual(recipient, "owner@example.com")
        self.assertEqual(sender, "Reports <onboarding@resend.dev>")
        self.assertIn("Feature suggestion", subject)
        self.assertIn("Suggestion:\nAdd a ghost racing mode.", body)
        self.assertEqual(len(controller.report_success_epochs), 1)
        messages = [plain_console_text(item) for item in controller.sink.commands]
        self.assertIn(
            'PLAYER_MESSAGE racer "Your suggestion was sent. Thank you."',
            messages,
        )


if __name__ == "__main__":
    unittest.main()
