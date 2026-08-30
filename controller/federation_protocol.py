"""Authenticated datagram protocol for Tronner server federation.

The high-rate federation path deliberately has no dependency on Firebase or
the game controller.  Packets are small, self-contained JSON datagrams signed
with HMAC-SHA256.  A receiver can therefore discard malformed, stale, or
replayed traffic before it reaches any game-facing code.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import re
import time
from collections import OrderedDict
from typing import Any


PROTOCOL_VERSION = 1
MAX_DATAGRAM_BYTES = 16_384
DEFAULT_MAX_CLOCK_SKEW_NS = 30_000_000_000
MINIMUM_KEY_BYTES = 32
MAX_IDENTIFIER_LENGTH = 64
MAX_KIND_LENGTH = 48
SEQUENCE_MAXIMUM = (1 << 63) - 1

KNOWN_KINDS = frozenset(
    {
        "heartbeat",
        "chat",
        "command",
        "controller_message",
        "countdown_state",
        "player_event",
        "player_snapshot",
        "map_prepare",
        "map_commit",
        "round_sync",
        "cycle",
        "records_delta",
    }
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


class ProtocolError(ValueError):
    """Raised when a federation datagram is not safe to accept."""


@dataclasses.dataclass(frozen=True, slots=True)
class Packet:
    version: int
    cluster_id: str
    server_id: str
    boot_id: str
    sequence: int
    sent_ns: int
    kind: str
    payload: dict[str, Any]


def _validate_key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) < MINIMUM_KEY_BYTES:
        raise ProtocolError(
            f"federation key must contain at least {MINIMUM_KEY_BYTES} bytes"
        )
    return key


def _validate_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ProtocolError(f"invalid {label}")
    return value


def _validate_kind(value: object) -> str:
    if not isinstance(value, str) or not _KIND_RE.fullmatch(value):
        raise ProtocolError("invalid packet kind")
    if value not in KNOWN_KINDS:
        raise ProtocolError(f"unsupported packet kind: {value}")
    return value


def _validate_integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"invalid {label}")
    if value < minimum or value > maximum:
        raise ProtocolError(f"invalid {label}")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_json(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"packet is not valid JSON data: {exc}") from exc
    return text.encode("utf-8")


def _signature(key: bytes, body: dict[str, Any]) -> str:
    return hmac.new(key, _canonical_json(body), hashlib.sha256).hexdigest()


def encode_packet(
    key: bytes,
    *,
    cluster_id: str,
    server_id: str,
    boot_id: str,
    sequence: int,
    kind: str,
    payload: dict[str, Any],
    sent_ns: int | None = None,
) -> bytes:
    """Return one signed federation datagram."""

    key = _validate_key(key)
    cluster_id = _validate_identifier(cluster_id, "cluster ID")
    server_id = _validate_identifier(server_id, "server ID")
    boot_id = _validate_identifier(boot_id, "boot ID")
    sequence = _validate_integer(sequence, "sequence", 0, SEQUENCE_MAXIMUM)
    sent_ns = _validate_integer(
        time.time_ns() if sent_ns is None else sent_ns,
        "sent timestamp",
        1,
        SEQUENCE_MAXIMUM,
    )
    kind = _validate_kind(kind)
    if not isinstance(payload, dict):
        raise ProtocolError("packet payload must be an object")

    body = {
        "boot_id": boot_id,
        "cluster_id": cluster_id,
        "kind": kind,
        "payload": payload,
        "sent_ns": sent_ns,
        "sequence": sequence,
        "server_id": server_id,
        "version": PROTOCOL_VERSION,
    }
    packet = dict(body)
    packet["signature"] = _signature(key, body)
    encoded = _canonical_json(packet)
    if len(encoded) > MAX_DATAGRAM_BYTES:
        raise ProtocolError(
            f"packet exceeds {MAX_DATAGRAM_BYTES}-byte datagram limit"
        )
    return encoded


def decode_packet(
    data: bytes,
    key: bytes,
    *,
    expected_cluster_id: str,
    expected_server_id: str | None = None,
    now_ns: int | None = None,
    max_clock_skew_ns: int = DEFAULT_MAX_CLOCK_SKEW_NS,
) -> Packet:
    """Authenticate and validate one federation datagram."""

    key = _validate_key(key)
    expected_cluster_id = _validate_identifier(
        expected_cluster_id, "expected cluster ID"
    )
    if expected_server_id is not None:
        expected_server_id = _validate_identifier(
            expected_server_id, "expected server ID"
        )
    if not isinstance(data, bytes):
        raise ProtocolError("datagram must be bytes")
    if not data or len(data) > MAX_DATAGRAM_BYTES:
        raise ProtocolError("invalid datagram size")
    try:
        decoded = json.loads(
            data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except ProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("datagram is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("packet must be an object")

    required = {
        "version",
        "cluster_id",
        "server_id",
        "boot_id",
        "sequence",
        "sent_ns",
        "kind",
        "payload",
        "signature",
    }
    if set(decoded) != required:
        raise ProtocolError("packet fields do not match protocol schema")

    signature = decoded.pop("signature")
    if (
        not isinstance(signature, str)
        or len(signature) != 64
        or any(character not in "0123456789abcdef" for character in signature)
    ):
        raise ProtocolError("invalid packet signature encoding")
    expected_signature = _signature(key, decoded)
    if not hmac.compare_digest(signature, expected_signature):
        raise ProtocolError("packet signature mismatch")

    version = _validate_integer(decoded["version"], "version", 1, 1)
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {version}")
    cluster_id = _validate_identifier(decoded["cluster_id"], "cluster ID")
    if cluster_id != expected_cluster_id:
        raise ProtocolError("packet belongs to another cluster")
    server_id = _validate_identifier(decoded["server_id"], "server ID")
    if expected_server_id is not None and server_id != expected_server_id:
        raise ProtocolError("packet came from an unexpected server")
    boot_id = _validate_identifier(decoded["boot_id"], "boot ID")
    sequence = _validate_integer(
        decoded["sequence"], "sequence", 0, SEQUENCE_MAXIMUM
    )
    sent_ns = _validate_integer(
        decoded["sent_ns"], "sent timestamp", 1, SEQUENCE_MAXIMUM
    )
    kind = _validate_kind(decoded["kind"])
    payload = decoded["payload"]
    if not isinstance(payload, dict):
        raise ProtocolError("packet payload must be an object")
    if isinstance(max_clock_skew_ns, bool) or not isinstance(max_clock_skew_ns, int):
        raise ProtocolError("invalid maximum clock skew")
    if max_clock_skew_ns < 0:
        raise ProtocolError("invalid maximum clock skew")
    effective_now_ns = time.time_ns() if now_ns is None else now_ns
    effective_now_ns = _validate_integer(
        effective_now_ns, "current timestamp", 1, SEQUENCE_MAXIMUM
    )
    if abs(effective_now_ns - sent_ns) > max_clock_skew_ns:
        raise ProtocolError("packet timestamp is outside the accepted clock window")

    return Packet(
        version=version,
        cluster_id=cluster_id,
        server_id=server_id,
        boot_id=boot_id,
        sequence=sequence,
        sent_ns=sent_ns,
        kind=kind,
        payload=payload,
    )


class ReplayWindow:
    """Bounded replay protection that tolerates limited UDP reordering."""

    def __init__(self, width: int = 128):
        if isinstance(width, bool) or not isinstance(width, int) or width < 8:
            raise ValueError("replay window width must be an integer of at least 8")
        self.width = width
        self.highest = -1
        self.bitmap = 0
        self._mask = (1 << width) - 1

    def accept(self, sequence: int) -> bool:
        sequence = _validate_integer(sequence, "sequence", 0, SEQUENCE_MAXIMUM)
        if self.highest < 0:
            self.highest = sequence
            self.bitmap = 1
            return True
        if sequence > self.highest:
            shift = sequence - self.highest
            self.bitmap = (
                1 if shift >= self.width else ((self.bitmap << shift) | 1) & self._mask
            )
            self.highest = sequence
            return True
        distance = self.highest - sequence
        if distance >= self.width:
            return False
        bit = 1 << distance
        if self.bitmap & bit:
            return False
        self.bitmap |= bit
        return True


class ReplayProtector:
    """Maintain bounded replay windows for recent server boot identities."""

    def __init__(self, width: int = 128, maximum_boots: int = 16):
        if (
            isinstance(maximum_boots, bool)
            or not isinstance(maximum_boots, int)
            or maximum_boots < 1
        ):
            raise ValueError("maximum_boots must be a positive integer")
        self.width = width
        self.maximum_boots = maximum_boots
        self._windows: OrderedDict[tuple[str, str], ReplayWindow] = OrderedDict()

    def accept(self, packet: Packet) -> bool:
        key = (packet.server_id, packet.boot_id)
        window = self._windows.get(key)
        if window is None:
            window = ReplayWindow(self.width)
            self._windows[key] = window
            while len(self._windows) > self.maximum_boots:
                self._windows.popitem(last=False)
        else:
            self._windows.move_to_end(key)
        return window.accept(packet.sequence)
