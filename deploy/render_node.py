#!/usr/bin/env python3
"""Render one fail-closed Tronner node from operator-owned JSON inputs."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
REGION_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,15}$")
GIT_REF = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,126}[A-Za-z0-9])?$")
SECRET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.key$")
EXAMPLE_HOST_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
    ".example.invalid",
    ".invalid",
    ".test",
)


class ConfigurationError(ValueError):
    """Raised when an operator input is unsafe or incomplete."""


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"unable to read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path} must contain a JSON object")
    if value.get("schema_version") != 1:
        raise ConfigurationError(f"{path} must use schema_version 1")
    return value


def identifier(value: object, label: str) -> str:
    text = str(value).strip()
    if not IDENTIFIER.fullmatch(text):
        raise ConfigurationError(f"invalid {label}")
    return text


def region_label(value: object, label: str = "region label") -> str:
    text = str(value).strip()
    if not REGION_LABEL.fullmatch(text):
        raise ConfigurationError(f"invalid {label}")
    return text


def git_ref(value: object, label: str = "repository branch") -> str:
    text = str(value).strip()
    if (
        not GIT_REF.fullmatch(text)
        or ".." in text
        or "//" in text
        or text.endswith(".lock")
        or text.startswith("-")
    ):
        raise ConfigurationError(f"invalid {label}")
    return text


def bounded_line(value: object, label: str, maximum: int = 256) -> str:
    text = str(value).strip()
    if not text or len(text) > maximum or any(character in text for character in "\r\n\0"):
        raise ConfigurationError(f"invalid {label}")
    return text


def port(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"invalid {label}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid {label}") from exc
    if not 1 <= number <= 65535:
        raise ConfigurationError(f"invalid {label}")
    return number


def ip_address(value: object, label: str) -> str:
    text = str(value).strip()
    try:
        return str(ipaddress.ip_address(text))
    except ValueError as exc:
        raise ConfigurationError(f"{label} must be a literal IP address") from exc


def ipv4_address(value: object, label: str) -> str:
    text = ip_address(value, label)
    if not isinstance(ipaddress.ip_address(text), ipaddress.IPv4Address):
        raise ConfigurationError(f"{label} must be a literal IPv4 address")
    return text


def private_overlay_address(value: str, label: str) -> None:
    address = ipaddress.ip_address(value)
    if (
        not address.is_private
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    ):
        raise ConfigurationError(f"{label} must be a private overlay address")


def url(value: object, label: str, *, allow_http: bool) -> str:
    text = bounded_line(value, label, 512)
    parsed = urllib.parse.urlsplit(text)
    allowed = {"https"} | ({"http"} if allow_http else set())
    if (
        parsed.scheme not in allowed
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(f"invalid {label}")
    return text


def secret_name(value: object, label: str) -> str:
    text = str(value).strip()
    if not SECRET_NAME.fullmatch(text) or Path(text).name != text:
        raise ConfigurationError(f"invalid {label}")
    return text


def example_hostname(hostname: str) -> bool:
    lowered = hostname.casefold().rstrip(".")
    return lowered in {
        "example.com",
        "example.net",
        "example.org",
        "localhost",
        "test",
    } or lowered.endswith(EXAMPLE_HOST_SUFFIXES)


def atomic_write(path: Path, data: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def json_text(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def render(cluster: dict[str, Any], node: dict[str, Any], *, production: bool) -> dict[str, str]:
    cluster_id = identifier(cluster.get("cluster_id", ""), "cluster ID")
    leader_id = identifier(cluster.get("leader_server_id", ""), "leader server ID")
    server_id = identifier(node.get("server_id", ""), "server ID")
    local_region = region_label(node.get("region_label", ""))
    role = str(node.get("role", "standalone")).strip().casefold()
    if role not in {"standalone", "leader", "follower"}:
        raise ConfigurationError("role must be standalone, leader, or follower")
    if role == "leader" and server_id != leader_id:
        raise ConfigurationError("leader node server_id must match cluster leader_server_id")
    if role == "follower" and server_id == leader_id:
        raise ConfigurationError("a follower cannot use the leader server ID")

    server_name = bounded_line(node.get("server_name", ""), "server name")
    website_url = url(node.get("website_url", ""), "website URL", allow_http=False)
    public_base_url = url(
        node.get("public_base_url", ""),
        "public map base URL",
        allow_http=True,
    )
    if not public_base_url.endswith("/"):
        public_base_url += "/"
    public_hostname = urllib.parse.urlsplit(public_base_url).hostname or ""
    server_dns = str(node.get("server_dns", "")).strip()
    if server_dns:
        server_dns = bounded_line(server_dns, "server DNS name", 253)
        if any(character in server_dns for character in " /:@"):
            raise ConfigurationError("invalid server DNS name")
    game_bind = ip_address(node.get("game_bind", "0.0.0.0"), "game bind")
    game_port = port(node.get("game_port", 4534), "game port")
    resource_bind = ip_address(
        node.get("resource_bind", "0.0.0.0"), "resource bind"
    )
    resource_port = port(node.get("resource_port", 8080), "resource port")
    master_list = node.get("master_list", False)
    if not isinstance(master_list, bool):
        raise ConfigurationError("master_list must be true or false")

    maps = cluster.get("map_repository")
    if not isinstance(maps, dict):
        raise ConfigurationError("map_repository must be an object")
    repository_source = str(maps.get("source", "git")).strip().casefold()
    if repository_source not in {"git", "firebase"}:
        raise ConfigurationError("map_repository source must be git or firebase")
    repository_url = url(maps.get("url", ""), "map repository URL", allow_http=False)
    repository_branch = git_ref(maps.get("branch", "main"))

    federation = node.get("federation", {})
    if not isinstance(federation, dict):
        raise ConfigurationError("federation must be an object")
    federation_enabled = federation.get("enabled", False)
    if not isinstance(federation_enabled, bool):
        raise ConfigurationError("federation.enabled must be true or false")
    if (role == "standalone") != (not federation_enabled):
        raise ConfigurationError(
            "standalone nodes must disable federation; leader/follower nodes must enable it"
        )

    peer_id = ""
    peer_region = ""
    remote_servers: dict[str, str] = {}
    listen_host = ""
    peer_host = ""
    expected_peer_ip = ""
    leader_resource_base_url = ""
    federation_config: dict[str, Any] | None = None
    required_secrets: list[str] = []
    federation_private_addresses: list[tuple[str, str]] = []
    if federation_enabled:
        listen_host = ipv4_address(
            federation.get("listen_host", ""), "federation listen host"
        )
        federation_port = port(federation.get("port", 4540), "federation port")
        raw_members = cluster.get("members")
        raw_peers = federation.get("peers")
        if isinstance(raw_members, dict) and isinstance(raw_peers, list):
            if not 2 <= len(raw_members) <= 16:
                raise ConfigurationError("cluster members must contain 2..16 servers")
            members: dict[str, str] = {}
            for raw_member_id, raw_member_region in raw_members.items():
                member_id = identifier(raw_member_id, "member server ID")
                members[member_id] = region_label(
                    raw_member_region, "member region label"
                )
            if members.get(server_id) != local_region or leader_id not in members:
                raise ConfigurationError(
                    "cluster members must contain matching local and leader identities"
                )
            remote_servers = {
                member_id: member_region
                for member_id, member_region in members.items()
                if member_id != server_id
            }
            expected_ids = (
                set(remote_servers) if role == "leader" else {leader_id}
            )
            if not raw_peers or len(raw_peers) > 15:
                raise ConfigurationError("federation peers must contain 1..15 entries")
            rendered_peers: list[dict[str, object]] = []
            seen_ids: set[str] = set()
            seen_secret_names: set[str] = set()
            for raw_peer in raw_peers:
                if not isinstance(raw_peer, dict):
                    raise ConfigurationError("federation peer must be an object")
                current_peer_id = identifier(
                    raw_peer.get("server_id", ""), "peer server ID"
                )
                current_peer_region = region_label(
                    raw_peer.get("region_label", ""), "peer region label"
                )
                if (
                    current_peer_id in seen_ids
                    or current_peer_id not in expected_ids
                    or members.get(current_peer_id) != current_peer_region
                ):
                    raise ConfigurationError("invalid or duplicate federation peer")
                current_peer_host = ipv4_address(
                    raw_peer.get("host", ""), "federation peer host"
                )
                current_expected_ip = ipv4_address(
                    raw_peer.get("expected_peer_ip", ""), "expected peer IP"
                )
                federation_private_addresses.extend(
                    [
                        (current_peer_host, "federation peer host"),
                        (current_expected_ip, "expected peer IP"),
                    ]
                )
                publish_name = secret_name(
                    raw_peer.get("publish_key_name", ""), "publish key name"
                )
                receive_name = secret_name(
                    raw_peer.get("receive_key_name", ""), "receive key name"
                )
                if (
                    publish_name == receive_name
                    or publish_name in seen_secret_names
                    or receive_name in seen_secret_names
                ):
                    raise ConfigurationError("federation key names must be unique")
                seen_ids.add(current_peer_id)
                seen_secret_names.update((publish_name, receive_name))
                required_secrets.extend((publish_name, receive_name))
                rendered_peers.append(
                    {
                        "server_id": current_peer_id,
                        "region_label": current_peer_region,
                        "host": current_peer_host,
                        "port": port(raw_peer.get("port", federation_port), "peer port"),
                        "expected_ip": current_expected_ip,
                        "publish_key_file": f"/etc/tronner-federation/keys/{publish_name}",
                        "receive_key_file": f"/etc/tronner-federation/keys/{receive_name}",
                    }
                )
            if seen_ids != expected_ids:
                raise ConfigurationError(
                    "leader must peer with every follower; followers must peer only with leader"
                )
            peer_id = leader_id if role == "follower" else rendered_peers[0]["server_id"]
            peer_region = members[str(peer_id)]
            if role == "follower":
                leader_resource_base_url = url(
                    federation.get("leader_resource_base_url", ""),
                    "leader resource base URL",
                    allow_http=True,
                )
                if not leader_resource_base_url.endswith("/"):
                    leader_resource_base_url += "/"
                leader_resource_host = urllib.parse.urlsplit(
                    leader_resource_base_url
                ).hostname
                leader_peer = next(
                    peer
                    for peer in rendered_peers
                    if peer["server_id"] == leader_id
                )
                if leader_resource_host != leader_peer["host"]:
                    raise ConfigurationError(
                        "leader resource URL must use the leader overlay address"
                    )
            federation_config = {
                "protocol_version": 2,
                "cluster_id": cluster_id,
                "server_id": server_id,
                "mode": "both",
                "role": role,
                "leader_server_id": leader_id,
                "region_label": local_region,
                "members": members,
                "listen_host": listen_host,
                "listen_port": federation_port,
                "peers": rendered_peers,
                "ladderlog": "/var/lib/armagetronad/ladderlog.txt",
                "engine_export_socket": "/run/tronner-federation/engine-export.sock",
                "controller_publish_socket": "/run/tronner-federation/controller-publish.sock",
                "controller_import_socket": "/run/tronner-racing/federation-import.sock",
                "engine_import_socket": "/run/armagetronad/federation-import.sock",
                "heartbeat_seconds": 2.0,
                "maximum_clock_skew_seconds": 30.0,
                "game_text_encoding": "iso8859-1",
            }
            federation_private_addresses.append(
                (listen_host, "federation listen host")
            )
        else:
            peer_id = identifier(
                federation.get("peer_server_id", ""), "peer server ID"
            )
            if peer_id == server_id:
                raise ConfigurationError("peer server ID must differ from local server ID")
            if role == "follower" and peer_id != leader_id:
                raise ConfigurationError("a follower must peer with the configured leader")
            peer_region = region_label(
                federation.get("peer_region_label", ""), "peer region label"
            )
            remote_servers = {peer_id: peer_region}
            peer_host = ipv4_address(
                federation.get("peer_host", ""), "federation peer host"
            )
            expected_peer_ip = ipv4_address(
                federation.get("expected_peer_ip", ""), "expected peer IP"
            )
            if role == "follower":
                leader_resource_base_url = url(
                    federation.get("leader_resource_base_url", ""),
                    "leader resource base URL",
                    allow_http=True,
                )
                if not leader_resource_base_url.endswith("/"):
                    leader_resource_base_url += "/"
                if (
                    urllib.parse.urlsplit(leader_resource_base_url).hostname
                    != peer_host
                ):
                    raise ConfigurationError(
                        "leader resource URL must use the leader overlay address"
                    )
            federation_private_addresses.extend(
                [
                    (listen_host, "federation listen host"),
                    (peer_host, "federation peer host"),
                    (expected_peer_ip, "expected peer IP"),
                ]
            )
            publish_name = secret_name(
                federation.get("publish_key_name", ""), "publish key name"
            )
            receive_name = secret_name(
                federation.get("receive_key_name", ""), "receive key name"
            )
            if publish_name == receive_name:
                raise ConfigurationError("publish and receive keys must be different")
            required_secrets = [publish_name, receive_name]
            federation_config = {
                "cluster_id": cluster_id,
                "server_id": server_id,
                "mode": "both",
                "role": role,
                "region_label": local_region,
                "peer_host": peer_host,
                "peer_port": federation_port,
                "listen_host": listen_host,
                "listen_port": federation_port,
                "expected_server_id": peer_id,
                "expected_peer_ip": expected_peer_ip,
                "publish_key_file": f"/etc/tronner-federation/keys/{publish_name}",
                "receive_key_file": f"/etc/tronner-federation/keys/{receive_name}",
                "ladderlog": "/var/lib/armagetronad/ladderlog.txt",
                "engine_export_socket": "/run/tronner-federation/engine-export.sock",
                "controller_publish_socket": "/run/tronner-federation/controller-publish.sock",
                "controller_import_socket": "/run/tronner-racing/federation-import.sock",
                "engine_import_socket": "/run/armagetronad/federation-import.sock",
                "heartbeat_seconds": 2.0,
                "maximum_clock_skew_seconds": 30.0,
                "game_text_encoding": "iso8859-1",
            }

    firebase = cluster.get("firebase", {})
    if not isinstance(firebase, dict):
        raise ConfigurationError("firebase must be an object")
    firebase_enabled = firebase.get("enabled", False)
    if not isinstance(firebase_enabled, bool):
        raise ConfigurationError("firebase.enabled must be true or false")
    if repository_source == "firebase" and not firebase_enabled:
        raise ConfigurationError("Firebase map catalogs require firebase.enabled")

    controller: dict[str, Any] = {
        "server_id": server_id,
        "repository_source": repository_source,
        "repository_git_url": repository_url,
        "repository_branch": repository_branch,
        "repository_checkout": "/var/lib/tronner-racing/repository",
        "repository_auto_sync": True,
        "repository_refresh_seconds": 300,
        "public_dir": "/var/lib/tronner-racing/public",
        "public_bind": resource_bind,
        "public_port": resource_port,
        "public_base_url": public_base_url,
        "resource_cache_dir": "/var/lib/armagetronad/resource/automatic",
        "map_override_dir": "/var/lib/tronner-racing/map-overrides",
        "map_revision_dir": "/var/lib/tronner-racing/map-revisions",
        "dtd_source_dir": "/opt/armagetronad/share/games/armagetronad-dedicated/resource/included",
        "console_input": "/var/lib/armagetronad/console.in",
        "ladderlog": "/var/lib/armagetronad/ladderlog.txt",
        "online_players_file": "/var/lib/armagetronad/online_players.txt",
        "database": "/var/lib/tronner-racing/TronnerRacing.sqlite3",
        "spawn_preferences_file": "/var/lib/tronner-racing/spawn_preferences.json",
        "helpful_messages_file": "/etc/tronner-racing/helpful_messages.txt",
        "server_options_refresh_seconds": 1,
        "map_duration_seconds": 300,
        "map_time_racer_multiplier": 1.25,
        "map_time_target_finishes": 5,
        "extend_seconds": 300,
        "freeze_seconds": 3.0,
        "freeze_tick_seconds": 0.05,
        "respawn_delay_seconds": 2.0,
        "checkpoint_respawn_delay_seconds": 0.1,
        "checkpoint_double_respawn_seconds": 1.5,
        "go_message_seconds": 1.0,
        "final_countdown_idle_seconds": 10,
        "clock_runout_prevention_enabled": True,
        "clock_runout_minimum_seconds": 120,
        "clock_runout_personal_best_multiplier": 3,
        "clock_runout_checkpoint_grace_seconds": 20,
        "afk_timeout_seconds": 60,
        "afk_poll_interval_seconds": 1.0,
        "afk_position_epsilon": 0.01,
        "round_display_delay_seconds": 0.35,
        "round_intermission_display_delay_seconds": 0.0,
        "map_transition_timeout_seconds": 20,
        "map_transition_probe_seconds": 1,
        "map_transition_failure_confirmations": 2,
        "command_rate_maximum": 4,
        "command_rate_window_seconds": 5,
        "command_rate_warning_interval_seconds": 5,
        "maximum_record_seconds": 7200,
        "default_size_factor": 0,
        "size_admin_access_level": 1,
        "map_admin_access_level": 1,
        "records_admin_access_level": 1,
        "firebase_catalog_dir": "/var/lib/tronner-racing/firebase-catalog",
        "firebase_catalog_require_ready": True,
        "firebase_request_timeout_seconds": 20,
        "firebase_server_id": server_id,
        "federation": {"role": "off"},
        "live_dashboard": {
            "enabled": False,
            "chat_enabled": False,
            "management_enabled": False,
            "local_region": local_region,
            "remote_region": peer_region,
        },
    }
    if federation_enabled:
        controller["federation"] = {
            "role": role,
            "local_server_id": server_id,
            "remote_server_id": peer_id,
            "remote_region_label": peer_region,
            "leader_server_id": leader_id,
            "remote_servers": remote_servers,
            "controller_import_socket": "/run/tronner-racing/federation-import.sock",
            "controller_publish_socket": "/run/tronner-federation/controller-publish.sock",
            "sync_chat": True,
            "sync_presence": True,
            "sync_maps": role == "follower",
            "map_prepare_lead_seconds": 3.0,
            "round_sync_enabled": True,
            "round_sync_release_lead_seconds": 0.5,
            "round_sync_timeout_seconds": 30.0,
        }
        if role == "follower":
            controller["federation"]["leader_resource_base_url"] = (
                leader_resource_base_url
            )
            controller["federation"]["resource_timeout_seconds"] = 10.0

    firebase_database_url = ""
    if firebase_enabled:
        project_id = identifier(firebase.get("project_id", ""), "Firebase project ID")
        bucket = bounded_line(firebase.get("storage_bucket", ""), "Firebase bucket")
        controller.update(
            {
                "firebase_project_id": project_id,
                "firebase_storage_bucket": bucket,
                "firebase_service_account_file": "/etc/tronner-racing/firebase-service-account.json",
            }
        )
        live_enabled = firebase.get("live_dashboard_enabled", False)
        management_enabled = firebase.get("management_enabled", False)
        if not isinstance(live_enabled, bool) or not isinstance(management_enabled, bool):
            raise ConfigurationError("Firebase feature flags must be true or false")
        if live_enabled:
            if role not in {"leader", "standalone"}:
                raise ConfigurationError(
                    "only the leader or a standalone node may publish the live dashboard"
                )
            firebase_database_url = url(
                firebase.get("database_url", ""),
                "Firebase database URL",
                allow_http=False,
            )
            controller["live_dashboard"] = {
                "enabled": True,
                "chat_enabled": True,
                "management_enabled": management_enabled,
                "database_url": firebase_database_url,
                "local_region": local_region,
                "remote_region": peer_region,
            }

    if production:
        parsed_repository = urllib.parse.urlsplit(repository_url)
        website_hostname = urllib.parse.urlsplit(website_url).hostname or ""
        if cluster_id.casefold().startswith("example-"):
            raise ConfigurationError("replace the example cluster ID before production rendering")
        if any(
            example_hostname(hostname)
            for hostname in (
                website_hostname,
                public_hostname,
                parsed_repository.hostname or "",
            )
        ):
            raise ConfigurationError("replace example hostnames before production rendering")
        if server_name.casefold().startswith("example "):
            raise ConfigurationError("replace the example server name")
        if server_dns and example_hostname(server_dns):
            raise ConfigurationError("replace the example server DNS name")
        if federation_enabled:
            for address, label in federation_private_addresses:
                private_overlay_address(address, label)
        if firebase_database_url and example_hostname(
            urllib.parse.urlsplit(firebase_database_url).hostname or ""
        ):
            raise ConfigurationError("replace the example Firebase database URL")
    server_lines = [
        "# Generated by deploy/render_node.py. Do not edit on the server.",
        f"SERVER_PORT {game_port}",
        f"SERVER_IP {game_bind}",
        f"TALK_TO_MASTER {1 if master_list else 0}",
        f"SERVER_NAME {server_name}",
        f"URL {website_url}",
        f"RESOURCE_REPOSITORY_SERVER {public_base_url}",
    ]
    if server_dns:
        server_lines.append(f"SERVER_DNS {server_dns}")
    server_lines.extend(
        [
            "GLOBAL_ID 1",
            "INCLUDE tronner-racing.cfg",
            "INCLUDE federation.cfg",
            "",
        ]
    )

    if federation_enabled:
        engine_federation = "\n".join(
            [
                "# Generated authenticated federation bridge.",
                "LADDERLOG_WRITE_CURRENT_MAP 1",
                "LADDERLOG_WRITE_FEDERATION_ROUND_READY 1",
                "FEDERATION_ROUND_SYNC 1",
                "FEDERATION_ROUND_SYNC_TIMEOUT 30",
                "FEDERATION_ROUND_RELEASE_AT 0",
                f"FEDERATION_LOCAL_LABEL {local_region}",
                "FEDERATION_EXPORT_ENABLED 1",
                "FEDERATION_EXPORT_SOCKET /run/tronner-federation/engine-export.sock",
                "FEDERATION_EXPORT_INTERVAL 0.05",
                "FEDERATION_IMPORT_ENABLED 1",
                "FEDERATION_IMPORT_SOCKET /run/armagetronad/federation-import.sock",
                f"FEDERATION_GHOST_LABEL {peer_region}",
                "FEDERATION_GHOST_TIMEOUT 6",
                "FEDERATION_GHOST_LIMIT 256",
                "",
            ]
        )
    else:
        engine_federation = "\n".join(
            [
                "# Standalone, fail-closed defaults.",
                "FEDERATION_ROUND_SYNC 0",
                "FEDERATION_EXPORT_ENABLED 0",
                "FEDERATION_IMPORT_ENABLED 0",
                "",
            ]
        )

    manifest = {
        "schemaVersion": 1,
        "clusterId": cluster_id,
        "serverId": server_id,
        "role": role,
        "federationEnabled": federation_enabled,
        "masterListEnabled": master_list,
        "firebaseEnabled": firebase_enabled,
        "requiredSecretFiles": required_secrets,
    }
    result = {
        "controller.json": json_text(controller),
        "server.cfg": "\n".join(server_lines),
        "federation.cfg": engine_federation,
        "manifest.json": json_text(manifest),
    }
    if federation_config is not None:
        result["federation.json"] = json_text(federation_config)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--production",
        action="store_true",
        help="reject documentation values and incomplete production identity",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cluster = load_object(args.cluster)
        node = load_object(args.node)
        rendered = render(cluster, node, production=args.production)
        args.output.mkdir(parents=True, exist_ok=True)
        os.chmod(args.output, 0o750)
        for name, contents in rendered.items():
            atomic_write(args.output / name, contents)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    print(f"rendered {len(rendered)} files for {node['server_id']} in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
