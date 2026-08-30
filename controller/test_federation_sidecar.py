import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from federation_protocol import Packet, ProtocolError, encode_packet
from federation_sidecar import (
    ConfigurationError,
    ControllerPublishProtocol,
    FederationConfig,
    FollowerQueues,
    LadderlogProjection,
    LocalEventForwarder,
    NetworkFollowerProtocol,
    parse_engine_telemetry,
    publish_engine_telemetry,
    read_key,
)


KEY = b"f" * 32


def follower_config(**updates):
    raw = {
        "cluster_id": "tronner-racing",
        "server_id": "region-b",
        "mode": "follow",
        "region_label": "SFO",
        "listen_host": "127.0.0.1",
        "listen_port": 4540,
        "expected_server_id": "region-a",
        "expected_peer_ip": "127.0.0.1",
        "receive_key_file": "/tmp/key",
        "controller_import_socket": "/tmp/controller.sock",
        "engine_import_socket": "/tmp/engine.sock",
    }
    raw.update(updates)
    return FederationConfig.from_dict(raw)


class ConfigurationTests(unittest.TestCase):
    def test_follower_config(self):
        config = follower_config()
        self.assertTrue(config.follows)
        self.assertFalse(config.publishes)
        self.assertEqual(config.expected_server_id, "region-a")

    def test_publish_requires_outbound_fields(self):
        with self.assertRaisesRegex(ConfigurationError, "peer_host"):
            FederationConfig.from_dict(
                {
                    "cluster_id": "tronner-racing",
                    "server_id": "region-a",
                    "mode": "publish",
                }
            )

    def test_key_supports_hex_and_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key"
            path.write_text("ab" * 32)
            path.chmod(0o600)
            self.assertEqual(read_key(path), bytes.fromhex("ab" * 32))
            path.write_bytes(KEY)
            self.assertEqual(read_key(path), KEY)

    def test_follower_requires_literal_expected_peer_address(self):
        with self.assertRaisesRegex(ConfigurationError, "expected_peer_ip"):
            follower_config(expected_peer_ip="")
        with self.assertRaisesRegex(ConfigurationError, "literal IPv4"):
            follower_config(expected_peer_ip="peer.example.invalid")

    def test_key_rejects_world_readable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key"
            path.write_text("ab" * 32)
            path.chmod(0o644)
            with self.assertRaisesRegex(ConfigurationError, "accessible by others"):
                read_key(path)


