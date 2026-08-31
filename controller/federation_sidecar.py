#!/usr/bin/env python3
"""One-way and bidirectional transport sidecar for Tronner federation.

The publisher tails the existing ladderlog and receives high-rate cycle state
from the engine over a local Unix datagram socket.  The follower authenticates
network datagrams and forwards already-normalized events to separate local
controller and engine sockets.  No network or Firebase operation occurs on the
game engine's thread.
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import contextlib
import dataclasses
import ipaddress
import json
import logging
import math
import os
import re
import signal
import socket
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from federation_protocol import (
    DEFAULT_MAX_CLOCK_SKEW_NS,
    Packet,
    ProtocolError,
    ReplayProtector,
    decode_packet,
    encode_packet,
)


LOG = logging.getLogger("TronnerFederation")
DEFAULT_PORT = 4540
MAX_CHAT_CHARACTERS = 512
MAX_PLAYER_NAME_CHARACTERS = 128
MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024
MAX_LOCAL_EVENT_BYTES = 16_384
PLAYER_TOKEN_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,128}$")
MAX_FEDERATION_PEERS = 15
ENGINE_METADATA_REFRESH_NS = 2_000_000_000
FEDERATION_HEALTH_FILE = Path("/run/tronner-federation/health.json")


class ConfigurationError(ValueError):
    pass


def _identifier(value: object, label: str) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", text):
        raise ConfigurationError(f"invalid {label}")
    return text


def _port(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"invalid {label}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid {label}") from exc
    if number < 1 or number > 65535:
        raise ConfigurationError(f"invalid {label}")
    return number


def _positive_float(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid {label}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ConfigurationError(f"invalid {label}")
    return number


def _ipv4_literal(value: object, label: str) -> str:
    text = str(value).strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError as exc:
        raise ConfigurationError(f"{label} must be a literal IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise ConfigurationError(f"{label} must be a literal IPv4 address")
    return str(address)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclasses.dataclass(frozen=True, slots=True)
class FederationPeer:
    server_id: str
    region_label: str
    host: str
    port: int
    expected_ip: str
    publish_key_file: Path
    receive_key_file: Path


@dataclasses.dataclass(frozen=True, slots=True)
class FederationConfig:
    cluster_id: str
    server_id: str
    mode: str
    region_label: str
    peer_host: str | None
    peer_port: int
    listen_host: str | None
    listen_port: int
    expected_server_id: str | None
    expected_peer_ip: str | None
    publish_key_file: Path | None
    receive_key_file: Path | None
    ladderlog: Path | None
    engine_export_socket: Path | None
    controller_publish_socket: Path | None
    controller_import_socket: Path | None
    engine_import_socket: Path | None
    heartbeat_seconds: float
    game_text_encoding: str
    maximum_clock_skew_ns: int
    protocol_version: int
    role: str
    leader_server_id: str
    members: tuple[tuple[str, str], ...]
    peers: tuple[FederationPeer, ...]

    @property
    def publishes(self) -> bool:
        return self.mode in {"publish", "both"}

    @property
    def follows(self) -> bool:
        return self.mode in {"follow", "both"}

    @property
    def multi_peer(self) -> bool:
        return self.protocol_version == 2

    @property
    def member_server_ids(self) -> frozenset[str]:
        return frozenset(server_id for server_id, _ in self.members)

    def region_for(self, server_id: str) -> str:
        return dict(self.members).get(server_id, server_id[:16])

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FederationConfig":
        if not isinstance(raw, dict):
            raise ConfigurationError("federation config must be an object")
        protocol_version_raw = raw.get("protocol_version", 1)
        if isinstance(protocol_version_raw, bool):
            raise ConfigurationError("invalid protocol version")
        try:
            protocol_version = int(protocol_version_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("invalid protocol version") from exc
        if protocol_version not in {1, 2}:
            raise ConfigurationError("protocol version must be 1 or 2")

        mode = str(raw.get("mode", "")).strip().casefold()
        if mode not in {"publish", "follow", "both"}:
            raise ConfigurationError("mode must be publish, follow, or both")
        cluster_id = _identifier(raw.get("cluster_id", ""), "cluster ID")
        server_id = _identifier(raw.get("server_id", ""), "server ID")
        region_label = str(raw.get("region_label", server_id)).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}", region_label):
            raise ConfigurationError("invalid region label")

        peer_host_raw = str(raw.get("peer_host", "")).strip()
        listen_host_raw = str(raw.get("listen_host", "")).strip()
        expected_peer_ip_raw = str(raw.get("expected_peer_ip", "")).strip()
        peer_host = _ipv4_literal(peer_host_raw, "peer host") if peer_host_raw else None
        listen_host = (
            _ipv4_literal(listen_host_raw, "listen host") if listen_host_raw else None
        )
        expected_peer_ip = (
            _ipv4_literal(expected_peer_ip_raw, "expected peer IP")
            if expected_peer_ip_raw
            else None
        )
        expected_server_id_raw = str(raw.get("expected_server_id", "")).strip()
        expected_server_id = (
            _identifier(expected_server_id_raw, "expected server ID")
            if expected_server_id_raw
            else None
        )
        publish_key_file = (
            Path(str(raw["publish_key_file"]))
            if raw.get("publish_key_file")
            else None
        )
        receive_key_file = (
            Path(str(raw["receive_key_file"]))
            if raw.get("receive_key_file")
            else None
        )
        ladderlog = Path(str(raw["ladderlog"])) if raw.get("ladderlog") else None
        engine_export_socket = (
            Path(str(raw["engine_export_socket"]))
            if raw.get("engine_export_socket")
            else None
        )
        controller_publish_socket = (
            Path(str(raw["controller_publish_socket"]))
            if raw.get("controller_publish_socket")
            else None
        )
        controller_import_socket = (
            Path(str(raw["controller_import_socket"]))
            if raw.get("controller_import_socket")
            else None
        )
        engine_import_socket = (
            Path(str(raw["engine_import_socket"]))
            if raw.get("engine_import_socket")
            else None
        )
        heartbeat_seconds = _positive_float(
            raw.get("heartbeat_seconds", 2.0), "heartbeat interval"
        )
        maximum_clock_skew_seconds = _positive_float(
            raw.get(
                "maximum_clock_skew_seconds",
                DEFAULT_MAX_CLOCK_SKEW_NS / 1_000_000_000,
            ),
            "maximum clock skew",
        )

        role = str(raw.get("role", "")).strip().casefold()
        leader_server_id = str(raw.get("leader_server_id", "")).strip()
        members: tuple[tuple[str, str], ...] = ()
        peers: tuple[FederationPeer, ...] = ()
        if protocol_version == 2:
            if mode != "both":
                raise ConfigurationError("protocol version 2 requires mode=both")
            if role not in {"leader", "follower"}:
                raise ConfigurationError(
                    "protocol version 2 role must be leader or follower"
                )
            leader_server_id = _identifier(leader_server_id, "leader server ID")
            if (role == "leader") != (server_id == leader_server_id):
                raise ConfigurationError("role does not match leader server ID")
            raw_members = raw.get("members")
            if not isinstance(raw_members, dict) or not 2 <= len(raw_members) <= 16:
                raise ConfigurationError("members must contain 2..16 servers")
            member_items: list[tuple[str, str]] = []
            for raw_server_id, raw_region in raw_members.items():
                member_id = _identifier(raw_server_id, "member server ID")
                member_region = str(raw_region).strip()
                if not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}", member_region
                ):
                    raise ConfigurationError("invalid member region label")
                member_items.append((member_id, member_region))
            member_map = dict(member_items)
            if server_id not in member_map or leader_server_id not in member_map:
                raise ConfigurationError("members must include this node and the leader")
            if member_map[server_id] != region_label:
                raise ConfigurationError("local member region does not match region_label")

            raw_peers = raw.get("peers")
            if (
                not isinstance(raw_peers, list)
                or not raw_peers
                or len(raw_peers) > MAX_FEDERATION_PEERS
            ):
                raise ConfigurationError("peers must contain 1..15 entries")
            peer_items: list[FederationPeer] = []
            seen_peer_ids: set[str] = set()
            seen_key_paths: set[Path] = set()
            for index, raw_peer in enumerate(raw_peers):
                if not isinstance(raw_peer, dict):
                    raise ConfigurationError(f"peer {index} must be an object")
                peer_id = _identifier(raw_peer.get("server_id", ""), "peer server ID")
                if (
                    peer_id == server_id
                    or peer_id in seen_peer_ids
                    or peer_id not in member_map
                ):
                    raise ConfigurationError("invalid or duplicate peer server ID")
                peer_region = str(raw_peer.get("region_label", "")).strip()
                if peer_region != member_map[peer_id]:
                    raise ConfigurationError("peer region does not match members")
                peer_publish = Path(str(raw_peer.get("publish_key_file", "")))
                peer_receive = Path(str(raw_peer.get("receive_key_file", "")))
                if (
                    not str(peer_publish)
                    or not str(peer_receive)
                    or peer_publish == peer_receive
                    or peer_publish in seen_key_paths
                    or peer_receive in seen_key_paths
                ):
                    raise ConfigurationError("peer key paths must be unique")
                seen_key_paths.update((peer_publish, peer_receive))
                seen_peer_ids.add(peer_id)
                peer_items.append(
                    FederationPeer(
                        server_id=peer_id,
                        region_label=peer_region,
                        host=_ipv4_literal(raw_peer.get("host", ""), "peer host"),
                        port=_port(raw_peer.get("port", DEFAULT_PORT), "peer port"),
                        expected_ip=_ipv4_literal(
                            raw_peer.get("expected_ip", ""), "expected peer IP"
                        ),
                        publish_key_file=peer_publish,
                        receive_key_file=peer_receive,
                    )
                )
            expected_peers = (
                set(member_map) - {server_id}
                if role == "leader"
                else {leader_server_id}
            )
            if seen_peer_ids != expected_peers:
                raise ConfigurationError(
                    "leader must peer with every follower; followers must peer only with leader"
                )
            if not listen_host:
                raise ConfigurationError("protocol version 2 requires listen_host")
            if (
                controller_publish_socket is None
                or controller_import_socket is None
                or engine_import_socket is None
            ):
                raise ConfigurationError(
                    "protocol version 2 requires controller publish/import sockets "
                    "and an engine import socket"
                )
            if ladderlog is None or engine_export_socket is None:
                raise ConfigurationError("protocol version 2 requires publisher inputs")
            members = tuple(member_items)
            peers = tuple(peer_items)
        else:
            role = "leader" if mode == "publish" else "follower"
            if mode == "both":
                role = str(raw.get("role", "leader")).strip().casefold()
                if role not in {"leader", "follower"}:
                    raise ConfigurationError("legacy role must be leader or follower")
            leader_server_id = (
                server_id if role == "leader" else (expected_server_id or "")
            )
            member_items = [(server_id, region_label)]
            if expected_server_id:
                member_items.append((expected_server_id, expected_server_id[:16]))
            members = tuple(member_items)

        if protocol_version == 1 and mode in {"publish", "both"}:
            if not peer_host:
                raise ConfigurationError("publisher requires peer_host")
            if publish_key_file is None:
                raise ConfigurationError("publisher requires publish_key_file")
            if ladderlog is None:
                raise ConfigurationError("publisher requires ladderlog")
            if engine_export_socket is None:
                raise ConfigurationError("publisher requires engine_export_socket")
        if protocol_version == 1 and mode in {"follow", "both"}:
            if not listen_host:
                raise ConfigurationError("follower requires listen_host")
            if receive_key_file is None:
                raise ConfigurationError("follower requires receive_key_file")
            if expected_server_id is None:
                raise ConfigurationError("follower requires expected_server_id")
            if expected_peer_ip is None:
                raise ConfigurationError("follower requires expected_peer_ip")
            if controller_import_socket is None:
                raise ConfigurationError(
                    "follower requires controller_import_socket"
                )
            if engine_import_socket is None:
                raise ConfigurationError("follower requires engine_import_socket")

        encoding = str(raw.get("game_text_encoding", "iso8859-1")).strip()
        try:
            encoding = codecs.lookup(encoding).name
        except LookupError as exc:
            raise ConfigurationError("invalid game text encoding") from exc
        return cls(
            cluster_id=cluster_id,
            server_id=server_id,
            mode=mode,
            region_label=region_label,
            peer_host=peer_host,
            peer_port=_port(raw.get("peer_port", DEFAULT_PORT), "peer port"),
            listen_host=listen_host,
            listen_port=_port(raw.get("listen_port", DEFAULT_PORT), "listen port"),
            expected_server_id=expected_server_id,
            expected_peer_ip=expected_peer_ip,
            publish_key_file=publish_key_file,
            receive_key_file=receive_key_file,
            ladderlog=ladderlog,
            engine_export_socket=engine_export_socket,
            controller_publish_socket=controller_publish_socket,
            controller_import_socket=controller_import_socket,
            engine_import_socket=engine_import_socket,
            heartbeat_seconds=heartbeat_seconds,
            game_text_encoding=encoding,
            maximum_clock_skew_ns=int(maximum_clock_skew_seconds * 1_000_000_000),
            protocol_version=protocol_version,
            role=role,
            leader_server_id=leader_server_id,
            members=members,
            peers=peers,
        )


def load_config(path: Path) -> FederationConfig:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"unable to load config {path}: {exc}") from exc
    return FederationConfig.from_dict(raw)


def read_key(path: Path) -> bytes:
    try:
        if path.is_symlink():
            raise ConfigurationError("federation key must not be a symbolic link")
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigurationError("federation key must be a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o007:
            raise ConfigurationError("federation key must not be accessible by others")
        raw_value = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"unable to read federation key {path}: {exc}") from exc
    if len(raw_value) > 4096:
        raise ConfigurationError("federation key file is unexpectedly large")
    value = raw_value.strip()
    if len(value) == 64:
        with contextlib.suppress(ValueError):
            decoded = bytes.fromhex(value.decode("ascii"))
            if len(decoded) == 32:
                return decoded
    if len(value) < 32:
        raise ConfigurationError("federation key must contain at least 32 bytes")
    return value


def _bounded_text(value: object, maximum: int) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").strip()[:maximum]


def _player_token(value: object) -> str | None:
    token = str(value)
    return token if PLAYER_TOKEN_RE.fullmatch(token) else None


@dataclasses.dataclass(slots=True)
class PlayerProjection:
    player_id: str
    display_name: str
    colored_name: str = ""
    authenticated_name: str = ""
    rgb: tuple[int, int, int] = (15, 15, 15)
    ping: float = 0.0
    active: bool = False
    alive: bool = False
    connected: bool = True

    def payload(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "display_name": self.display_name,
            "colored_name": self.colored_name,
            "authenticated_name": self.authenticated_name,
            "rgb": list(self.rgb),
            "ping": self.ping,
            "active": self.active,
            "alive": self.alive,
            "connected": self.connected,
        }


class LadderlogProjection:
    """Normalize privacy-safe federation events from ladderlog lines."""

    def __init__(self):
        self.players: dict[str, PlayerProjection] = {}
        self.online_snapshot_player_ids: set[str] = set()
        self.authoritative_online_player_ids: set[str] = set()
        self.current_map: str | None = None
        self.size_factor: float | None = None
        self.round_started_at: str | None = None

    def player(self, player_id: str, display_name: str | None = None) -> PlayerProjection:
        item = self.players.get(player_id.casefold())
        if item is None:
            item = PlayerProjection(
                player_id=player_id,
                display_name=display_name or player_id,
            )
            self.players[player_id.casefold()] = item
        elif display_name:
            item.display_name = display_name
        return item

    def snapshot_payload(self) -> dict[str, Any]:
        players = [
            player.payload()
            for player in self.players.values()
            if player.connected
        ]
        players.sort(key=lambda item: (item["display_name"].casefold(), item["player_id"]))
        return {
            "players": players,
            "current_map": self.current_map,
            "size_factor": self.size_factor,
            "round_active": self.round_started_at is not None,
            "round_started_at": self.round_started_at,
        }

    def consume(self, line: str) -> list[tuple[str, dict[str, Any]]]:
        line = line.rstrip("\r\n")
        if not line:
            return []
        event, _, payload = line.partition(" ")
        if event == "CHAT":
            player_id, separator, message = payload.partition(" ")
            token = _player_token(player_id)
            message = _bounded_text(message, MAX_CHAT_CHARACTERS)
            if not separator or token is None or not message:
                return []
            player = self.player(token)
            return [
                (
                    "chat",
                    {
                        "player_id": player.player_id,
                        "display_name": player.display_name,
                        "colored_name": player.colored_name,
                        "rgb": list(player.rgb),
                        "ping": player.ping,
                        "active": player.active,
                        "alive": player.alive,
                        "message": message,
                    },
                )
            ]

        if event in {
            "PLAYER_ENTERED_GRID",
            "PLAYER_ENTERED_SPECTATOR",
        }:
            parts = payload.split(maxsplit=2)
            if len(parts) < 2:
                return []
            token = _player_token(parts[0])
            if token is None:
                return []
            # The middle field is the network address. It is deliberately not
            # included in any federation event.
            display_name = _bounded_text(
                parts[2] if len(parts) > 2 else token,
                MAX_PLAYER_NAME_CHARACTERS,
            )
            player = self.player(token, display_name)
            player.connected = True
            self.authoritative_online_player_ids.add(token.casefold())
            player.active = event == "PLAYER_ENTERED_GRID"
            return [("player_event", {"action": "entered", **player.payload()})]

        if event in {"PLAYER_LEAVES_SPECTATORS", "PLAYER_JOINS_SPECTATORS"}:
            parts = payload.split(maxsplit=1)
            token = _player_token(parts[0]) if parts else None
            if token is None:
                return []
            display_name = (
                _bounded_text(parts[1], MAX_PLAYER_NAME_CHARACTERS)
                if len(parts) > 1
                else token
            )
            player = self.player(token, display_name)
            player.connected = True
            self.authoritative_online_player_ids.add(token.casefold())
            player.active = event == "PLAYER_LEAVES_SPECTATORS"
            return [("player_event", {"action": "activity", **player.payload()})]

        if event == "PLAYER_LEFT":
            parts = payload.split(maxsplit=2)
            token = _player_token(parts[0]) if parts else None
            if token is None:
                return []
            player = self.player(token)
            player.connected = False
            player.active = False
            player.alive = False
            self.authoritative_online_player_ids.discard(token.casefold())
            return [("player_event", {"action": "left", **player.payload()})]

        if event == "PLAYER_LOGIN":
            parts = payload.split(maxsplit=1)
            token = _player_token(parts[0]) if parts else None
            if token is None or len(parts) < 2:
                return []
            player = self.player(token)
            player.authenticated_name = _bounded_text(
                parts[1], MAX_PLAYER_NAME_CHARACTERS
            )
            return [("player_event", {"action": "login", **player.payload()})]

        if event == "PLAYER_LOGOUT":
            token = _player_token(payload.split(maxsplit=1)[0]) if payload else None
            if token is None:
                return []
            player = self.player(token)
            player.authenticated_name = ""
            return [("player_event", {"action": "logout", **player.payload()})]

        if event == "PLAYER_COLORED_NAME":
            player_id, separator, colored_name = payload.partition(" ")
            token = _player_token(player_id)
            if token is None or not separator:
                return []
            player = self.player(token)
            player.colored_name = _bounded_text(
                colored_name, MAX_PLAYER_NAME_CHARACTERS * 2
            )
            return [("player_event", {"action": "color", **player.payload()})]

        if event == "ONLINE_PLAYER":
            parts = payload.split()
            token = _player_token(parts[0]) if parts else None
            if token is None or len(parts) < 5:
                return []
            try:
                # Native ladderlog values occasionally arrive just outside the
                # documented 0..15 range (including wrapped negatives). Match
                # the controller's established rendering behavior by clamping
                # instead of discarding all color metadata for that player.
                rgb = tuple(max(0, min(15, int(value))) for value in parts[2:5])
            except ValueError:
                return []
            player = self.player(token)
            player.connected = True
            self.online_snapshot_player_ids.add(token.casefold())
            player.rgb = rgb
            if len(parts) >= 8:
                try:
                    ping = float(parts[7])
                    if math.isfinite(ping):
                        player.ping = max(0.0, min(30.0, ping))
                except ValueError:
                    pass
            return []

        if event == "ONLINE_PLAYERS_COUNT":
            # ONLINE_PLAYER lines immediately preceding this marker are the
            # engine's authoritative current-player snapshot. Reconciling at
            # the marker prevents a bootstrap from resurrecting a historical
            # session whose PLAYER_LEFT was lost to rotation or an ungraceful
            # exit. Federation ghosts are absent from ONLINE_PLAYER ladderlog
            # output, so they cannot be echoed back to their origin.
            fields = payload.split()
            try:
                local_player_count = int(fields[0])
            except (IndexError, ValueError):
                return []

            # The count/alive/dead group is emitted every second, while the
            # ONLINE_PLAYER detail rows are emitted only for a fresh native
            # snapshot. Do not interpret a later count marker with no detail
            # rows as an empty snapshot or every real player disappears after
            # one second.
            if self.online_snapshot_player_ids:
                current = self.online_snapshot_player_ids
                self.authoritative_online_player_ids = set(current)
                for key, player in self.players.items():
                    if key not in current:
                        player.connected = False
                        player.active = False
                        player.alive = False
                self.online_snapshot_player_ids = set()
            elif local_player_count == 0:
                for key in self.authoritative_online_player_ids:
                    player = self.players.get(key)
                    if player is not None:
                        player.connected = False
                        player.active = False
                        player.alive = False
                self.authoritative_online_player_ids.clear()
            return []

        if event in {"ONLINE_PLAYERS_ALIVE", "ONLINE_PLAYERS_DEAD"}:
            alive = event == "ONLINE_PLAYERS_ALIVE"
            for value in payload.split():
                token = _player_token(value)
                key = token.casefold() if token is not None else ""
                # The alive/dead writers include visual federation ghosts.
                # Only IDs confirmed by the preceding ONLINE_PLAYER snapshot
                # belong to this engine and may be published upstream.
                if key in self.authoritative_online_player_ids:
                    player = self.players.get(key)
                    if player is not None:
                        player.alive = alive
            return []

        if event == "COMMAND":
            parts = payload.split(maxsplit=4)
            if len(parts) < 4:
                return []
            command = parts[0].casefold()
            token = _player_token(parts[1])
            if (
                token is None
                or not re.fullmatch(r"/[a-z0-9_]{1,47}", command)
            ):
                return []
            try:
                access_level = int(parts[3])
            except ValueError:
                return []
            if access_level < 0 or access_level > 255:
                return []
            player = self.player(token)
            return [
                (
                    "command",
                    {
                        **player.payload(),
                        "command": command,
                        "access_level": access_level,
                        "arguments": _bounded_text(
                            parts[4] if len(parts) > 4 else "",
                            MAX_CHAT_CHARACTERS,
                        ),
                    },
                )
            ]

        if event == "PLAYER_RENAMED":
            parts = payload.split(maxsplit=4)
            if len(parts) < 2:
                return []
            old_token = _player_token(parts[0])
            new_token = _player_token(parts[1])
            if old_token is None or new_token is None:
                return []
            player = self.players.pop(old_token.casefold(), None) or self.player(old_token)
            player.player_id = new_token
            if len(parts) > 4:
                player.display_name = _bounded_text(
                    parts[4], MAX_PLAYER_NAME_CHARACTERS
                )
            self.players[new_token.casefold()] = player
            return [
                (
                    "player_event",
                    {
                        "action": "renamed",
                        "previous_player_id": old_token,
                        **player.payload(),
                    },
                )
            ]

        if event == "CURRENT_MAP":
            parts = payload.split(maxsplit=2)
            if len(parts) < 3:
                return []
            try:
                size_factor = float(parts[0])
            except ValueError:
                return []
            if not math.isfinite(size_factor):
                return []
            map_key = _bounded_text(parts[2], 512)
            if not map_key or "\x00" in map_key:
                return []
            if map_key != self.current_map:
                self.round_started_at = None
            self.size_factor = size_factor
            self.current_map = map_key
            return [
                (
                    "map_commit",
                    {
                        "map_key": map_key,
                        "size_factor": size_factor,
                        "observed": True,
                    },
                )
            ]

        if event == "ROUND_STARTED":
            self.round_started_at = _bounded_text(payload, 64)
            return [
                (
                    "player_event",
                    {
                        "action": "round_started",
                        "map_key": self.current_map,
                        "started_at": self.round_started_at,
                    },
                )
            ]
        if event in {"ROUND_FINISHED", "ROUND_ENDED", "NEW_ROUND"}:
            self.round_started_at = None
            return [
                (
                    "player_event",
                    {"action": event.casefold(), "map_key": self.current_map},
                )
            ]
        return []


def parse_engine_telemetry(data: bytes) -> dict[str, Any]:
    """Parse the intentionally small engine-to-sidecar line protocol."""

    if not isinstance(data, bytes) or not data or len(data) > 2048:
        raise ProtocolError("invalid engine telemetry size")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError("engine telemetry is not ASCII") from exc
    parts = text.strip().split()
    if len(parts) not in {9, 10, 11, 14} or parts[0] != "CYCLE_V1":
        raise ProtocolError("invalid engine telemetry schema")
    player_id = _player_token(parts[1])
    if player_id is None:
        raise ProtocolError("invalid engine telemetry player")
    try:
        game_time, x, y, xdir, ydir, speed = map(float, parts[2:8])
        if len(parts) >= 10:
            ping = float(parts[8])
            alive = int(parts[9])
        else:
            # Accept the previous engine schema during a coordinated upgrade;
            # the latest ladderlog snapshot supplies its fallback ping.
            ping = None
            alive = int(parts[8])
        chatting = int(parts[10]) if len(parts) in {11, 14} else None
        cycle_rgb = tuple(map(float, parts[11:14])) if len(parts) == 14 else None
    except ValueError as exc:
        raise ProtocolError("invalid engine telemetry value") from exc
    if not all(math.isfinite(value) for value in (game_time, x, y, xdir, ydir, speed)):
        raise ProtocolError("non-finite engine telemetry value")
    if alive not in {0, 1}:
        raise ProtocolError("invalid engine telemetry alive state")
    if chatting is not None and chatting not in {0, 1}:
        raise ProtocolError("invalid engine telemetry chatting state")
    if cycle_rgb is not None:
        if not all(math.isfinite(value) for value in cycle_rgb):
            raise ProtocolError("invalid engine telemetry cycle color")
        # Old clients can send color components far outside the documented
        # 0..15 player range.  The engine tolerates and renders those players,
        # so dropping their entire motion stream makes them disappear from
        # every other region.  Bound only the visual metadata to the import
        # protocol's safe range while preserving position/state delivery.
        cycle_rgb = tuple(max(0.0, min(4.0, value)) for value in cycle_rgb)
    if ping is not None and (not math.isfinite(ping) or ping < 0 or ping > 30):
        raise ProtocolError("invalid engine telemetry ping")
    payload = {
        "player_id": player_id,
        "game_time": game_time,
        "x": x,
        "y": y,
        "xdir": xdir,
        "ydir": ydir,
        "speed": speed,
        "alive": bool(alive),
        "observed_ns": time.time_ns(),
    }
    if ping is not None:
        payload["ping"] = ping
    if chatting is not None:
        payload["chatting"] = bool(chatting)
    if cycle_rgb is not None:
        payload["cycle_rgb"] = list(cycle_rgb)
    return payload


class PacketPublisher:
    def __init__(
        self,
        config: FederationConfig,
        key: bytes,
        peer: FederationPeer | None = None,
    ):
        self.config = config
        self.key = key
        self.peer = peer
        self.boot_id = uuid.uuid4().hex
        self.sequence = 0
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        if peer is not None:
            assert config.listen_host is not None
            self.socket.bind((config.listen_host, 0))

    async def send(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        origin_server_id: str | None = None,
    ) -> None:
        if self.sequence > (1 << 63) - 1:
            raise RuntimeError("federation sequence exhausted")
        packet_options: dict[str, Any] = {}
        if self.peer is not None:
            packet_options = {
                "origin_server_id": origin_server_id or self.config.server_id,
                "destination_server_id": self.peer.server_id,
            }
        data = encode_packet(
            self.key,
            cluster_id=self.config.cluster_id,
            server_id=self.config.server_id,
            boot_id=self.boot_id,
            sequence=self.sequence,
            kind=kind,
            payload=payload,
            **packet_options,
        )
        self.sequence += 1
        if self.peer is not None:
            target = (self.peer.host, self.peer.port)
        else:
            assert self.config.peer_host is not None
            target = (self.config.peer_host, self.config.peer_port)
        await asyncio.get_running_loop().sock_sendto(
            self.socket,
            data,
            target,
        )

    def close(self) -> None:
        self.socket.close()


class FanoutPublisher:
    """Publish local events and origin-preserving leader relays per peer."""

    def __init__(self, config: FederationConfig):
        self.config = config
        self.publishers = {
            peer.server_id: PacketPublisher(
                config,
                read_key(peer.publish_key_file),
                peer,
            )
            for peer in config.peers
        }

    async def _send_many(
        self,
        operations: list[tuple[str, Awaitable[None]]],
    ) -> None:
        results = await asyncio.gather(
            *(operation for _peer_id, operation in operations),
            return_exceptions=True,
        )
        for (peer_id, _operation), result in zip(operations, results, strict=True):
            if isinstance(result, BaseException):
                LOG.warning("unable to send federation datagram to %s: %s", peer_id, result)

    async def send(self, kind: str, payload: dict[str, Any]) -> None:
        await self._send_many(
            [
                (peer_id, publisher.send(kind, payload))
                for peer_id, publisher in self.publishers.items()
            ]
        )

    async def relay(self, packet: Packet) -> None:
        if not should_relay_from_follower(packet):
            return
        await self._send_many(
            [
                (
                    peer_id,
                    publisher.send(
                        packet.kind,
                        packet.payload,
                        origin_server_id=packet.server_id,
                    ),
                )
                for peer_id, publisher in self.publishers.items()
                if peer_id != packet.server_id
            ]
        )

    def close(self) -> None:
        for publisher in self.publishers.values():
            publisher.close()


def should_relay_from_follower(packet: Packet) -> bool:
    """Return whether a follower-origin packet belongs at other followers.

    Commands, observed follower maps, readiness notices, and PB snapshot
    requests terminate at the leader.  Relaying them to every edge created
    work and duplicate state without a consumer.  Presence, chat, motion and
    actual PB deltas still fan out through the hub.
    """

    if packet.kind in {"command", "map_commit"}:
        return False
    if packet.kind == "round_sync" and packet.payload.get("action") == "ready":
        return False
    if (
        packet.kind == "records_delta"
        and packet.payload.get("operation") == "snapshot_request"
    ):
        return False
    return True


class EngineTelemetryProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[dict[str, Any]]):
        self.queue = queue

    def datagram_received(self, data: bytes, address) -> None:
        try:
            payload = parse_engine_telemetry(data)
        except ProtocolError as exc:
            LOG.warning("dropping invalid local engine telemetry: %s", exc)
            return
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(payload)

    def error_received(self, exc: Exception) -> None:
        LOG.warning("engine telemetry socket error: %s", exc)


class ControllerPublishProtocol(asyncio.DatagramProtocol):
    """Accept bounded leader-control events over a local-only socket."""

    def __init__(self, queue: asyncio.Queue[tuple[str, dict[str, Any]]]):
        self.queue = queue

    def datagram_received(self, data: bytes, address) -> None:
        try:
            if not data or len(data) > MAX_LOCAL_EVENT_BYTES:
                raise ValueError("invalid event size")
            event = json.loads(data.decode("utf-8"))
            if not isinstance(event, dict) or set(event) != {"kind", "payload"}:
                raise ValueError("invalid event envelope")
            kind = event["kind"]
            payload = event["payload"]
            if kind not in {
                "map_prepare",
                "round_sync",
                "controller_message",
                "countdown_state",
                "records_delta",
            } or not isinstance(payload, dict):
                raise ValueError("invalid event kind")
            self.queue.put_nowait((kind, payload))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, asyncio.QueueFull):
            LOG.warning("dropping invalid local controller publish event")

    def error_received(self, exc: Exception) -> None:
        LOG.warning("controller publish socket error: %s", exc)


async def bind_unix_datagram(
    path: Path,
    protocol_factory: Callable[[], asyncio.DatagramProtocol],
) -> tuple[asyncio.DatagramTransport, asyncio.DatagramProtocol]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
        protocol_factory,
        family=socket.AF_UNIX,
        local_addr=str(path),
    )
    return transport, protocol


async def bootstrap_ladderlog(
    path: Path,
    projection: LadderlogProjection,
    encoding: str,
) -> None:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            start = max(0, size - MAX_BOOTSTRAP_BYTES)
            handle.seek(start)
            if start:
                handle.readline()
            for raw_line in handle:
                projection.consume(raw_line.decode(encoding, "replace"))
    except OSError as exc:
        LOG.warning("unable to bootstrap ladderlog state: %s", exc)


async def follow_ladderlog(
    path: Path,
    projection: LadderlogProjection,
    encoding: str,
    publisher: PacketPublisher,
) -> None:
    await bootstrap_ladderlog(path, projection, encoding)
    handle = None
    inode = None
    position = 0
    try:
        while True:
            try:
                stat = path.stat()
                if handle is None or inode != stat.st_ino or stat.st_size < position:
                    if handle is not None:
                        handle.close()
                    handle = path.open("rb")
                    handle.seek(0, os.SEEK_END)
                    position = handle.tell()
                    inode = stat.st_ino
                raw_line = handle.readline()
                if not raw_line:
                    await asyncio.sleep(0.05)
                    continue
                position = handle.tell()
                for kind, payload in projection.consume(
                    raw_line.decode(encoding, "replace")
                ):
                    await publisher.send(kind, payload)
            except FileNotFoundError:
                await asyncio.sleep(0.25)
            except OSError as exc:
                LOG.warning("ladderlog follow error: %s", exc)
                await asyncio.sleep(0.25)
    finally:
        if handle is not None:
            handle.close()


async def publisher_heartbeat(
    config: FederationConfig,
    projection: LadderlogProjection,
    publisher: PacketPublisher,
) -> None:
    while True:
        await publisher.send(
            "heartbeat",
            {
                "mode": config.mode,
                "region": config.region_label,
                "protocol": 1,
            },
        )
        await publisher.send("player_snapshot", projection.snapshot_payload())
        await asyncio.sleep(config.heartbeat_seconds)


async def publish_engine_telemetry(
    queue: asyncio.Queue[dict[str, Any]],
    projection: LadderlogProjection,
    publisher: PacketPublisher,
) -> None:
    while True:
        payload = await queue.get()
        player = projection.players.get(payload["player_id"].casefold())
        if player is not None:
            local_ping = payload.get("ping", player.ping)
            payload = {
                **payload,
                "display_name": player.display_name,
                "colored_name": player.colored_name,
                "authenticated_name": player.authenticated_name,
                "rgb": list(player.rgb),
                "ping": local_ping,
            }
        await publisher.send("cycle", payload)


async def publish_controller_events(
    queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    publisher: PacketPublisher,
) -> None:
    while True:
        kind, payload = await queue.get()
        await publisher.send(kind, payload)


class FollowerQueues:
    """Protect control events from high-rate cycle packet pressure."""

    def __init__(self, control_size: int = 1024):
        self.control: asyncio.Queue[Packet] = asyncio.Queue(maxsize=control_size)
        self.cycles: dict[tuple[str, str], Packet] = {}
        self.cycle_deaths: dict[tuple[str, str], Packet] = {}
        self.cycle_ready = asyncio.Event()

    def put(self, packet: Packet) -> None:
        if packet.kind == "cycle":
            player_id = str(packet.payload.get("player_id", ""))
            key = (packet.server_id, player_id)
            # Preserve a dead edge even when an immediate respawn packet lands
            # in the same event-loop batch. Position traffic may coalesce, but
            # death followed by spawn must reach the engine in that order.
            if packet.payload.get("alive") is False:
                self.cycle_deaths[key] = packet
            self.cycles[key] = packet
            self.cycle_ready.set()
            return
        try:
            self.control.put_nowait(packet)
        except asyncio.QueueFull:
            LOG.error("federation control queue is full; dropping %s", packet.kind)

    async def take_cycles(self) -> list[Packet]:
        await self.cycle_ready.wait()
        packets = list(self.cycle_deaths.values())
        packets.extend(
            packet
            for key, packet in self.cycles.items()
            if key not in self.cycle_deaths
            or self.cycle_deaths[key].sequence != packet.sequence
        )
        packets.sort(key=lambda packet: packet.sequence)
        self.cycles.clear()
        self.cycle_deaths.clear()
        self.cycle_ready.clear()
        return packets


class FederationHealth:
    """Small local status snapshot built from authenticated inbound packets."""

    def __init__(self, config: FederationConfig):
        self.config = config
        self.started_ns = time.time_ns()
        self.received: dict[str, dict[str, object]] = {}

    def observe(self, packet: Packet) -> None:
        origin = packet.server_id
        now_ns = time.time_ns()
        peer = self.received.setdefault(origin, {"kinds": {}})
        peer["received_ns"] = now_ns
        peer["sent_ns"] = packet.sent_ns
        peer["kind"] = packet.kind
        peer["sequence"] = packet.sequence
        kinds = peer["kinds"]
        assert isinstance(kinds, dict)
        kinds[packet.kind] = {
            "received_ns": now_ns,
            "sent_ns": packet.sent_ns,
            "sequence": packet.sequence,
        }

    def write(self) -> None:
        data = {
            "version": 1,
            "generated_ns": time.time_ns(),
            "started_ns": self.started_ns,
            "cluster_id": self.config.cluster_id,
            "server_id": self.config.server_id,
            "role": self.config.role,
            "members": dict(self.config.members),
            "received": self.received,
        }
        path = FEDERATION_HEALTH_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(
                    data,
                    output,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


async def write_federation_health(
    health: FederationHealth,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            health.write()
        except OSError:
            LOG.exception("unable to write federation health snapshot")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except TimeoutError:
            pass
    with contextlib.suppress(OSError):
        health.write()


class NetworkFollowerProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        config: FederationConfig,
        key: bytes,
        queues: FollowerQueues,
    ):
        self.config = config
        self.key = key
        self.queues = queues
        self.replays = ReplayProtector(width=256, maximum_boots=8)

    def datagram_received(self, data: bytes, address) -> None:
        if self.config.expected_peer_ip and address[0] != self.config.expected_peer_ip:
            LOG.warning("dropping federation datagram from unexpected address %s", address[0])
            return
        try:
            packet = decode_packet(
                data,
                self.key,
                expected_cluster_id=self.config.cluster_id,
                expected_server_id=self.config.expected_server_id,
                max_clock_skew_ns=self.config.maximum_clock_skew_ns,
            )
        except ProtocolError as exc:
            LOG.warning("dropping invalid federation datagram: %s", exc)
            return
        if not self.replays.accept(packet):
            LOG.warning(
                "dropping replayed federation datagram server=%s boot=%s sequence=%d",
                packet.server_id,
                packet.boot_id,
                packet.sequence,
            )
            return
        self.queues.put(packet)

    def error_received(self, exc: Exception) -> None:
        LOG.warning("federation network socket error: %s", exc)


class MultiPeerNetworkProtocol(asyncio.DatagramProtocol):
    """Authenticate a bounded peer registry and relay through the leader."""

    def __init__(
        self,
        config: FederationConfig,
        queues: FollowerQueues,
        publisher: FanoutPublisher,
        health: FederationHealth | None = None,
    ):
        self.config = config
        self.queues = queues
        self.publisher = publisher
        self.health = health
        self.keys = {
            peer.server_id: read_key(peer.receive_key_file)
            for peer in config.peers
        }
        self.replays = {
            peer.server_id: ReplayProtector(width=256, maximum_boots=8)
            for peer in config.peers
        }
        self.relay_tasks: set[asyncio.Task[None]] = set()

    def datagram_received(self, data: bytes, address) -> None:
        candidates = [
            peer for peer in self.config.peers if peer.expected_ip == address[0]
        ]
        if not candidates:
            LOG.warning(
                "dropping federation datagram from unexpected address %s", address[0]
            )
            return
        packet: Packet | None = None
        authenticated_peer: FederationPeer | None = None
        last_error: ProtocolError | None = None
        for peer in candidates:
            try:
                candidate = decode_packet(
                    data,
                    self.keys[peer.server_id],
                    expected_cluster_id=self.config.cluster_id,
                    expected_server_id=peer.server_id,
                    expected_destination_server_id=self.config.server_id,
                    allowed_origin_server_ids=self.config.member_server_ids,
                    max_clock_skew_ns=self.config.maximum_clock_skew_ns,
                )
            except ProtocolError as exc:
                last_error = exc
                continue
            packet = candidate
            authenticated_peer = peer
            break
        if packet is None or authenticated_peer is None:
            LOG.warning("dropping invalid federation datagram: %s", last_error)
            return
        if (
            self.config.role == "leader"
            and packet.server_id != authenticated_peer.server_id
        ):
            LOG.warning("dropping follower packet with a forged relay origin")
            return
        if not self.replays[authenticated_peer.server_id].accept(packet):
            LOG.warning(
                "dropping replayed federation datagram sender=%s boot=%s sequence=%d",
                authenticated_peer.server_id,
                packet.boot_id,
                packet.sequence,
            )
            return
        if self.health is not None:
            self.health.observe(packet)
        self.queues.put(packet)
        if self.config.role == "leader":
            task = asyncio.create_task(
                self.publisher.relay(packet),
                name=f"federation-relay-{packet.server_id}",
            )
            self.relay_tasks.add(task)
            task.add_done_callback(self._relay_finished)

    def _relay_finished(self, task: asyncio.Task[None]) -> None:
        self.relay_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOG.exception("federation relay task failed")

    def connection_lost(self, exc: Exception | None) -> None:
        for task in self.relay_tasks:
            task.cancel()

    def error_received(self, exc: Exception) -> None:
        LOG.warning("federation network socket error: %s", exc)


class LocalEventForwarder:
    def __init__(self, config: FederationConfig):
        self.config = config
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self.backpressure_warning_ns: dict[Path, int] = {}
        self.engine_colors: dict[tuple[str, str], tuple[tuple[float, ...], int]] = {}
        self.engine_flags: dict[tuple[str, str], tuple[bool, int]] = {}

    async def _send(self, path: Path, data: bytes) -> None:
        if len(data) > MAX_LOCAL_EVENT_BYTES:
            LOG.error("local federation event exceeds size limit")
            return
        try:
            # Local federation datagrams are snapshots/edges on a continuously
            # refreshed stream.  Waiting for a full consumer socket here can
            # wedge the shared forwarder indefinitely, which also prevents
            # unrelated controller traffic (including map commits) from being
            # delivered.  Make the best-effort send immediately and let a
            # later snapshot refresh any packet dropped under backpressure.
            self.socket.sendto(data, str(path))
        except BlockingIOError:
            now_ns = time.monotonic_ns()
            previous_ns = self.backpressure_warning_ns.get(path, now_ns - 5_000_000_000)
            if now_ns - previous_ns >= 5_000_000_000:
                LOG.warning("local federation consumer is backlogged: %s", path)
                self.backpressure_warning_ns[path] = now_ns
        except (FileNotFoundError, ConnectionRefusedError):
            LOG.debug("local federation consumer is unavailable: %s", path)
        except OSError as exc:
            LOG.warning("unable to forward local federation event to %s: %s", path, exc)

    async def controller(self, packet: Packet) -> None:
        assert self.config.controller_import_socket is not None
        event = {
            "version": packet.version,
            "server_id": packet.server_id,
            "boot_id": packet.boot_id,
            "sequence": packet.sequence,
            "sent_ns": packet.sent_ns,
            "kind": packet.kind,
            "payload": packet.payload,
            "region_label": self.config.region_for(packet.server_id),
        }
        data = json.dumps(
            event,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        await self._send(self.config.controller_import_socket, data)

    @staticmethod
    def _ghost_identity(server_id: str, player_id: str) -> str:
        return f"{server_id}:{player_id}".encode("utf-8").hex()

    @staticmethod
    def _ghost_metadata(
        payload: dict[str, Any],
    ) -> tuple[str, str, str, tuple[int, int, int], float]:
        player_id = str(payload.get("player_id", ""))
        display_name = _bounded_text(
            payload.get("display_name", player_id), MAX_PLAYER_NAME_CHARACTERS
        ).encode("utf-8").hex()
        colored_name = _bounded_text(
            payload.get("colored_name", ""), MAX_PLAYER_NAME_CHARACTERS * 2
        ).encode("utf-8").hex()
        authenticated_name = _bounded_text(
            payload.get("authenticated_name", ""), MAX_PLAYER_NAME_CHARACTERS
        ).encode("utf-8").hex()
        raw_rgb = payload.get("rgb", (15, 15, 15))
        try:
            rgb = tuple(int(value) for value in raw_rgb)
        except (TypeError, ValueError):
            rgb = (15, 15, 15)
        if len(rgb) != 3 or any(value < 0 or value > 15 for value in rgb):
            rgb = (15, 15, 15)
        try:
            ping = float(payload.get("ping", 0.0))
        except (TypeError, ValueError):
            ping = 0.0
        if not math.isfinite(ping) or ping < 0 or ping > 30:
            ping = 0.0
        return display_name, colored_name, authenticated_name, rgb, ping

    async def _engine_presence_payload(
        self,
        packet: Packet,
        payload: dict[str, Any],
    ) -> None:
        assert self.config.engine_import_socket is not None
        player_id = _player_token(payload.get("player_id", ""))
        if player_id is None:
            return
        display_name, colored_name, authenticated_name, rgb, ping = (
            self._ghost_metadata(payload)
        )
        identity = self._ghost_identity(packet.server_id, player_id)
        region = self.config.region_for(packet.server_id).encode("utf-8").hex()
        connected = int(bool(payload.get("connected", True)))
        line = (
            f"GHOST_V3 PRESENCE {identity} {region} {display_name} {colored_name or '-'} "
            f"{authenticated_name or '-'} "
            f"{rgb[0]} {rgb[1]} {rgb[2]} {ping:.9g} {packet.sent_ns} {connected}"
        ).encode("ascii")
        await self._send(self.config.engine_import_socket, line)

    async def engine_presence(self, packet: Packet) -> None:
        if packet.kind == "player_snapshot":
            players = packet.payload.get("players", [])
            if not isinstance(players, list) or len(players) > 256:
                return
            for payload in players:
                if isinstance(payload, dict):
                    await self._engine_presence_payload(packet, payload)
            return
        if packet.payload.get("action") == "renamed":
            previous_player_id = _player_token(
                packet.payload.get("previous_player_id", "")
            )
            if previous_player_id is not None:
                previous = {
                    "player_id": previous_player_id,
                    "display_name": previous_player_id,
                    "connected": False,
                }
                await self._engine_presence_payload(packet, previous)
        await self._engine_presence_payload(packet, packet.payload)

    async def engine_chat(self, packet: Packet) -> None:
        assert self.config.engine_import_socket is not None
        player_id = _player_token(packet.payload.get("player_id", ""))
        message = _bounded_text(
            packet.payload.get("message", ""), MAX_CHAT_CHARACTERS
        )
        if player_id is None or not message:
            return
        await self._engine_presence_payload(packet, packet.payload)
        identity = self._ghost_identity(packet.server_id, player_id)
        message_hex = message.encode("utf-8").hex()
        await self._send(
            self.config.engine_import_socket,
            f"GHOST_V1 CHAT {identity} {message_hex}".encode("ascii"),
        )

    async def engine(self, packet: Packet) -> None:
        assert self.config.engine_import_socket is not None
        payload = packet.payload
        player_id = _player_token(payload.get("player_id", ""))
        if player_id is None:
            return
        try:
            values = [
                float(payload[name])
                for name in ("game_time", "x", "y", "xdir", "ydir", "speed")
            ]
            observed_ns = int(payload["observed_ns"])
            alive = int(bool(payload["alive"]))
        except (KeyError, TypeError, ValueError):
            return
        if not all(math.isfinite(value) for value in values):
            return
        identity = self._ghost_identity(packet.server_id, player_id)
        state_key = (packet.server_id, player_id.casefold())
        refresh_ns = time.monotonic_ns()
        region = self.config.region_for(packet.server_id).encode("utf-8").hex()
        display_name, colored_name, authenticated_name, rgb, ping = (
            self._ghost_metadata(payload)
        )
        cycle_rgb = payload.get("cycle_rgb")
        if isinstance(cycle_rgb, list) and len(cycle_rgb) == 3:
            try:
                effective = tuple(float(value) for value in cycle_rgb)
            except (TypeError, ValueError):
                effective = ()
            if (
                len(effective) == 3
                and all(math.isfinite(value) for value in effective)
                and all(0 <= value <= 4 for value in effective)
            ):
                previous_color = self.engine_colors.get(state_key)
                if (
                    previous_color is None
                    or previous_color[0] != effective
                    or refresh_ns - previous_color[1] >= ENGINE_METADATA_REFRESH_NS
                ):
                    color = (
                        f"GHOST_V1 COLOR {identity} {observed_ns} "
                        + " ".join(f"{value:.17g}" for value in effective)
                    ).encode("ascii")
                    await self._send(self.config.engine_import_socket, color)
                    self.engine_colors[state_key] = (effective, refresh_ns)
        line = (
            f"GHOST_V3 STATE {identity} {region} {display_name} {colored_name or '-'} "
            f"{authenticated_name or '-'} "
            f"{rgb[0]} {rgb[1]} {rgb[2]} {ping:.9g} {observed_ns} "
            + " ".join(f"{value:.17g}" for value in values)
            + f" {alive}"
        ).encode("ascii")
        await self._send(self.config.engine_import_socket, line)
        chatting = payload.get("chatting")
        if isinstance(chatting, bool):
            previous_flags = self.engine_flags.get(state_key)
            if (
                previous_flags is None
                or previous_flags[0] != chatting
                or refresh_ns - previous_flags[1] >= ENGINE_METADATA_REFRESH_NS
            ):
                flags = (
                    f"GHOST_V1 FLAGS {identity} {observed_ns} {int(chatting)}"
                ).encode("ascii")
                await self._send(self.config.engine_import_socket, flags)
                self.engine_flags[state_key] = (chatting, refresh_ns)
        if not alive:
            self.engine_colors.pop(state_key, None)
            self.engine_flags.pop(state_key, None)

    def close(self) -> None:
        self.socket.close()


async def forward_control_events(
    queues: FollowerQueues,
    forwarder: LocalEventForwarder,
) -> None:
    while True:
        packet = await queues.control.get()
        if packet.kind == "chat":
            await forwarder.engine_chat(packet)
            continue
        if packet.kind in {"player_snapshot", "player_event"}:
            await forwarder.engine_presence(packet)
        await forwarder.controller(packet)


async def forward_cycle_events(
    queues: FollowerQueues,
    forwarder: LocalEventForwarder,
) -> None:
    while True:
        for packet in await queues.take_cycles():
            await forwarder.engine(packet)


async def run_publisher(config: FederationConfig, stop: asyncio.Event) -> None:
    assert config.publish_key_file is not None
    assert config.ladderlog is not None
    assert config.engine_export_socket is not None
    publisher = PacketPublisher(config, read_key(config.publish_key_file))
    projection = LadderlogProjection()
    telemetry_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=4096)
    telemetry_transport, _ = await bind_unix_datagram(
        config.engine_export_socket,
        lambda: EngineTelemetryProtocol(telemetry_queue),
    )
    controller_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
        maxsize=128
    )
    controller_transport: asyncio.DatagramTransport | None = None
    if config.controller_publish_socket is not None:
        controller_transport, _ = await bind_unix_datagram(
            config.controller_publish_socket,
            lambda: ControllerPublishProtocol(controller_queue),
        )
    tasks = [
        asyncio.create_task(
            follow_ladderlog(
                config.ladderlog,
                projection,
                config.game_text_encoding,
                publisher,
            ),
            name="federation-ladderlog",
        ),
        asyncio.create_task(
            publisher_heartbeat(config, projection, publisher),
            name="federation-heartbeat",
        ),
        asyncio.create_task(
            publish_engine_telemetry(telemetry_queue, projection, publisher),
            name="federation-engine-telemetry",
        ),
    ]
    if controller_transport is not None:
        tasks.append(
            asyncio.create_task(
                publish_controller_events(controller_queue, publisher),
                name="federation-controller-publisher",
            )
        )
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        telemetry_transport.close()
        with contextlib.suppress(FileNotFoundError):
            config.engine_export_socket.unlink()
        if controller_transport is not None:
            controller_transport.close()
            assert config.controller_publish_socket is not None
            with contextlib.suppress(FileNotFoundError):
                config.controller_publish_socket.unlink()
        publisher.close()


async def run_follower(config: FederationConfig, stop: asyncio.Event) -> None:
    assert config.receive_key_file is not None
    assert config.listen_host is not None
    queues = FollowerQueues()
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: NetworkFollowerProtocol(
            config,
            read_key(config.receive_key_file),
            queues,
        ),
        family=socket.AF_INET,
        local_addr=(config.listen_host, config.listen_port),
    )
    forwarder = LocalEventForwarder(config)
    tasks = [
        asyncio.create_task(
            forward_control_events(queues, forwarder),
            name="federation-control-forwarder",
        ),
        asyncio.create_task(
            forward_cycle_events(queues, forwarder),
            name="federation-cycle-forwarder",
        ),
    ]
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.close()
        forwarder.close()


async def run_multi_peer(config: FederationConfig, stop: asyncio.Event) -> None:
    assert config.listen_host is not None
    assert config.ladderlog is not None
    assert config.engine_export_socket is not None
    publisher = FanoutPublisher(config)
    health = FederationHealth(config)
    projection = LadderlogProjection()
    queues = FollowerQueues()
    telemetry_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=4096)
    controller_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
        maxsize=128
    )
    telemetry_transport, _ = await bind_unix_datagram(
        config.engine_export_socket,
        lambda: EngineTelemetryProtocol(telemetry_queue),
    )
    controller_transport: asyncio.DatagramTransport | None = None
    if config.controller_publish_socket is not None:
        controller_transport, _ = await bind_unix_datagram(
            config.controller_publish_socket,
            lambda: ControllerPublishProtocol(controller_queue),
        )
    network_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: MultiPeerNetworkProtocol(config, queues, publisher, health),
        family=socket.AF_INET,
        local_addr=(config.listen_host, config.listen_port),
    )
    forwarder = LocalEventForwarder(config)
    tasks = [
        asyncio.create_task(
            follow_ladderlog(
                config.ladderlog,
                projection,
                config.game_text_encoding,
                publisher,
            ),
            name="federation-ladderlog",
        ),
        asyncio.create_task(
            publisher_heartbeat(config, projection, publisher),
            name="federation-heartbeat",
        ),
        asyncio.create_task(
            publish_engine_telemetry(telemetry_queue, projection, publisher),
            name="federation-engine-telemetry",
        ),
        asyncio.create_task(
            forward_control_events(queues, forwarder),
            name="federation-control-forwarder",
        ),
        asyncio.create_task(
            forward_cycle_events(queues, forwarder),
            name="federation-cycle-forwarder",
        ),
        asyncio.create_task(
            write_federation_health(health, stop),
            name="federation-health-writer",
        ),
    ]
    if controller_transport is not None:
        tasks.append(
            asyncio.create_task(
                publish_controller_events(controller_queue, publisher),
                name="federation-controller-publisher",
            )
        )
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        network_transport.close()
        telemetry_transport.close()
        with contextlib.suppress(FileNotFoundError):
            config.engine_export_socket.unlink()
        if controller_transport is not None:
            controller_transport.close()
            assert config.controller_publish_socket is not None
            with contextlib.suppress(FileNotFoundError):
                config.controller_publish_socket.unlink()
        forwarder.close()
        publisher.close()


async def async_main(config: FederationConfig) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    if config.multi_peer:
        await run_multi_peer(config, stop)
        return
    tasks = []
    if config.publishes:
        tasks.append(asyncio.create_task(run_publisher(config, stop), name="publisher"))
    if config.follows:
        tasks.append(asyncio.create_task(run_follower(config, stop), name="follower"))
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tronner federation transport sidecar")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and key files, then exit",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("TRONNER_FEDERATION_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    try:
        config = load_config(args.config)
        if config.multi_peer:
            for peer in config.peers:
                read_key(peer.publish_key_file)
                read_key(peer.receive_key_file)
        else:
            if config.publishes:
                assert config.publish_key_file is not None
                read_key(config.publish_key_file)
            if config.follows:
                assert config.receive_key_file is not None
                read_key(config.receive_key_file)
    except ConfigurationError as exc:
        LOG.error("%s", exc)
        return 2
    if args.check:
        LOG.info(
            "configuration valid: mode=%s cluster=%s server=%s",
            config.mode,
            config.cluster_id,
            config.server_id,
        )
        return 0
    asyncio.run(async_main(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
