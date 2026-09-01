import asyncio
import dataclasses
import unittest
from pathlib import Path

from TronnerRacing import (
    DEFAULT_START_COUNTDOWN_SECONDS,
    MapEntry,
    MAX_START_COUNTDOWN_SECONDS,
    Player,
    SpawnPoint,
    TronnerRacing,
    normalize_start_preference,
    plain_console_text,
    start_preference_details,
)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class Store:
    def __init__(self):
        self.values = {}

    def set_json(self, key, value):
        self.values[key] = value


def start_controller(mode="brake"):
    controller = object.__new__(TronnerRacing)
    controller.current = MapEntry(
        "map",
        "Map",
        "Author",
        "v1",
        "maps",
        "map",
        Path("map"),
        (SpawnPoint(3, 4, 0, 1),),
    )
    controller.config = {"go_message_seconds": 30}
    controller.sink = Sink()
    controller.store = Store()
    controller.freeze_tasks = {}
    controller.center_clear_tasks = {}
    controller.spawn_preferences = {}
    controller.start_preferences = {}
    controller.final_countdown_active = False
    controller.transitioning = False
    controller.respawns_paused = False
    player = Player("racer", "Racer", start_mode=mode)
    controller.start_preferences[player.identity_key] = mode
    controller.players = {"racer": player}
    controller.aliases = {"racer": player}
    return controller, player