class LadderlogProjectionTests(unittest.TestCase):
    def test_round_state_snapshot_tracks_current_map_lifecycle(self):
        state = LadderlogProjection()
        state.consume("CURRENT_MAP 0 0 Tester/maps/Race-v1.aamap.xml")
        state.consume("ROUND_STARTED 2026-08-30 16:00:00 UTC")

        active = state.snapshot_payload()
        self.assertTrue(active["round_active"])
        self.assertEqual(
            active["round_started_at"], "2026-08-30 16:00:00 UTC"
        )

        state.consume("NEW_ROUND")
        inactive = state.snapshot_payload()
        self.assertFalse(inactive["round_active"])
        self.assertIsNone(inactive["round_started_at"])

        state.consume("ROUND_STARTED 2026-08-30 16:01:00 UTC")
        state.consume("CURRENT_MAP 0 0 Tester/maps/Next-v1.aamap.xml")
        changed = state.snapshot_payload()
        self.assertFalse(changed["round_active"])
        self.assertIsNone(changed["round_started_at"])

    def test_chat_uses_known_display_metadata(self):
        state = LadderlogProjection()
        events = state.consume("PLAYER_ENTERED_GRID alice 192.0.2.1 Alice Rider")
        self.assertNotIn("192.0.2.1", json.dumps(events))
        state.consume("PLAYER_COLORED_NAME alice 0x11aaffAlice")
        events = state.consume("CHAT alice hello from A")
        self.assertEqual(events[0][0], "chat")
        self.assertEqual(events[0][1]["display_name"], "Alice Rider")
        self.assertEqual(events[0][1]["message"], "hello from A")

    def test_map_commit_is_normalized(self):
        state = LadderlogProjection()
        events = state.consume("CURRENT_MAP -2 0.5 Author/maps/Test-v1.aamap.xml")
        self.assertEqual(events[0][0], "map_commit")
        self.assertEqual(events[0][1]["size_factor"], -2.0)
        self.assertEqual(state.current_map, "Author/maps/Test-v1.aamap.xml")

    def test_player_left_clears_snapshot(self):
        state = LadderlogProjection()
        state.consume("PLAYER_ENTERED_GRID alice 192.0.2.1 Alice")
        state.consume("PLAYER_LEFT alice 192.0.2.1 Alice")
        self.assertEqual(state.snapshot_payload()["players"], [])

    def test_online_player_updates_federated_cycle_color(self):
        state = LadderlogProjection()
        state.consume("ONLINE_PLAYER alice 1 2 7 14 0 0 0.125 team")
        self.assertEqual(state.player("alice").rgb, (2, 7, 14))
        self.assertEqual(state.player("alice").ping, 0.125)

    def test_online_player_color_matches_native_clamping(self):
        state = LadderlogProjection()
        state.consume("ONLINE_PLAYER alice 1 -1 65514 16 0 0 25.5 team")
        self.assertEqual(state.player("alice").rgb, (0, 15, 15))

    def test_online_snapshot_removes_stale_bootstrap_players(self):
        state = LadderlogProjection()
        state.consume("PLAYER_ENTERED_GRID stale 192.0.2.1 Stale")
        state.consume("PLAYER_ENTERED_GRID current 192.0.2.2 Current")
        state.consume("ONLINE_PLAYER current 1 1 2 3 20 0 0.125 team")
        state.consume("ONLINE_PLAYERS_COUNT 1 0 1 0 0 0 1")
        state.consume("ONLINE_PLAYERS_ALIVE current [a]_current")
        state.consume("ONLINE_PLAYERS_COUNT 1 0 1 0 0 0 1")
        state.consume("ONLINE_PLAYERS_ALIVE current [a]_current")

        players = state.snapshot_payload()["players"]
        self.assertEqual([player["player_id"] for player in players], ["current"])
        self.assertFalse(state.players["stale"].connected)
        self.assertNotIn("[a]_current", state.players)

    def test_invalid_chat_and_control_characters_do_not_escape_event(self):
        state = LadderlogProjection()
        self.assertEqual(state.consume("CHAT bad\x01id message"), [])
        events = state.consume("CHAT alice hello\nCONSOLE_MESSAGE injected")
        self.assertEqual(len(events), 1)
        self.assertNotIn("\n", events[0][1]["message"])

    def test_command_is_forwarded_without_the_player_ip(self):
        state = LadderlogProjection()
        state.consume("PLAYER_ENTERED_GRID alice 192.0.2.10 Alice")
        events = state.consume("COMMAND /skip alice 192.0.2.10 20 ignored")
        self.assertEqual(events[0][0], "command")
        self.assertEqual(events[0][1]["command"], "/skip")
        self.assertEqual(events[0][1]["access_level"], 20)
        self.assertNotIn("192.0.2.10", json.dumps(events))

    def test_rename_carries_the_previous_identity(self):
        state = LadderlogProjection()
        state.consume("PLAYER_ENTERED_GRID alice2 192.0.2.1 Alice2")
        events = state.consume(
            "PLAYER_RENAMED alice2 alice 192.0.2.1 0 Alice"
        )
        self.assertEqual(events[0][1]["action"], "renamed")
        self.assertEqual(events[0][1]["previous_player_id"], "alice2")
        self.assertEqual(events[0][1]["player_id"], "alice")
        self.assertNotIn("alice2", state.players)


class TelemetryTests(unittest.TestCase):
    def test_cycle_telemetry(self):
        payload = parse_engine_telemetry(
            b"CYCLE_V1 alice 12.5 1 2 0 1 30.25 0.042 1 1 0.2 0.4 0.6"
        )
        self.assertEqual(payload["player_id"], "alice")
        self.assertEqual(payload["speed"], 30.25)
        self.assertEqual(payload["ping"], 0.042)
        self.assertTrue(payload["alive"])
        self.assertTrue(payload["chatting"])
        self.assertEqual(payload["cycle_rgb"], [0.2, 0.4, 0.6])

    def test_previous_cycle_telemetry_has_no_chatting_state(self):
        payload = parse_engine_telemetry(
            b"CYCLE_V1 alice 12.5 1 2 0 1 30.25 0.042 1"
        )
        self.assertNotIn("chatting", payload)

    def test_nonfinite_and_bad_schema_are_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "non-finite"):
            parse_engine_telemetry(b"CYCLE_V1 alice 1 nan 2 0 1 30 1")
        with self.assertRaisesRegex(ProtocolError, "schema"):
            parse_engine_telemetry(b"CYCLE_V1 too short")
        with self.assertRaisesRegex(ProtocolError, "chatting"):
            parse_engine_telemetry(
                b"CYCLE_V1 alice 12.5 1 2 0 1 30.25 0.042 1 2"
            )
        with self.assertRaisesRegex(ProtocolError, "cycle color"):
            parse_engine_telemetry(
                b"CYCLE_V1 alice 12.5 1 2 0 1 30.25 0.042 1 0 5 0 0"
            )


class TelemetryPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_is_enriched_with_projected_player_color(self):
        projection = LadderlogProjection()
        projection.consume("ONLINE_PLAYER alice 1 2 7 14 0 0 25.5 team")
        queue = asyncio.Queue()

        class Publisher:
            def __init__(self):
                self.payload = None
                self.sent = asyncio.Event()

            async def send(self, kind, payload):
                self.payload = payload
                self.sent.set()

        publisher = Publisher()
        task = asyncio.create_task(
            publish_engine_telemetry(queue, projection, publisher)
        )
        await queue.put(
            {
                "player_id": "alice",
                "observed_ns": time.time_ns(),
                "game_time": 1.0,
                "x": 2.0,
                "y": 3.0,
                "xdir": 1.0,
                "ydir": 0.0,
                "speed": 30.0,
                "ping": 0.042,
                "alive": True,
            }
        )
        await asyncio.wait_for(publisher.sent.wait(), timeout=1)
        self.assertEqual(publisher.payload["rgb"], [2, 7, 14])
        self.assertEqual(publisher.payload["ping"], 0.042)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class LocalForwarderTests(unittest.IsolatedAsyncioTestCase):
    async def test_backlogged_local_consumer_does_not_block_forwarding(self):
        class BackloggedSocket:
            def __init__(self):
                self.calls = []

            def sendto(self, data, path):
                self.calls.append((data, path))
                raise BlockingIOError

            def close(self):
                pass

        forwarder = LocalEventForwarder(follower_config())
        forwarder.socket.close()
        forwarder.socket = BackloggedSocket()

        with self.assertLogs("TronnerFederation", level="WARNING") as logs:
            await asyncio.wait_for(
                forwarder._send(Path("/tmp/backlogged.sock"), b"event"),
                timeout=0.1,
            )

        self.assertEqual(
            forwarder.socket.calls,
            [(b"event", "/tmp/backlogged.sock")],
        )
        self.assertIn("consumer is backlogged", "\n".join(logs.output))
        forwarder.close()

    async def test_engine_line_hex_encodes_names_and_includes_color(self):
        forwarder = LocalEventForwarder(follower_config())
        sent = []

        async def capture(path, data):
            sent.append((path, data))

        forwarder._send = capture
        packet = Packet(
            version=1,
            cluster_id="tronner-racing",
            server_id="region-a",
            boot_id="boot-a",
            sequence=7,
            sent_ns=time.time_ns(),
            kind="cycle",
            payload={
                "player_id": "alice",
                "display_name": "Alice Rider",
                "colored_name": "0xff0000Alice",
                "authenticated_name": "Alice@forums",
                "rgb": [15, 2, 3],
                "observed_ns": time.time_ns(),
                "game_time": 12.5,
                "x": 1,
                "y": 2,
                "xdir": 1,
                "ydir": 0,
                "speed": 30,
                "alive": True,
                "chatting": True,
                "cycle_rgb": [0.2, 0.4, 0.6],
            },
        )
        await forwarder.engine(packet)
        self.assertEqual(len(sent), 3)
        color_parts = sent[0][1].decode("ascii").split()
        self.assertEqual(color_parts[:2], ["GHOST_V1", "COLOR"])
        for actual, expected in zip(map(float, color_parts[-3:]), (0.2, 0.4, 0.6)):
            self.assertAlmostEqual(actual, expected)
        parts = sent[1][1].decode("ascii").split()
        self.assertEqual(parts[:2], ["GHOST_V2", "STATE"])
        self.assertEqual(len(parts), 18)
        self.assertEqual(bytes.fromhex(parts[3]).decode(), "Alice Rider")
        self.assertEqual(bytes.fromhex(parts[5]).decode(), "Alice@forums")
        self.assertEqual(parts[6:9], ["15", "2", "3"])
        self.assertEqual(parts[9], "0")
        flag_parts = sent[2][1].decode("ascii").split()
        self.assertEqual(flag_parts[:2], ["GHOST_V1", "FLAGS"])
        self.assertEqual(flag_parts[2], parts[2])
        self.assertEqual(flag_parts[-1], "1")
        forwarder.close()

    async def test_rename_removes_the_previous_engine_identity_first(self):
        forwarder = LocalEventForwarder(follower_config())
        sent = []

        async def capture(path, data):
            sent.append(data.decode("ascii").split())

        forwarder._send = capture
        packet = Packet(
            version=1,
            cluster_id="tronner-racing",
            server_id="region-a",
            boot_id="boot-a",
            sequence=9,
            sent_ns=time.time_ns(),
            kind="player_event",
            payload={
                "action": "renamed",
                "previous_player_id": "alice2",
                "player_id": "alice",
                "display_name": "Alice",
                "connected": True,
            },
        )
        await forwarder.engine_presence(packet)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][1], "PRESENCE")
        self.assertEqual(sent[0][-1], "0")
        self.assertEqual(sent[1][1], "PRESENCE")
        self.assertEqual(sent[1][-1], "1")
        self.assertNotEqual(sent[0][2], sent[1][2])
        forwarder.close()

    async def test_chat_is_attributed_to_a_present_remote_player(self):
        forwarder = LocalEventForwarder(follower_config())
        sent = []

        async def capture(path, data):
            sent.append(data.decode("ascii"))

        forwarder._send = capture
        packet = Packet(
            version=1,
            cluster_id="tronner-racing",
            server_id="region-a",
            boot_id="boot-a",
            sequence=8,
            sent_ns=time.time_ns(),
            kind="chat",
            payload={
                "player_id": "alice",
                "display_name": "Alice",
                "colored_name": "0xff0000Alice",
                "rgb": [15, 0, 0],
                "ping": 0.125,
                "message": "hello",
            },
        )
        await forwarder.engine_chat(packet)
        self.assertTrue(sent[0].startswith("GHOST_V2 PRESENCE "))
        self.assertIn(" 0.125 ", sent[0])
        self.assertTrue(sent[1].startswith("GHOST_V1 CHAT "))
        self.assertEqual(bytes.fromhex(sent[1].split()[-1]).decode(), "hello")
        forwarder.close()


