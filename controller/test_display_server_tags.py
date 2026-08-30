import unittest

from TronnerRacing import Player, TronnerRacing, plain_console_text


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class Store:
    def __init__(self):
        self.values = {}

    def set_json(self, key, value):
        self.values[key] = dict(value)


def controller_and_player():
    controller = object.__new__(TronnerRacing)
    controller.sink = Sink()
    controller.store = Store()
    controller.display_server_tag_preferences = {}
    controller.start_preferences = {}
    controller.spawn_preferences = {}
    controller.current = None
    player = Player("racer", "Racer", connected=True)
    controller.players = {"racer": player}
    controller.aliases = {"racer": player}
    return controller, player


class DisplayServerTagsTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_toggles_and_persists_the_viewer_preference(self):
        controller, player = controller_and_player()

        await controller._command_display_server_tags(player)

        self.assertTrue(player.display_server_tags)
        self.assertEqual(
            controller.display_server_tag_preferences,
            {player.identity_key: True},
        )
        self.assertEqual(
            controller.store.values["display_server_tag_preferences"],
            {player.identity_key: True},
        )
        self.assertEqual(
            controller.sink.commands[0],
            "FEDERATION_DISPLAY_SERVER_TAGS racer 1",
        )
        self.assertIn(
            "other players' names are now enabled",
            plain_console_text(controller.sink.commands[1]),
        )

        await controller._command_display_server_tags(player)

        self.assertFalse(player.display_server_tags)
        self.assertEqual(
            controller.sink.commands[-2],
            "FEDERATION_DISPLAY_SERVER_TAGS racer 0",
        )

    async def test_saved_preference_is_reapplied_to_the_viewer(self):
        controller, player = controller_and_player()
        controller.display_server_tag_preferences[player.identity_key] = True

        await controller._apply_display_server_tag_preference(player)

        self.assertTrue(player.display_server_tags)
        self.assertEqual(
            controller.sink.commands,
            ["FEDERATION_DISPLAY_SERVER_TAGS racer 1"],
        )

    async def test_login_migrates_guest_preference_to_authenticated_identity(self):
        controller, player = controller_and_player()
        guest_identity = player.identity_key
        controller.display_server_tag_preferences[guest_identity] = True

        logged_in = controller._handle_player_login("racer Racer@forums")

        self.assertIs(logged_in, player)
        self.assertEqual(player.identity_key, "auth:racer@forums")
        self.assertTrue(player.display_server_tags)
        self.assertTrue(
            controller.display_server_tag_preferences["auth:racer@forums"]
        )


if __name__ == "__main__":
    unittest.main()
