#!/usr/bin/env python3
"""Fail-fast local checks for one Tronner federation node.

Run this on every node. A healthy result on all nodes proves that authenticated
heartbeats and presence snapshots traverse every required direction through the
hub, and that the engine's current immutable map exists in both local serving
locations with identical bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import stat
import time
from pathlib import Path


def load_object(path: Path) -> dict:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def latest_current_map(path: Path) -> str:
    maximum_bytes = 2 * 1024 * 1024
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - maximum_bytes))
        data = handle.read(maximum_bytes)
    for raw in reversed(data.splitlines()):
        if not raw.startswith(b"CURRENT_MAP "):
            continue
        parts = raw.decode("iso8859-1", "replace").split(maxsplit=3)
        if len(parts) == 4:
            return parts[3]
    return ""


def is_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def catalog_summary(controller: dict) -> tuple[dict[str, object], list[str]]:
    """Verify the effective Firebase catalog and its public map mirror."""
    failures: list[str] = []
    details: dict[str, object] = {}
    if str(controller.get("repository_source", "git")).casefold() != "firebase":
        return details, failures
    catalog_root = Path(str(controller.get("firebase_catalog_dir", "")))
    manifest_path = catalog_root / "current" / ".catalog.json"
    try:
        manifest = load_object(manifest_path)
        maps = manifest.get("maps")
        if not isinstance(maps, list):
            raise ValueError("maps must be a list")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return details, [f"Firebase catalog: {exc}"]

    active: dict[str, str] = {}
    for item in maps:
        if not isinstance(item, dict) or item.get("status") != "active":
            continue
        key = item.get("resourcePath")
        digest = item.get("sha256")
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            failures.append("Firebase catalog contains an invalid active map")
            continue
        active[key] = digest

    exclusions: set[str] = set()
    database = Path(str(controller.get("database", "")))
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                ("excluded_map_keys",),
            ).fetchone()
        finally:
            connection.close()
        values = json.loads(row[0]) if row else []
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ValueError("excluded_map_keys must be a string list")
        exclusions = set(values)
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        failures.append(f"catalog exclusions: {exc}")

    effective = sorted(set(active) - exclusions)
    digest_input = "".join(f"{key}\0{active[key]}\n" for key in effective)
    try:
        catalog_version = int(manifest.get("catalogVersion", 0) or 0)
    except (TypeError, ValueError):
        catalog_version = 0
        failures.append("Firebase catalog has an invalid version")
    details.update(
        {
            "catalog_generation": str(manifest.get("generation", "")),
            "catalog_version": catalog_version,
            "catalog_active_maps": len(active),
            "catalog_exclusions": len(exclusions),
            "catalog_effective_maps": len(effective),
            "catalog_effective_sha256": hashlib.sha256(
                digest_input.encode("utf-8")
            ).hexdigest(),
        }
    )

    public_root = Path(str(controller.get("public_dir", "")))
    missing: list[str] = []
    mismatched: list[str] = []
    for key in effective:
        path = public_root / key
        if not path.is_file():
            missing.append(key)
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != active[key]:
            mismatched.append(key)
    details["catalog_public_missing"] = len(missing)
    details["catalog_public_mismatched"] = len(mismatched)
    if missing:
        failures.append(
            "effective maps missing from public mirror: "
            + ", ".join(missing[:5])
        )
    if mismatched:
        failures.append(
            "effective maps differ from Firebase catalog: "
            + ", ".join(mismatched[:5])
        )
    return details, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--federation-config",
        type=Path,
        default=Path("/etc/tronner-federation/config.json"),
    )
    parser.add_argument(
        "--controller-config",
        type=Path,
        default=Path("/etc/tronner-racing/config.json"),
    )
    parser.add_argument(
        "--health-file",
        type=Path,
        default=Path("/run/tronner-federation/health.json"),
    )
    parser.add_argument("--maximum-age", type=float, default=8.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    failures: list[str] = []
    details: dict[str, object] = {}
    loaded: dict[str, dict] = {}
    for label, path in (
        ("federation config", args.federation_config),
        ("controller config", args.controller_config),
        ("sidecar health snapshot", args.health_file),
    ):
        try:
            loaded[label] = load_object(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{label}: {exc}")
            loaded[label] = {}
    federation = loaded["federation config"]
    controller = loaded["controller config"]
    health = loaded["sidecar health snapshot"]

    now_ns = time.time_ns()
    maximum_age_ns = int(max(1.0, args.maximum_age) * 1_000_000_000)
    local_id = str(federation.get("server_id", ""))
    members = federation.get("members", {})
    expected = (
        sorted(set(members) - {local_id})
        if isinstance(members, dict)
        else []
    )
    details["server_id"] = local_id
    details["expected_origins"] = expected

    health_server_id = str(health.get("server_id", ""))
    if health and health_server_id != local_id:
        failures.append(
            "sidecar health snapshot belongs to "
            f"{health_server_id or 'an unknown node'}, expected {local_id or 'unknown'}"
        )

    generated_ns = int(health.get("generated_ns", 0) or 0)
    if generated_ns <= 0 or now_ns - generated_ns > maximum_age_ns:
        failures.append("sidecar health snapshot is stale")
    received = health.get("received", {})
    received = received if isinstance(received, dict) else {}
    peer_ages: dict[str, dict[str, float | None]] = {}
    observed_kinds: dict[str, dict[str, float]] = {}
    for server_id in expected:
        peer = received.get(server_id, {})
        kinds = peer.get("kinds", {}) if isinstance(peer, dict) else {}
        kinds = kinds if isinstance(kinds, dict) else {}
        peer_ages[server_id] = {}
        observed_kinds[server_id] = {}
        for kind, item in sorted(kinds.items()):
            if not isinstance(item, dict):
                continue
            received_ns = int(item.get("received_ns", 0) or 0)
            if received_ns:
                observed_kinds[server_id][kind] = (
                    now_ns - received_ns
                ) / 1_000_000_000
        for kind in ("heartbeat", "player_snapshot"):
            item = kinds.get(kind, {})
            received_ns = (
                int(item.get("received_ns", 0) or 0)
                if isinstance(item, dict)
                else 0
            )
            age = (now_ns - received_ns) / 1_000_000_000 if received_ns else None
            peer_ages[server_id][kind] = age
            if received_ns <= 0 or now_ns - received_ns > maximum_age_ns:
                failures.append(f"no fresh {kind} from {server_id}")
    details["peer_age_seconds"] = peer_ages
    details["observed_kind_age_seconds"] = observed_kinds

    socket_paths = {
        "engine_export": federation.get("engine_export_socket"),
        "controller_publish": federation.get("controller_publish_socket"),
        "controller_import": federation.get("controller_import_socket"),
        "engine_import": federation.get("engine_import_socket"),
    }
    socket_health = {}
    for label, value in socket_paths.items():
        path = Path(str(value)) if value else Path()
        ready = bool(value) and is_socket(path)
        socket_health[label] = ready
        if not ready:
            failures.append(f"{label} socket is unavailable")
    details["sockets"] = socket_health

    if controller:
        catalog_details, catalog_failures = catalog_summary(controller)
        details.update(catalog_details)
        failures.extend(catalog_failures)

    ladderlog = Path(str(controller.get("ladderlog", "")))
    map_key = latest_current_map(ladderlog) if ladderlog.is_file() else ""
    details["current_map"] = map_key
    if not map_key:
        failures.append("current map is unavailable from ladderlog")
    else:
        resource = Path(str(controller.get("resource_cache_dir", ""))) / map_key
        public = Path(str(controller.get("public_dir", ""))) / map_key
        map_files = {}
        for label, path in (("engine_cache", resource), ("public_mirror", public)):
            if not path.is_file():
                failures.append(f"current map missing from {label}: {map_key}")
                map_files[label] = None
            else:
                map_files[label] = hashlib.sha256(path.read_bytes()).hexdigest()
        if (
            map_files.get("engine_cache")
            and map_files.get("public_mirror")
            and map_files["engine_cache"] != map_files["public_mirror"]
        ):
            failures.append("current map differs between engine cache and public mirror")
        details["current_map_sha256"] = map_files

    result = {"healthy": not failures, "failures": failures, **details}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        status = "HEALTHY" if not failures else "UNHEALTHY"
        print(f"{status} {local_id or 'unknown'}")
        for server_id, ages in peer_ages.items():
            heartbeat = ages.get("heartbeat")
            snapshot = ages.get("player_snapshot")
            print(
                f"  {server_id}: heartbeat={heartbeat!s}s "
                f"snapshot={snapshot!s}s"
            )
        if map_key:
            print(f"  map: {map_key}")
        if "catalog_effective_maps" in details:
            print(
                "  catalog: "
                f"{details['catalog_effective_maps']} effective map(s), "
                f"hash={details['catalog_effective_sha256']}"
            )
        for failure in failures:
            print(f"  FAIL: {failure}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
