#!/usr/bin/env python3
"""Create and approve manual federation enrollment requests.

Requests contain no credential. Approval creates two directional HMAC keys in
operator-only bundles; it never edits a live node or opens a firewall.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any

from render_node import (
    ConfigurationError,
    atomic_write,
    identifier,
    ip_address,
    json_text,
    load_object,
    region_label,
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def optional_wireguard_key(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError("invalid WireGuard public key") from exc
    if len(decoded) != 32:
        raise ConfigurationError("invalid WireGuard public key")
    return text


def safe_output_file(path: Path, contents: str, mode: int) -> None:
    if path.exists():
        raise ConfigurationError(f"refusing to overwrite {path}")
    atomic_write(path, contents, mode)


def require_empty_private_directory(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise ConfigurationError(f"output directory must be empty: {path}")
    else:
        path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def create_request(args: argparse.Namespace) -> int:
    server_id = identifier(args.server_id, "server ID")
    label = region_label(args.region_label)
    overlay_address = ip_address(args.overlay_address, "overlay address")
    request = {
        "schema_version": 1,
        "request_id": uuid.uuid4().hex,
        "requested_at": utc_now(),
        "server_id": server_id,
        "region_label": label,
        "overlay_address": overlay_address,
        "wireguard_public_key": optional_wireguard_key(args.wireguard_public_key),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    safe_output_file(args.output, json_text(request), 0o644)
    print(f"wrote credential-free enrollment request to {args.output}")
    return 0


def load_request(path: Path) -> dict[str, Any]:
    request = load_object(path)
    expected = {
        "schema_version",
        "request_id",
        "requested_at",
        "server_id",
        "region_label",
        "overlay_address",
        "wireguard_public_key",
    }
    if set(request) != expected:
        raise ConfigurationError("enrollment request fields do not match schema")
    request["request_id"] = identifier(request["request_id"], "request ID")
    request["server_id"] = identifier(request["server_id"], "server ID")
    request["region_label"] = region_label(request["region_label"])
    request["overlay_address"] = ip_address(
        request["overlay_address"], "overlay address"
    )
    request["wireguard_public_key"] = optional_wireguard_key(
        request["wireguard_public_key"]
    )
    return request


def key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()


def key_component(identifier_value: str) -> str:
    """Map a validated protocol identifier to a safe, unambiguous filename part."""
    encoded = re.sub(r"[^A-Za-z0-9._-]", "_", identifier_value)
    suffix = hashlib.sha256(identifier_value.encode("utf-8")).hexdigest()[:8]
    return f"{encoded}-{suffix}"


def write_key(path: Path, key: bytes) -> None:
    safe_output_file(path, key.hex() + "\n", 0o600)


def approve_request(args: argparse.Namespace) -> int:
    request = load_request(args.request)
    leader = load_object(args.leader_node)
    leader_id = identifier(leader.get("server_id", ""), "leader server ID")
    if str(leader.get("role", "")).casefold() != "leader":
        raise ConfigurationError("leader node configuration must use role=leader")
    leader_region = region_label(leader.get("region_label", ""))
    follower_id = str(request["server_id"])
    if follower_id == leader_id:
        raise ConfigurationError("follower and leader server IDs must differ")
    leader_address = ip_address(args.leader_overlay_address, "leader overlay address")
    follower_address = str(request["overlay_address"])
    if leader_address == follower_address:
        raise ConfigurationError("leader and follower overlay addresses must differ")
    federation_port = int(args.port)
    if not 1 <= federation_port <= 65535:
        raise ConfigurationError("invalid federation port")

    require_empty_private_directory(args.output)
    leader_bundle = args.output / "leader"
    follower_bundle = args.output / "follower"
    for bundle in (leader_bundle, follower_bundle):
        (bundle / "secrets").mkdir(parents=True, mode=0o700)
        os.chmod(bundle, 0o700)
        os.chmod(bundle / "secrets", 0o700)

    follower_file_id = key_component(follower_id)
    leader_file_id = key_component(leader_id)
    follower_to_leader_name = f"{follower_file_id}-to-{leader_file_id}.key"
    leader_to_follower_name = f"{leader_file_id}-to-{follower_file_id}.key"
    follower_to_leader = secrets.token_bytes(32)
    leader_to_follower = secrets.token_bytes(32)
    for bundle in (leader_bundle, follower_bundle):
        write_key(bundle / "secrets" / follower_to_leader_name, follower_to_leader)
        write_key(bundle / "secrets" / leader_to_follower_name, leader_to_follower)

    leader_fragment = {
        "peer": {
            "server_id": follower_id,
            "region_label": request["region_label"],
            "host": follower_address,
            "expected_peer_ip": follower_address,
            "port": federation_port,
            "publish_key_name": leader_to_follower_name,
            "receive_key_name": follower_to_leader_name,
        }
    }
    follower_fragment = {
        "enabled": True,
        "listen_host": follower_address,
        "port": federation_port,
        "peers": [
            {
                "server_id": leader_id,
                "region_label": leader_region,
                "host": leader_address,
                "expected_peer_ip": leader_address,
                "publish_key_name": follower_to_leader_name,
                "receive_key_name": leader_to_follower_name,
            }
        ],
    }
    safe_output_file(
        leader_bundle / "federation-fragment.json",
        json_text(leader_fragment),
        0o600,
    )
    safe_output_file(
        follower_bundle / "federation-fragment.json",
        json_text(follower_fragment),
        0o600,
    )
    record = {
        "schemaVersion": 1,
        "requestId": request["request_id"],
        "approvedAt": utc_now(),
        "leaderServerId": leader_id,
        "followerServerId": follower_id,
        "wireguardPublicKey": request["wireguard_public_key"],
        "followerToLeaderKeyFingerprint": key_fingerprint(follower_to_leader),
        "leaderToFollowerKeyFingerprint": key_fingerprint(leader_to_follower),
        "revoked": False,
    }
    safe_output_file(args.output / "enrollment-record.json", json_text(record), 0o600)
    print(f"approved {follower_id}; created operator-only bundles in {args.output}")
    print("copy each bundle over an authenticated administrative channel; never commit it")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    request = commands.add_parser("request", help="create a credential-free join request")
    request.add_argument("--server-id", required=True)
    request.add_argument("--region-label", required=True)
    request.add_argument("--overlay-address", required=True)
    request.add_argument("--wireguard-public-key", default="")
    request.add_argument("--output", type=Path, required=True)
    request.set_defaults(handler=create_request)

    approve = commands.add_parser("approve", help="approve a request and create pair keys")
    approve.add_argument("--request", type=Path, required=True)
    approve.add_argument("--leader-node", type=Path, required=True)
    approve.add_argument("--leader-overlay-address", required=True)
    approve.add_argument("--port", type=int, default=4540)
    approve.add_argument("--output", type=Path, required=True)
    approve.set_defaults(handler=approve_request)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return int(args.handler(args))
    except ConfigurationError as exc:
        print(f"enrollment error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
