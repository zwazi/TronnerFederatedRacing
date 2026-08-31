import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from TronnerRacing import (
    COLOR_FEDERATION_TAG,
    COLOR_PLAYER_ENTERED,
    COLOR_PLAYER_LEFT,
    Player,
    TronnerRacing as Controller,
    plain_console_text,
)


class CaptureSink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class MemoryStore:
    def __init__(self):
        self.values = {}

    def set_json(self, key, value):
        self.values[key] = value


def follower_controller():
    controller = Controller.__new__(Controller)
    controller.federation_role = "follower"
    controller.federation_remote_server_id = "region-a"
    controller.federation_remote_region = "A"
    controller.federation_local_server_id = "region-b"
    controller.federation_leader_server_id = "region-a"
    controller.federation_remote_regions = {"region-a": "A"}
    controller.federation_sync_chat = True
    controller.federation_sync_presence = True
    controller.federation_sync_maps = True
    controller.federation_remote_players = {}
    controller.federation_remote_maps = {}
    controller.federation_remote_rounds = {}
    controller.federation_remote_round_ready = {}
    controller.federation_remote_round_active = False
    controller.federation_remote_round_map_key = ""
    controller.federation_remote_round_started_at = ""
    controller.federation_remote_round_adopted_key = ""
    controller.federation_command_players = {}
    controller.federation_finalists = set()
    controller.federation_snapshot_received = False
    controller.federation_snapshots_received = set()
    controller.federation_last_sent_ns = 0
    controller.federation_last_state_sent_ns = {}
    controller.federation_last_boot_id = ""
    controller.federation_peer_last_received_monotonic = {"region-a": time.monotonic()}
    controller.federation_peer_timeout_seconds = 7.0
    controller.federation_prepared_map_key = None
    controller.federation_prepared_map_activate_ns = 0
    controller.federation_prepared_map_sha256 = ""
    controller.federation_map_prepare_lock = asyncio.Lock()
    controller.federation_leader_current_map_key = ""
    controller.federation_leader_next_map_key = ""
    controller._federation_server_state_last_publish_monotonic = 0.0
    controller.federation_leader_resource_base_url = ""
    controller.federation_resource_timeout_seconds = 10.0
    controller.players = {}
    controller.aliases = {}
    controller.start_preferences = {}
    controller.sink = CaptureSink()
    return controller


def event(kind, payload, sent_ns=None, server_id="region-a"):
    return json.dumps(
        {
            "version": 1,
            "server_id": server_id,
            "boot_id": "boot-a",
            "sequence": 1,
            "sent_ns": sent_ns or time.time_ns(),
            "kind": kind,
            "payload": payload,
        }
    ).encode()


class FederationChatAndPresenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_region_release_waits_for_both_healthy_followers(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        controller.federation_local_server_id = "region-a"
        controller.federation_leader_server_id = "region-a"
        controller.federation_remote_regions = {"region-b": "B", "region-c": "C"}
        controller.federation_round_sync_enabled = True
        controller.federation_round_sync_release_lead_seconds = 0.5
        controller.federation_local_round_ready_key = ""
        controller.federation_local_round_ready_at = 0.0
        controller.federation_remote_round_ready = {}
        controller.federation_round_last_release_at = 0.0
        controller.federation_round_release_key = ""
        controller.federation_peer_last_received_monotonic = {
            "region-b": time.monotonic(),
            "region-c": time.monotonic(),
        }
        controller.current = SimpleNamespace(key="Tester/maps/Race-v1.aamap.xml")
        controller.transitioning = True
        controller.transition_target_key = controller.current.key
        controller._publish_federation_control = AsyncMock(return_value=True)

        await controller._handle_local_federation_round_ready(controller.current.key)
        await controller._handle_federation_round_sync(
            "region-b",
            {"action": "ready", "map_key": controller.current.key, "ready_at": time.time()},
        )
        self.assertFalse(controller.sink.commands)
        await controller._handle_federation_round_sync(
            "region-c",
            {"action": "ready", "map_key": controller.current.key, "ready_at": time.time()},
        )
        self.assertTrue(
            controller.sink.commands[-1].startswith("FEDERATION_ROUND_RELEASE_AT")
        )

    async def test_empty_healthy_region_does_not_stall_active_region(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        controller.federation_local_server_id = "region-a"
        controller.federation_leader_server_id = "region-a"
        controller.federation_remote_regions = {"region-b": "B", "region-c": "C"}
        controller.federation_round_sync_enabled = True
        controller.federation_round_sync_release_lead_seconds = 0.5
        controller.federation_local_round_ready_key = ""
        controller.federation_local_round_ready_at = 0.0
        controller.federation_remote_round_ready = {}
        controller.federation_round_last_release_at = 0.0
        controller.federation_round_release_key = ""
        controller.federation_peer_last_received_monotonic = {
            "region-b": time.monotonic(),
            "region-c": time.monotonic(),
        }
        controller.federation_snapshots_received = {"region-b", "region-c"}
        controller.federation_remote_players = {
            "region-b\0alice": {
                "_server_id": "region-b",
                "connected": True,
                "active": True,
            }
        }
        controller.current = SimpleNamespace(key="Tester/maps/Race-v1.aamap.xml")
        controller.transitioning = True
        controller.transition_target_key = controller.current.key
        controller._publish_federation_control = AsyncMock(return_value=True)

        await controller._handle_local_federation_round_ready(controller.current.key)
        await controller._handle_federation_round_sync(
            "region-b",
            {"action": "ready", "map_key": controller.current.key, "ready_at": time.time()},
        )

        self.assertTrue(
            controller.sink.commands[-1].startswith("FEDERATION_ROUND_RELEASE_AT")
        )

    def test_one_idle_peer_cannot_clear_another_peers_active_round(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        controller.federation_local_server_id = "region-a"
        controller.federation_remote_regions = {"region-b": "B", "region-c": "C"}
        controller.current = SimpleNamespace(key="Tester/maps/Race-v1.aamap.xml")
        controller.round_active = False
        controller.round_started_epoch = 1.0
        controller.deadline_epoch = 301.0
        controller.transitioning = False
        controller._begin_helpful_message_round = Mock()
        controller._cancel_helpful_message = Mock()

        controller._handle_federation_round_state(
            "region-b",
            {"action": "round_started", "map_key": controller.current.key},
        )
        controller._handle_federation_round_state(
            "region-c",
            {"action": "round_finished", "map_key": controller.current.key},
        )

        self.assertTrue(controller.federation_remote_round_active)
        self.assertEqual(
            controller.federation_remote_round_map_key,
            controller.current.key,
        )
        self.assertEqual(
            controller.federation_remote_round_adopted_key,
            controller.current.key,
        )
        controller._cancel_helpful_message.assert_not_called()

    async def test_same_player_id_from_two_regions_remains_distinct(self):
        controller = follower_controller()
        controller.federation_remote_regions = {"region-a": "A", "region-c": "C"}
        controller.federation_leader_server_id = "region-a"
        controller.federation_snapshots_received = set()
        await controller.handle_federation_datagram(
            event(
                "player_snapshot",
                {"players": [{"player_id": "alice", "display_name": "Alice A"}]},
                server_id="region-a",
            )
        )
        await controller.handle_federation_datagram(
            event(
                "player_snapshot",
                {"players": [{"player_id": "alice", "display_name": "Alice C"}]},
                server_id="region-c",
            )
        )
        self.assertEqual(
            set(controller.federation_remote_players),
            {"region-a\0alice", "region-c\0alice"},
        )

    async def test_round_release_waits_until_both_federated_engines_are_ready(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        controller.federation_round_sync_enabled = True
        controller.federation_round_sync_release_lead_seconds = 0.5
        controller.federation_local_round_ready_key = ""
        controller.federation_remote_round_ready_key = ""
        controller.federation_local_round_ready_at = 0.0
        controller.federation_remote_round_ready_at = 0.0
        controller.federation_round_last_release_at = 0.0
        controller.federation_round_release_key = ""
        controller.current = SimpleNamespace(
            key="Tester/maps/Race-v1.aamap.xml"
        )
        controller.transitioning = True
        controller.transition_target_key = controller.current.key
        controller._publish_federation_control = AsyncMock(return_value=True)

        await controller._handle_local_federation_round_ready(
            f"{controller.current.key} 1788120000.0"
        )
        self.assertFalse(any(
            command.startswith("FEDERATION_ROUND_RELEASE_AT")
            for command in controller.sink.commands
        ))

        before = time.time()
        await controller._handle_federation_round_sync("region-a", {
            "action": "ready",
            "map_key": controller.current.key,
            "ready_at": time.time(),
        })
        release = next(
            command for command in controller.sink.commands
            if command.startswith("FEDERATION_ROUND_RELEASE_AT")
        )
        release_at = float(release.split()[1])
        self.assertGreaterEqual(release_at, before + 0.45)
        self.assertEqual(
            controller._publish_federation_control.await_count,
            3,
        )
        self.assertTrue(all(
            call.args[0] == "round_sync"
            and call.args[1]["action"] == "release"
            for call in controller._publish_federation_control.await_args_list
        ))

    async def test_follower_reports_ready_and_accepts_bounded_release(self):
        controller = follower_controller()
        controller.federation_round_sync_enabled = True
        controller.federation_local_round_ready_key = ""
        controller.federation_remote_round_ready_key = ""
        controller.federation_local_round_ready_at = 0.0
        controller.federation_remote_round_ready_at = 0.0
        controller.federation_round_last_release_at = 0.0
        controller.federation_round_release_key = ""
        controller.current = SimpleNamespace(
            key="Tester/maps/Race-v1.aamap.xml"
        )
        controller.transitioning = True
        controller.transition_target_key = controller.current.key
        controller._publish_federation_control = AsyncMock(return_value=True)

        await controller._handle_local_federation_round_ready(
            controller.current.key
        )
        self.assertEqual(
            controller._publish_federation_control.await_count,
            3,
        )

        release_at = time.time() + 0.5
        await controller._handle_federation_round_sync("region-a", {
            "action": "release",
            "map_key": controller.current.key,
            "release_at": release_at,
        })
        self.assertIn(
            f"FEDERATION_ROUND_RELEASE_AT {release_at:.6f}",
            controller.sink.commands,
        )

        await controller._handle_federation_round_sync("region-a", {
            "action": "release",
            "map_key": controller.current.key,
            "release_at": release_at,
        })
        self.assertEqual(
            controller.sink.commands.count(
                f"FEDERATION_ROUND_RELEASE_AT {release_at:.6f}"
            ),
            1,
        )

    async def test_repeated_map_waits_for_fresh_ready_from_both_engines(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        controller.federation_round_sync_enabled = True
        controller.federation_round_sync_release_lead_seconds = 0.5
        controller.federation_local_round_ready_key = ""
        controller.federation_remote_round_ready_key = ""
        controller.federation_local_round_ready_at = 0.0
        controller.federation_remote_round_ready_at = 0.0
        controller.federation_round_last_release_at = 0.0
        controller.federation_round_release_key = ""
        controller.current = SimpleNamespace(
            key="Tester/maps/Repeated-v1.aamap.xml"
        )
        controller.transitioning = False
        controller.transition_target_key = None
        controller._publish_federation_control = AsyncMock(return_value=True)

        first_ready = time.time()
        await controller._handle_local_federation_round_ready(
            f"{controller.current.key} {first_ready:.6f}"
        )
        await controller._handle_federation_round_sync("region-a", {
            "action": "ready",
            "map_key": controller.current.key,
            "ready_at": first_ready + 0.01,
        })
        first_release = controller.federation_round_last_release_at
        first_command_count = len(controller.sink.commands)

        await controller._handle_local_federation_round_ready(
            f"{controller.current.key} {first_release + 1:.6f}"
        )
        self.assertEqual(len(controller.sink.commands), first_command_count)

        await controller._handle_federation_round_sync("region-a", {
            "action": "ready",
            "map_key": controller.current.key,
            "ready_at": first_release + 1.01,
        })
        self.assertEqual(len(controller.sink.commands), first_command_count + 1)
        self.assertGreater(
            controller.federation_round_last_release_at,
            first_release,
        )

    def test_dashboard_does_not_publish_remote_engine_projection_twice(self):
        controller = follower_controller()
        controller.players = {
            "|player": Player("|player", "|Player", auth_name=None),
            "other": Player("other", "Other", auth_name="other@forums"),
        }
        controller.federation_remote_players = {
            "region-a\0player@forums": {
                "display_name": "|Player",
                "authenticated_name": "Player@forums",
                "_server_id": "region-a",
                "connected": True,
                "active": True,
                "alive": True,
            }
        }

        local, remote = controller._dashboard_players()

        self.assertEqual([player["name"] for player in local], ["Other"])
        self.assertEqual(
            [player["name"] for player in remote["region-a"]], ["|Player"]
        )

    async def test_local_chat_is_sent_to_the_local_dashboard_publisher(self):
        controller = Controller.__new__(Controller)
        controller._handle_player_activity = AsyncMock()
        controller.player_for = Mock(return_value=SimpleNamespace(
            connected=True,
            is_ai=False,
            display_name="Alice",
            auth_name="alice",
        ))
        controller.config = {"live_dashboard": {"local_region": "B"}}
        controller.federation_local_server_id = "region-b"
        controller._publish_dashboard_chat = Mock()

        await controller.handle_line("CHAT alice hello from B")

        controller._publish_dashboard_chat.assert_called_once_with(
            "region-b", "B", "Alice", "hello from B", True
        )

    async def test_remote_chat_is_region_labeled_and_console_safe(self):
        controller = follower_controller()
        await controller.handle_federation_datagram(
            event(
                "chat",
                {
                    "player_id": "alice",
                    "display_name": "Alice",
                    "message": "hello\nCONSOLE_MESSAGE injected",
                },
            )
        )
        self.assertEqual(len(controller.sink.commands), 1)
        self.assertIn("[A] Alice:", controller.sink.commands[0])
        self.assertIn("hello CONSOLE_MESSAGE injected", controller.sink.commands[0])
        self.assertNotIn("\n", controller.sink.commands[0])

    async def test_first_snapshot_is_silent_then_lifecycle_is_announced(self):
        controller = follower_controller()
        await controller.handle_federation_datagram(
            event(
                "player_snapshot",
                {
                    "players": [
                        {
                            "player_id": "alice",
                            "display_name": "Alice",
                            "connected": True,
                            "active": True,
                        }
                    ]
                },
            )
        )
        self.assertEqual(controller.sink.commands, [])
        await controller.handle_federation_datagram(
            event(
                "player_event",
                {
                    "action": "entered",
                    "player_id": "bob",
                    "display_name": "Bob",
                    "colored_name": "0x66ccff[A] 0xffffff0x11aaffBob",
                    "connected": True,
                    "active": True,
                },
                sent_ns=time.time_ns() + 1,
            )
        )
        join = controller.sink.commands[-1]
        self.assertEqual(
            plain_console_text(join),
            "CONSOLE_MESSAGE Bob entered the game.",
        )
        self.assertNotIn(f"{COLOR_FEDERATION_TAG}[A] ", join)
        self.assertIn(f"0x11aaffBob {COLOR_PLAYER_ENTERED}entered the game.", join)

        await controller.handle_federation_datagram(
            event(
                "player_event",
                {
                    "action": "left",
                    "player_id": "bob",
                    "display_name": "Bob",
                    "colored_name": "0x66ccff[A] 0xffffff0x11aaffBob",
                    "connected": False,
                    "active": True,
                },
                sent_ns=time.time_ns() + 2,
            )
        )
        leave = controller.sink.commands[-1]
        self.assertEqual(
            plain_console_text(leave),
            "CONSOLE_MESSAGE Bob left the game.",
        )
        self.assertIn(f"0x11aaffBob {COLOR_PLAYER_LEFT}left the game.", leave)

    def test_spectator_presence_uses_native_templates_and_colors(self):
        controller = follower_controller()
        spectator = {
            "display_name": "Alice",
            "colored_name": "0xff55aaAlice",
            "active": False,
        }
        entered = controller._federation_presence_message(
            "region-a",
            spectator,
            entered=True,
        )
        left = controller._federation_presence_message(
            "region-a",
            spectator,
            entered=False,
        )
        self.assertEqual(
            plain_console_text(entered),
            "Alice entered as spectator.",
        )
        self.assertIn(COLOR_PLAYER_ENTERED, entered)
        self.assertEqual(
            plain_console_text(left),
            "Spectator Alice left.",
        )
        self.assertTrue(left.startswith(COLOR_PLAYER_LEFT))
        tagged = controller._federation_presence_message(
            "region-a",
            spectator,
            entered=True,
            display_server_tags=True,
        )
        self.assertEqual(
            plain_console_text(tagged),
            "[A] Alice entered as spectator.",
        )

    async def test_join_waits_for_the_immediate_colored_name_event(self):
        controller = follower_controller()
        controller.federation_snapshot_received = True
        controller.federation_snapshots_received.add("region-a")
        now = time.time_ns()
        entered = asyncio.create_task(
            controller.handle_federation_datagram(
                event(
                    "player_event",
                    {
                        "action": "entered",
                        "player_id": "bob",
                        "display_name": "Bob",
                        "connected": True,
                        "active": True,
                    },
                    sent_ns=now,
                )
            )
        )
        await asyncio.sleep(0)
        await controller.handle_federation_datagram(
            event(
                "player_event",
                {
                    "action": "color",
                    "player_id": "bob",
                    "display_name": "Bob",
                    "colored_name": "0x66ccff[A] 0xffffff0x11aaffBob",
                    "connected": True,
                    "active": True,
                },
                sent_ns=now + 1,
            )
        )
        await entered
        self.assertIn("0x11aaffBob", controller.sink.commands[-1])

    async def test_stale_local_event_cannot_revert_newer_state(self):
        controller = follower_controller()
        now = time.time_ns()
        await controller.handle_federation_datagram(
            event("player_snapshot", {"players": []}, sent_ns=now)
        )
        await controller.handle_federation_datagram(
            event(
                "player_event",
                {
                    "action": "entered",
                    "player_id": "alice",
                    "display_name": "Alice",
                },
                sent_ns=now - 1,
            )
        )
        self.assertEqual(controller.federation_remote_players, {})

    async def test_rename_replaces_the_previous_remote_identity(self):
        controller = follower_controller()
        now = time.time_ns()
        await controller.handle_federation_datagram(
            event(
                "player_snapshot",
                {
                    "players": [
                        {
                            "player_id": "alice2",
                            "display_name": "Alice2",
                            "connected": True,
                            "active": True,
                        }
                    ]
                },
                sent_ns=now,
            )
        )
        await controller.handle_federation_datagram(
            event(
                "player_event",
                {
                    "action": "renamed",
                    "previous_player_id": "alice2",
                    "player_id": "alice",
                    "display_name": "Alice",
                    "connected": True,
                    "active": True,
                },
                sent_ns=now + 1,
            )
        )
        self.assertNotIn("region-a\0alice2", controller.federation_remote_players)
        self.assertIn("region-a\0alice", controller.federation_remote_players)
        self.assertFalse(
            controller.federation_command_players["region-a\0alice2"].connected
        )

    async def test_leader_can_import_chat_from_its_remote_peer(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        controller.federation_remote_server_id = "region-b"
        controller.federation_remote_region = "SFO"
        controller.federation_remote_regions = {"region-b": "SFO"}
        controller.federation_leader_server_id = "region-a"
        await controller.handle_federation_datagram(
            event(
                "chat",
                {
                    "player_id": "bob",
                    "display_name": "Bob",
                    "message": "hello from SFO",
                },
                server_id="region-b",
            )
        )
        self.assertIn("[SFO] Bob:", controller.sink.commands[-1])
        self.assertIn("hello from SFO", controller.sink.commands[-1])

    async def test_remote_global_command_executes_once_on_leader(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        controller.federation_local_server_id = "region-a"
        controller.federation_remote_server_id = "region-b"
        controller.federation_remote_regions = {"region-b": "B"}
        controller.federation_leader_server_id = "region-a"
        controller._dispatch_command = AsyncMock()
        await controller.handle_federation_datagram(
            event(
                "command",
                {
                    "player_id": "bob",
                    "display_name": "Bob",
                    "connected": True,
                    "active": True,
                    "command": "/skip",
                    "access_level": 20,
                    "arguments": "",
                },
                server_id="region-b",
            )
        )
        controller._dispatch_command.assert_awaited_once()
        self.assertEqual(controller._dispatch_command.await_args.args[0], "/skip")

    async def test_newer_heartbeat_does_not_suppress_reordered_command(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        controller.federation_local_server_id = "region-a"
        controller.federation_remote_server_id = "region-b"
        controller.federation_remote_regions = {"region-b": "B"}
        controller.federation_leader_server_id = "region-a"
        controller._dispatch_command = AsyncMock()
        now = time.time_ns()
        await controller.handle_federation_datagram(
            event("heartbeat", {}, sent_ns=now, server_id="region-b")
        )
        await controller.handle_federation_datagram(
            event(
                "command",
                {
                    "player_id": "bob",
                    "display_name": "Bob",
                    "connected": True,
                    "active": True,
                    "command": "/extend",
                    "access_level": 20,
                    "arguments": "",
                },
                sent_ns=now - 1,
                server_id="region-b",
            )
        )
        controller._dispatch_command.assert_awaited_once()

    async def test_peer_only_round_unblocks_authority_skip(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        controller.federation_local_server_id = "region-a"
        controller.federation_remote_server_id = "region-b"
        controller.federation_remote_regions = {"region-b": "B"}
        controller.federation_leader_server_id = "region-a"
        controller.current = SimpleNamespace(key="Tester/maps/Race-v1.aamap.xml")
        controller.round_active = False
        controller.round_started_epoch = None
        controller.deadline_epoch = None
        controller.transitioning = True
        controller.transition_target_key = controller.current.key
        controller.transition_map_confirmed = True
        controller.transition_observed_key = controller.current.key
        controller.transition_started_epoch = time.time()
        controller.transition_round_started_pending = False
        controller._transition_watchdog_task = None
        controller.store = MemoryStore()
        controller.config = {"final_countdown_idle_seconds": 0}
        controller._map_open_play_seconds = Mock(return_value=300)
        controller._begin_helpful_message_round = Mock()
        controller.private = AsyncMock()
        controller.broadcast = AsyncMock()
        controller.final_countdown_active = False
        controller.final_countdown_end_epoch = None
        controller.final_countdown_map_key = None
        controller.final_countdown_announcement = None
        controller.extend_votes = set()
        controller.skip_votes = set()
        controller.extend_vote_generation = 0
        controller.skip_vote_generation = 0

        controller._handle_federation_round_state(
            "region-b",
            {
                "action": "round_started",
                "map_key": controller.current.key,
                "started_at": "2026-08-30 16:00:00 UTC",
            }
        )

        self.assertFalse(controller.transitioning)
        self.assertTrue(controller._round_is_active())
        self.assertAlmostEqual(
            controller.deadline_epoch - controller.round_started_epoch,
            300,
        )

        item = {
            "player_id": "bob",
            "display_name": "Bob",
            "connected": True,
            "active": True,
            "alive": True,
        }
        item["_server_id"] = "region-b"
        controller.federation_remote_players["region-b\0bob"] = item
        player = controller._federation_command_player("bob", item, "region-b")
        await controller._command_skip(player)

        self.assertTrue(controller.final_countdown_active)
        controller.private.assert_not_awaited()

    async def test_round_snapshot_recovers_after_controller_restart(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        controller.federation_remote_server_id = "region-b"
        controller.federation_remote_regions = {"region-b": "B"}
        controller.federation_leader_server_id = "region-a"
        controller._handle_federation_snapshot = AsyncMock()
        controller._handle_federation_round_state = Mock()

        await controller.handle_federation_datagram(
            event(
                "player_snapshot",
                {
                    "players": [],
                    "current_map": "Tester/maps/Race-v1.aamap.xml",
                    "round_active": True,
                    "round_started_at": "2026-08-30 16:00:00 UTC",
                },
                server_id="region-b",
            )
        )

        controller._handle_federation_round_state.assert_called_once_with(
            "region-b",
            {
                "action": "round_started",
                "map_key": "Tester/maps/Race-v1.aamap.xml",
                "started_at": "2026-08-30 16:00:00 UTC",
            }
        )

    async def test_follower_defers_global_but_keeps_player_local_commands(self):
        controller = follower_controller()
        controller._dispatch_command = AsyncMock()
        await controller._handle_command("/skip bob 192.0.2.2 20")
        controller._dispatch_command.assert_not_awaited()
        await controller._handle_command("/start bob 192.0.2.2 20 immediate")
        controller._dispatch_command.assert_awaited_once()
        self.assertEqual(controller._dispatch_command.await_args.args[0], "/start")

    async def test_targeted_authority_reply_is_delivered_only_locally(self):
        controller = follower_controller()
        await controller.handle_federation_datagram(
            event(
                "controller_message",
                {
                    "scope": "private",
                    "target_server_id": "region-b",
                    "target_player_id": "bob",
                    "message": "Skip vote: 1/2 required.",
                },
            )
        )
        self.assertIn("PLAYER_MESSAGE bob", controller.sink.commands[-1])
        self.assertIn("Skip vote:", controller.sink.commands[-1])

    async def test_follower_accepts_authoritative_server_map_state(self):
        controller = follower_controller()
        controller._server_options_last = "stale"
        await controller.handle_federation_datagram(
            event(
                "controller_message",
                {
                    "scope": "server_state",
                    "current_map_key": "Tester/maps/Race-v1.aamap.xml",
                    "next_map_key": "Tester/maps/Next-v2.aamap.xml",
                },
            )
        )
        self.assertEqual(
            controller.federation_leader_current_map_key,
            "Tester/maps/Race-v1.aamap.xml",
        )
        self.assertEqual(
            controller.federation_leader_next_map_key,
            "Tester/maps/Next-v2.aamap.xml",
        )
        self.assertIsNone(controller._server_options_last)

    async def test_follower_broadcast_is_shared_without_echoing_imports(self):
        controller = follower_controller()
        controller._publish_federation_control = AsyncMock(return_value=True)
        await controller.broadcast("SFO racer finished.")
        controller._publish_federation_control.assert_awaited_once_with(
            "controller_message",
            {"scope": "broadcast", "message": "SFO racer finished."},
        )
        controller._publish_federation_control.reset_mock()
        await controller.broadcast("Imported A message.", federate=False)
        controller._publish_federation_control.assert_not_awaited()

    def test_leader_vote_population_combines_local_and_remote_players(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        local = Controller.player_for(controller, "alice", create=True)
        local.connected = local.active = local.respawn_enabled = True
        controller.federation_remote_players = {
            "region-a\0bob": {
                "player_id": "bob",
                "display_name": "Bob",
                "_server_id": "region-a",
                "connected": True,
                "active": True,
                "alive": True,
            }
        }
        self.assertEqual(len(controller.eligible_voters()), 2)


class FederationMapTests(unittest.IsolatedAsyncioTestCase):
    def test_embedded_map_size_is_the_federation_authority(self):
        controller = Controller.__new__(Controller)
        controller.config = {"default_size_factor": 0}
        controller.repository = SimpleNamespace(
            map_size_factor=lambda _entry: 10.0
        )
        entry = SimpleNamespace(key="Tester/maps/Scaled-v1.aamap.xml")

        self.assertEqual(controller._effective_map_size_factor(entry), 10.0)

    async def test_leader_publishes_future_map_before_advancing(self):
        controller = Controller.__new__(Controller)
        controller.federation_role = "leader"
        controller.federation_map_prepare_lead_seconds = 0.01
        captured = []

        async def publish(kind, payload):
            captured.append((kind, payload))
            return True

        controller._publish_federation_control = publish
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Race-v1.aamap.xml"
            path.write_bytes(b"exact map")
            entry = SimpleNamespace(
                key="Tester/maps/Race-v1.aamap.xml",
                local_path=path,
            )
            before = time.time_ns()
            await controller._prepare_federated_leader_map(entry, 0.5)
        self.assertEqual(captured[0][0], "map_prepare")
        self.assertEqual(captured[0][1]["map_key"], entry.key)
        self.assertEqual(
            captured[0][1]["map_sha256"],
            hashlib.sha256(b"exact map").hexdigest(),
        )
        self.assertGreaterEqual(captured[0][1]["activate_at_ns"], before)
        self.assertEqual(len(captured), 3)

    async def test_follower_precaches_and_waits_for_observed_commit(self):
        controller = follower_controller()
        entry = SimpleNamespace(key="Tester/maps/Race-v1.aamap.xml")

        class Repository:
            cached = []

            def find_by_spec(self, spec):
                return entry if spec == entry.key else None

            def cache_for_server(self, selected):
                self.cached.append(selected.key)

        controller.repository = Repository()
        activate_at_ns = time.time_ns() + 10_000_000
        await controller._handle_federation_map_prepare(
            {
                "map_key": entry.key,
                "size_factor": 0.5,
                "activate_at_ns": activate_at_ns,
            }
        )
        self.assertEqual(controller.repository.cached, [entry.key])
        self.assertEqual(controller.federation_prepared_map_key, entry.key)
        self.assertEqual(
            controller.federation_prepared_map_activate_ns,
            activate_at_ns,
        )
        self.assertEqual(controller.sink.commands, [])

    async def test_follower_prepare_reliability_copies_are_idempotent(self):
        controller = follower_controller()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Race-v1.aamap.xml"
            path.write_bytes(b"exact map")
            entry = SimpleNamespace(
                key="Tester/maps/Race-v1.aamap.xml",
                local_path=path,
            )

            class Repository:
                cached = []

                def find_by_spec(self, spec):
                    return entry if spec == entry.key else None

                def cache_for_server(self, selected):
                    self.cached.append(selected.key)

            controller.repository = Repository()
            payload = {
                "map_key": entry.key,
                "map_sha256": hashlib.sha256(b"exact map").hexdigest(),
                "size_factor": 0.5,
                "activate_at_ns": time.time_ns() + 10_000_000,
            }
            await asyncio.gather(
                *(
                    controller._handle_federation_map_prepare(payload)
                    for _ in range(3)
                )
            )
            self.assertEqual(controller.repository.cached, [entry.key])

    async def test_follower_refreshes_new_leader_map_without_waiting_for_poll(self):
        controller = follower_controller()
        entry = SimpleNamespace(key="Tester/maps/New-v2.aamap.xml")

        class Firebase:
            calls = 0

            def get_catalog_state(self):
                self.calls += 1
                return {
                    "catalogVersion": 12,
                    "generation": "a" * 24,
                    "serverManifestSha256": "b" * 64,
                }

        class Repository:
            def __init__(self):
                self.firebase = Firebase()
                self.available = False
                self.cached = []

            def find_by_spec(self, spec):
                return entry if self.available and spec == entry.key else None

            def sync(self, *, catalog_state):
                self.available = True

            def cache_for_server(self, selected):
                self.cached.append(selected.key)

        controller.repository = Repository()
        controller.map_lock = asyncio.Lock()
        controller.catalog_state_signature = None
        controller._reconcile_rotation = Mock()
        activate_at_ns = time.time_ns() + 10_000_000

        await controller._handle_federation_map_prepare(
            {
                "map_key": entry.key,
                "size_factor": 0.5,
                "activate_at_ns": activate_at_ns,
            }
        )

        self.assertEqual(controller.repository.cached, [entry.key])
        self.assertEqual(
            controller.catalog_state_signature,
            (12, "a" * 24, "b" * 64),
        )
        self.assertEqual(controller.repository.firebase.calls, 1)
        controller._reconcile_rotation.assert_called_once_with()

    async def test_follower_fetches_exact_map_from_leader_before_firebase(self):
        controller = follower_controller()
        entry = SimpleNamespace(
            key="Tester/maps/New-v2.aamap.xml",
            local_path=Path("/tmp/New-v2.aamap.xml"),
        )
        digest = "a" * 64

        class Firebase:
            calls = 0

            def get_catalog_state(self):
                self.calls += 1
                raise AssertionError("Firebase should not be needed")

        class Repository:
            def __init__(self):
                self.firebase = Firebase()
                self.available = False
                self.fetches = []

            def find_by_spec(self, spec):
                return entry if self.available and spec == entry.key else None

            def fetch_federated_resource(self, base_url, key, expected, timeout):
                self.fetches.append((base_url, key, expected, timeout))
                self.available = True
                return entry

        controller.repository = Repository()
        controller.map_lock = asyncio.Lock()
        controller.federation_leader_resource_base_url = "http://10.77.0.1:8080/"

        selected = await controller._find_federation_map(entry.key, digest)

        self.assertIs(selected, entry)
        self.assertEqual(controller.repository.firebase.calls, 0)
        self.assertEqual(
            controller.repository.fetches,
            [("http://10.77.0.1:8080/", entry.key, digest, 10.0)],
        )

    async def test_missing_leader_map_refresh_is_rate_limited(self):
        controller = follower_controller()

        class Firebase:
            calls = 0

            def get_catalog_state(self):
                self.calls += 1
                return {
                    "catalogVersion": 12,
                    "generation": "a" * 24,
                    "serverManifestSha256": "b" * 64,
                }

        class Repository:
            def __init__(self):
                self.firebase = Firebase()

            def find_by_spec(self, spec):
                return None

            def sync(self, *, catalog_state):
                pass

        controller.repository = Repository()
        controller.map_lock = asyncio.Lock()
        controller.catalog_state_signature = None
        controller._reconcile_rotation = Mock()

        self.assertIsNone(await controller._find_federation_map("missing"))
        self.assertIsNone(await controller._find_federation_map("missing"))
        self.assertEqual(controller.repository.firebase.calls, 1)

    async def test_leader_map_is_cached_and_applied_without_record_writes(self):
        controller = follower_controller()
        entry = SimpleNamespace(
            key="Tester/maps/Race-v1.aamap.xml",
            name="Race",
            author="Tester",
        )

        class Repository:
            def __init__(self):
                self.cached = []

            def find_by_spec(self, spec):
                return entry if spec == entry.key else None

            def cache_for_server(self, selected):
                self.cached.append(selected.key)

        controller.repository = Repository()
        controller.current = None
        controller.current_size_factor = None
        controller.current_spec = None
        controller.transitioning = False
        controller.transition_target_key = None
        controller.map_lock = asyncio.Lock()
        controller.store = MemoryStore()
        controller.round_started_epoch = 123
        controller.deadline_epoch = 456
        controller.round_active = True
        controller._clear_final_countdown_state = Mock()
        controller._clear_all_votes = Mock()
        controller._begin_map_transition = Mock()
        controller._cancel_helpful_message = Mock()
        controller._reset_attempts = Mock()
        controller._display_map_name = lambda selected: selected.name

        await controller._handle_federation_map(
            {"map_key": entry.key, "size_factor": 0.5}
        )

        self.assertEqual(controller.repository.cached, [entry.key])
        self.assertEqual(controller.deadline_epoch, None)
        self.assertEqual(controller.store.values["deadline_epoch"], None)
        self.assertEqual(
            controller.sink.commands[:5],
            [
                "SIZE_FACTOR 0.5",
                f'MAP_FILE "{entry.key}"',
                "START_NEW_MATCH",
                "KILL_ALL",
                "GET_CURRENT_MAP",
            ],
        )

    async def test_concurrent_duplicate_map_commits_apply_once(self):
        controller = follower_controller()
        entry = SimpleNamespace(
            key="Tester/maps/Race-v1.aamap.xml",
            name="Race",
            author="Tester",
        )

        class Repository:
            def __init__(self):
                self.cached = []

            def find_by_spec(self, spec):
                return entry if spec == entry.key else None

            def cache_for_server(self, selected):
                time.sleep(0.02)
                self.cached.append(selected.key)

        controller.repository = Repository()
        controller.current = None
        controller.current_size_factor = None
        controller.current_spec = None
        controller.transitioning = False
        controller.transition_target_key = None
        controller.map_lock = asyncio.Lock()
        controller.store = MemoryStore()
        controller.round_started_epoch = 123
        controller.deadline_epoch = 456
        controller.round_active = True
        controller._clear_final_countdown_state = Mock()
        controller._clear_all_votes = Mock()
        controller._begin_map_transition = Mock()
        controller._cancel_helpful_message = Mock()
        controller._reset_attempts = Mock()
        controller._display_map_name = lambda selected: selected.name

        await asyncio.gather(*(
            controller._handle_federation_map(
                {"map_key": entry.key, "size_factor": 10}
            )
            for _ in range(3)
        ))

        self.assertEqual(controller.repository.cached, [entry.key])
        self.assertEqual(controller.sink.commands.count("KILL_ALL"), 1)

    async def test_leader_publishes_map_change_with_previous_map_key(self):
        controller = follower_controller()
        controller.federation_role = "leader"
        controller.controller_reload_draining = False
        controller.map_lock = asyncio.Lock()
        controller.current = SimpleNamespace(key="Tester/maps/Old-v1.aamap.xml")
        entry = SimpleNamespace(
            key="Tester/maps/New-v2.aamap.xml",
            name="New",
            author="Tester",
        )

        class Repository:
            @staticmethod
            def cache_for_server(selected):
                return None

        controller.repository = Repository()
        controller.store = MemoryStore()
        controller._take_next = Mock(return_value=entry)
        controller._effective_map_size_factor = Mock(return_value=1.0)
        controller._prepare_federated_leader_map = AsyncMock()
        controller._map_open_play_seconds = Mock(return_value=120.0)
        controller._clear_final_countdown_state = Mock()
        controller._clear_all_votes = Mock()
        controller._begin_map_transition = Mock()
        controller._cancel_helpful_message = Mock()
        controller._reset_attempts = Mock()
        controller._publish_dashboard_map_change = Mock()
        controller._display_map_name = lambda selected: selected.name
        controller.broadcast = AsyncMock()

        await controller.activate_next_map("round timer expired")

        self.assertEqual(controller.current, entry)
        controller._publish_dashboard_map_change.assert_called_once_with(
            "Tester/maps/Old-v1.aamap.xml"
        )

    async def test_follower_blocks_automatic_rotation_but_not_admin_rotation(self):
        controller = follower_controller()
        await controller.activate_next_map("final countdown expired")
        self.assertEqual(controller.sink.commands, [])

        # An administrator's local action remains local to SFO. The rest of
        # activate_next_map is replaced because this test only verifies the
        # follower authority gate.
        controller.controller_reload_draining = True
        await controller.activate_next_map("admin force skip")


if __name__ == "__main__":
    unittest.main()
