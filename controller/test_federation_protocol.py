import json
import unittest

from federation_protocol import (
    MAX_DATAGRAM_BYTES,
    ProtocolError,
    ReplayProtector,
    ReplayWindow,
    decode_packet,
    encode_packet,
)


KEY = b"k" * 32
NOW = 1_800_000_000_000_000_000


def packet(**overrides):
    values = {
        "cluster_id": "tronner-racing",
        "server_id": "region-a",
        "boot_id": "boot-123",
        "sequence": 7,
        "kind": "chat",
        "payload": {"player": "Alice", "message": "hello"},
        "sent_ns": NOW,
    }
    values.update(overrides)
    return encode_packet(KEY, **values)


class ProtocolTests(unittest.TestCase):
    def test_round_trip(self):
        decoded = decode_packet(
            packet(),
            KEY,
            expected_cluster_id="tronner-racing",
            expected_server_id="region-a",
            now_ns=NOW,
        )
        self.assertEqual(decoded.kind, "chat")
        self.assertEqual(decoded.sequence, 7)
        self.assertEqual(decoded.payload["message"], "hello")

    def test_tampering_is_rejected(self):
        raw = json.loads(packet())
        raw["payload"]["message"] = "changed"
        with self.assertRaisesRegex(ProtocolError, "signature mismatch"):
            decode_packet(
                json.dumps(raw).encode(),
                KEY,
                expected_cluster_id="tronner-racing",
                now_ns=NOW,
            )

    def test_wrong_key_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "signature mismatch"):
            decode_packet(
                packet(),
                b"x" * 32,
                expected_cluster_id="tronner-racing",
                now_ns=NOW,
            )

    def test_wrong_cluster_and_server_are_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "another cluster"):
            decode_packet(
                packet(), KEY, expected_cluster_id="other", now_ns=NOW
            )
        with self.assertRaisesRegex(ProtocolError, "unexpected server"):
            decode_packet(
                packet(),
                KEY,
                expected_cluster_id="tronner-racing",
                expected_server_id="region-b",
                now_ns=NOW,
            )

    def test_stale_and_future_packets_are_rejected(self):
        for sent_ns in (NOW - 31_000_000_000, NOW + 31_000_000_000):
            with self.assertRaisesRegex(ProtocolError, "clock window"):
                decode_packet(
                    packet(sent_ns=sent_ns),
                    KEY,
                    expected_cluster_id="tronner-racing",
                    now_ns=NOW,
                )

    def test_nan_payload_is_rejected_before_encoding(self):
        with self.assertRaisesRegex(ProtocolError, "valid JSON"):
            packet(payload={"position": float("nan")})

    def test_unknown_kind_and_weak_key_are_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "unsupported"):
            packet(kind="shell_command")
        with self.assertRaisesRegex(ProtocolError, "at least 32"):
            encode_packet(
                b"weak",
                cluster_id="tronner-racing",
                server_id="region-a",
                boot_id="boot-1",
                sequence=0,
                kind="heartbeat",
                payload={},
                sent_ns=NOW,
            )

    def test_oversized_payload_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "datagram limit"):
            packet(payload={"value": "x" * MAX_DATAGRAM_BYTES})

    def test_duplicate_json_keys_are_rejected(self):
        raw = (
            b'{"version":1,"version":1,"cluster_id":"tronner-racing",'
            b'"server_id":"region-a","boot_id":"boot-1","sequence":0,'
            b'"sent_ns":1800000000000000000,"kind":"heartbeat",'
            b'"payload":{},"signature":"' + b"0" * 64 + b'"}'
        )
        with self.assertRaisesRegex(ProtocolError, "duplicate JSON key"):
            decode_packet(raw, KEY, expected_cluster_id="tronner-racing", now_ns=NOW)

    def test_version_two_preserves_origin_through_an_authenticated_hub(self):
        encoded = packet(
            server_id="region-a",
            origin_server_id="region-b",
            destination_server_id="region-c",
        )
        decoded = decode_packet(
            encoded,
            KEY,
            expected_cluster_id="tronner-racing",
            expected_server_id="region-a",
            expected_destination_server_id="region-c",
            allowed_origin_server_ids={"region-a", "region-b", "region-c"},
            now_ns=NOW,
        )
        self.assertEqual(decoded.version, 2)
        self.assertEqual(decoded.sender_server_id, "region-a")
        self.assertEqual(decoded.server_id, "region-b")
        self.assertEqual(decoded.destination_server_id, "region-c")

    def test_version_two_rejects_wrong_destination_and_unknown_origin(self):
        encoded = packet(
            origin_server_id="region-b",
            destination_server_id="region-c",
        )
        with self.assertRaisesRegex(ProtocolError, "another server"):
            decode_packet(
                encoded,
                KEY,
                expected_cluster_id="tronner-racing",
                expected_server_id="region-a",
                expected_destination_server_id="region-b",
                allowed_origin_server_ids={"region-a", "region-b", "region-c"},
                now_ns=NOW,
            )
        with self.assertRaisesRegex(ProtocolError, "unexpected origin"):
            decode_packet(
                encoded,
                KEY,
                expected_cluster_id="tronner-racing",
                expected_server_id="region-a",
                expected_destination_server_id="region-c",
                allowed_origin_server_ids={"region-a", "region-c"},
                now_ns=NOW,
            )


class ReplayWindowTests(unittest.TestCase):
    def test_duplicates_and_old_packets_are_rejected(self):
        window = ReplayWindow(width=8)
        self.assertTrue(window.accept(10))
        self.assertTrue(window.accept(12))
        self.assertTrue(window.accept(11))
        self.assertFalse(window.accept(11))
        self.assertTrue(window.accept(5))
        self.assertFalse(window.accept(4))

    def test_large_forward_jump_resets_window(self):
        window = ReplayWindow(width=8)
        self.assertTrue(window.accept(1))
        self.assertTrue(window.accept(100))
        self.assertFalse(window.accept(1))

    def test_protector_separates_boots(self):
        protector = ReplayProtector(width=8, maximum_boots=2)
        first = decode_packet(
            packet(sequence=0, boot_id="boot-a"),
            KEY,
            expected_cluster_id="tronner-racing",
            now_ns=NOW,
        )
        second = decode_packet(
            packet(sequence=0, boot_id="boot-b"),
            KEY,
            expected_cluster_id="tronner-racing",
            now_ns=NOW,
        )
        self.assertTrue(protector.accept(first))
        self.assertFalse(protector.accept(first))
        self.assertTrue(protector.accept(second))


if __name__ == "__main__":
    unittest.main()