async def cancel_player_tasks(controller, player):
    tasks = []
    for task_map in (controller.freeze_tasks, controller.center_clear_tasks):
        task = task_map.pop(id(player), None)
        if task:
            task.cancel()
            tasks.append(task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class StartModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_brake_mode_clears_prompt_without_showing_go(self):
        controller, player = start_controller("brake")

        await controller._respawn_player(player)
        await asyncio.sleep(0)

        self.assertIn(
            "RESPAWN_PLAYER_HELD racer false 3 4 0 1",
            controller.sink.commands,
        )
        self.assertIn(
            'CENTER_PLAYER_MESSAGE racer "Press brake to start"',
            [plain_console_text(command) for command in controller.sink.commands],
        )

        await controller._handle_cycle_released("racer 123.456789")

        self.assertEqual(player.attempt_started_game, 123.456789)
        self.assertFalse(player.pending_respawn)
        center_messages = [
            plain_console_text(command)
            for command in controller.sink.commands
            if command.startswith("CENTER_PLAYER_MESSAGE racer")
        ]
        self.assertFalse(any("GO!" in message for message in center_messages))
        self.assertTrue(any(message.endswith('""') for message in center_messages))
        await cancel_player_tasks(controller, player)

    async def test_immediate_mode_uses_unheld_respawn(self):
        controller, player = start_controller("immediate")

        await controller._respawn_player(player)

        self.assertEqual(
            controller.sink.commands,
            ["RESPAWN_PLAYER racer false 3 4 0 1"],
        )
        self.assertNotIn(id(player), controller.freeze_tasks)
        controller._handle_cycle_created("racer 3 4 0 1 50.25")
        self.assertFalse(player.pending_respawn)
        self.assertEqual(player.attempt_started_game, 50.25)
        self.assertEqual(player.attempt_number, 1)

    async def test_release_uses_authoritative_physical_clock_origin(self):
        controller, player = start_controller("brake")

        await controller._respawn_player(player)
        await asyncio.sleep(0)
        await controller._handle_cycle_released("racer 123.75")

        self.assertEqual(player.attempt_started_game, 123.75)
        self.assertFalse(player.pending_respawn)
        await cancel_player_tasks(controller, player)

    async def test_countdown_mode_uses_exact_timed_hold_and_go_cue(self):
        controller, player = start_controller("countdown")

        await controller._respawn_player(player)
        await asyncio.sleep(0)

        self.assertEqual(
            controller.sink.commands[:2],
            [
                "RESPAWN_PLAYER_HELD racer false 3 4 0 1",
                "FREEZE_PLAYER racer 3",
            ],
        )
        self.assertIn(
            'CENTER_PLAYER_MESSAGE racer "3"',
            [plain_console_text(command) for command in controller.sink.commands],
        )

        await controller._handle_cycle_released("racer 53.25")

        self.assertIn(
            'CENTER_PLAYER_MESSAGE racer "     GO!     "',
            [plain_console_text(command) for command in controller.sink.commands],
        )
        await cancel_player_tasks(controller, player)

    async def test_countdown_shares_the_center_with_checkpoint_progress(self):
        controller, player = start_controller("countdown")
        controller.current = dataclasses.replace(
            controller.current, checkpoint_ids=(1, 2, 3)
        )
        player.checkpoints_collected = {1}

        await controller._respawn_player(player)
        await asyncio.sleep(0)

        self.assertIn(
            'CENTER_PLAYER_MESSAGE racer "3                                  0/3"',
            controller.sink.commands,
        )
        self.assertNotIn('CENTER_PLAYER_MESSAGE racer "3"', controller.sink.commands)
        await cancel_player_tasks(controller, player)

    async def test_custom_countdown_drives_engine_and_visible_timer(self):
        controller, player = start_controller("brake")

        await controller._command_start(player, "countdown 7")
        await controller._respawn_player(player)
        await asyncio.sleep(0)

        self.assertEqual(player.start_mode, "countdown")
        self.assertEqual(player.start_countdown_seconds, 7)
        self.assertEqual(
            controller.start_preferences[player.identity_key],
            "countdown 7",
        )
        self.assertEqual(
            controller.store.values["start_preferences"],
            {player.identity_key: "countdown 7"},
        )
        self.assertIn(
            "RESPAWN_PLAYER_HELD racer false 3 4 0 1",
            controller.sink.commands,
        )
        self.assertIn(
            "FREEZE_PLAYER racer 7",
            controller.sink.commands,
        )
        self.assertIn(
            'CENTER_PLAYER_MESSAGE racer "7"',
            [plain_console_text(command) for command in controller.sink.commands],
        )
        self.assertTrue(
            any(
                "7-second countdown" in plain_console_text(command)
                for command in controller.sink.commands
            )
        )
        await controller._handle_cycle_released("racer 57.25")
        self.assertEqual(player.attempt_started_game, 57.25)
        self.assertFalse(player.pending_respawn)
        await cancel_player_tasks(controller, player)

    async def test_plain_countdown_keeps_three_second_default(self):
        controller, player = start_controller("brake")

        await controller._command_start(player, "countdown")

        self.assertEqual(
            player.start_countdown_seconds,
            DEFAULT_START_COUNTDOWN_SECONDS,
        )
        self.assertEqual(
            controller.start_preferences[player.identity_key],
            "countdown",
        )

    async def test_countdown_rejects_invalid_seconds_without_changing_mode(self):
        invalid = (
            "countdown 0",
            "countdown 61",
            "countdown 1.5",
            "countdown 5 extra",
        )
        for argument in invalid:
            with self.subTest(argument=argument):
                controller, player = start_controller("brake")

                await controller._command_start(player, argument)

                self.assertEqual(player.start_mode, "brake")
                self.assertEqual(
                    controller.start_preferences[player.identity_key],
                    "brake",
                )
                self.assertTrue(
                    any(
                        "whole number from 1 to 60" in plain_console_text(command)
                        for command in controller.sink.commands
                    )
                )

    async def test_start_command_persists_preference(self):
        controller, player = start_controller("brake")

        await controller._command_start(player, "countdown")

        self.assertEqual(player.start_mode, "countdown")
        self.assertEqual(controller.start_preferences[player.identity_key], "countdown")
        self.assertEqual(
            controller.store.values["start_preferences"],
            {player.identity_key: "countdown"},
        )
        self.assertTrue(
            any(
                "Start mode set to countdown" in plain_console_text(command)
                for command in controller.sink.commands
            )
        )

    def test_new_player_defaults_to_immediate(self):
        self.assertEqual(Player("racer", "Racer").start_mode, "immediate")

    def test_start_preference_parser_is_bounded_and_backward_compatible(self):
        self.assertEqual(normalize_start_preference("countdown"), "countdown")
        self.assertEqual(normalize_start_preference("countdown 3"), "countdown")
        self.assertEqual(
            normalize_start_preference(f"countdown {MAX_START_COUNTDOWN_SECONDS}"),
            f"countdown {MAX_START_COUNTDOWN_SECONDS}",
        )
        self.assertIsNone(normalize_start_preference("countdown -1"))
        self.assertEqual(
            start_preference_details("countdown 12"),
            ("countdown", 12, "countdown 12"),
        )


if __name__ == "__main__":
    unittest.main()