class ControllerPublishTests(unittest.TestCase):
    def test_only_bounded_controller_events_are_accepted(self):
        queue = asyncio.Queue(maxsize=2)
        protocol = ControllerPublishProtocol(queue)
        protocol.datagram_received(
            json.dumps(
                {
                    "kind": "map_prepare",
                    "payload": {
                        "map_key": "Tester/maps/Race-v1.aamap.xml",
                        "activate_at_ns": time.time_ns(),
                    },
                }
            ).encode(),
            None,
        )
        self.assertEqual(queue.get_nowait()[0], "map_prepare")
        protocol.datagram_received(
            json.dumps(
                {
                    "kind": "records_delta",
                    "payload": {
                        "operation": "ack",
                        "event_ids": ["a" * 64],
                    },
                }
            ).encode(),
            None,
        )
        self.assertEqual(queue.get_nowait()[0], "records_delta")
        protocol.datagram_received(
            json.dumps(
                {
                    "kind": "round_sync",
                    "payload": {
                        "action": "ready",
                        "map_key": "Tester/maps/Race-v1.aamap.xml",
                    },
                }
            ).encode(),
            None,
        )
        self.assertEqual(queue.get_nowait()[0], "round_sync")
        protocol.datagram_received(
            json.dumps({"kind": "chat", "payload": {}}).encode(), None
        )
        self.assertTrue(queue.empty())


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.config = follower_config()
        self.queues = FollowerQueues(control_size=2)
        self.protocol = NetworkFollowerProtocol(self.config, KEY, self.queues)

    def datagram(self, sequence=0, kind="chat"):
        return encode_packet(
            KEY,
            cluster_id="tronner-racing",
            server_id="region-a",
            boot_id="boot-a",
            sequence=sequence,
            kind=kind,
            payload={"player_id": "alice", "message": "hello"},
            sent_ns=time.time_ns(),
        )

    def test_valid_packet_is_queued_and_replay_is_not(self):
        data = self.datagram()
        self.protocol.datagram_received(data, ("127.0.0.1", 50000))
        self.assertEqual(self.queues.control.qsize(), 1)
        self.protocol.datagram_received(data, ("127.0.0.1", 50000))
        self.assertEqual(self.queues.control.qsize(), 1)

    def test_unexpected_source_is_rejected(self):
        self.protocol.datagram_received(self.datagram(), ("127.0.0.2", 50000))
        self.assertEqual(self.queues.control.qsize(), 0)

    def test_cycle_packets_are_coalesced(self):
        first = self.datagram(sequence=1, kind="cycle")
        second = self.datagram(sequence=2, kind="cycle")
        self.protocol.datagram_received(first, ("127.0.0.1", 50000))
        self.protocol.datagram_received(second, ("127.0.0.1", 50000))
        self.assertEqual(len(self.queues.cycles), 1)
        self.assertEqual(next(iter(self.queues.cycles.values())).sequence, 2)


class FollowerQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_death_edge_survives_immediate_respawn_coalescing(self):
        queues = FollowerQueues()

        def packet(sequence, alive):
            return Packet(
                version=1,
                cluster_id="tronner-racing",
                server_id="region-a",
                boot_id="boot-a",
                sequence=sequence,
                sent_ns=time.time_ns(),
                kind="cycle",
                payload={"player_id": "alice", "alive": alive},
            )

        queues.put(packet(10, False))
        queues.put(packet(11, True))
        forwarded = await queues.take_cycles()

        self.assertEqual([item.sequence for item in forwarded], [10, 11])
        self.assertEqual(
            [item.payload["alive"] for item in forwarded], [False, True]
        )


if __name__ == "__main__":
    unittest.main()
