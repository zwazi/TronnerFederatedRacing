import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from TronnerRacing import (
    GameLinkServiceError,
    Player,
    TronnerRacing,
    plain_console_text,
    redeem_game_account_link,
)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class Response:
    def __init__(self, status=200, body=b'{"linked":true}'):
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


class GameLinkTests(unittest.IsolatedAsyncioTestCase):
    def test_redeem_sends_only_the_server_authenticated_name(self):
        with mock.patch(
            "TronnerRacing.urllib.request.urlopen",
            return_value=Response(
                body=b'{"linked":true,"websiteDisplayName":"Website Racer"}'
            ),
        ) as urlopen:
            result = redeem_game_account_link(
                "https://example.test/gameLinkClaim",
                "server-secret",
                "042731",
                "Player@forums",
                "region-b",
            )

        self.assertTrue(result["linked"])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.headers["Authorization"], "Bearer server-secret")
        self.assertEqual(
            json.loads(request.data),
            {
                "code": "042731",
                "gameUsername": "Player@forums",
                "serverId": "region-b",
            },
        )

    def test_redeem_preserves_a_safe_service_error(self):
        response = Response(
            status=410,
            body=json.dumps(
                {
                    "error": {
                        "code": "code-expired",
                        "message": "That link code expired.",
                    }
                }
            ).encode("utf-8"),
        )
        http_error = __import__("urllib.error").error.HTTPError(
            "https://example.test/gameLinkClaim",
            410,
            "Gone",
            {},
            io.BytesIO(response.body),
        )
        with mock.patch(
            "TronnerRacing.urllib.request.urlopen",
            side_effect=http_error,
        ):
            with self.assertRaises(GameLinkServiceError) as raised:
                redeem_game_account_link(
                    "https://example.test/gameLinkClaim",
                    "server-secret",
                    "042731",
                    "Player@forums",
                    "region-a",
                )
        self.assertEqual(raised.exception.code, "code-expired")
        self.assertEqual(raised.exception.public_message, "That link code expired.")

    async def test_link_requires_an_authenticated_game_login(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        controller.config = {}
        controller.federation_local_server_id = "region-a"
        player = Player("racer", "Racer")

        await controller._command_link(player, "042731")

        self.assertIn(
            "Sign in to your in-game account first",
            plain_console_text(controller.sink.commands[-1]),
        )

    async def test_link_redeems_locally_on_either_federation_server(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            secret_path = Path(temporary_directory) / "game-link-secret"
            secret_path.write_text("server-secret\n", encoding="utf-8")
            controller = object.__new__(TronnerRacing)
            controller.sink = Sink()
            controller.config = {
                "game_link": {
                    "endpoint": "https://example.test/gameLinkClaim",
                    "secret_file": str(secret_path),
                    "server_id": "region-b",
                }
            }
            controller.federation_local_server_id = "region-b"
            player = Player("racer", "Racer", auth_name="Player@forums")

            with mock.patch(
                "TronnerRacing.redeem_game_account_link",
                return_value={
                    "linked": True,
                    "websiteDisplayName": "Website Racer",
                },
            ) as redeem:
                await controller._command_link(player, "042731")

            redeem.assert_called_once_with(
                "https://example.test/gameLinkClaim",
                "server-secret",
                "042731",
                "Player@forums",
                "region-b",
                10.0,
            )
            self.assertIn(
                "Linked Player@forums to Website Racer on tronner.io.",
                plain_console_text(controller.sink.commands[-1]),
            )


if __name__ == "__main__":
    unittest.main()
