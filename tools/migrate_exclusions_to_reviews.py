#!/usr/bin/env python3
"""Move the controller's local map exclusions into Vectron's review queue."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "controller"
if str(CONTROLLER) not in sys.path:
    sys.path.insert(0, str(CONTROLLER))

from firebase_catalog import FirebaseCatalogClient, FirebaseCatalogError  # noqa: E402


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Atomically create Vectron reviews for every locally excluded map, "
            "wait for the catalog manifest, and clear only the migrated exclusions."
        )
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path("/var/backups/tronner-racing"),
    )
    parser.add_argument("--manifest-timeout", type=float, default=300.0)
    arguments = parser.parse_args()
    if arguments.apply and arguments.expected_count is None:
        parser.error("--apply requires --expected-count from a reviewed dry run")
    if arguments.expected_count is not None and arguments.expected_count < 0:
        parser.error("--expected-count cannot be negative")
    if arguments.manifest_timeout <= 0:
        parser.error("--manifest-timeout must be positive")
    return arguments


def metadata_json(
    connection: sqlite3.Connection,
    key: str,
    default: object,
) -> object:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key=?",
        (key,),
    ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"metadata {key} is not valid JSON") from exc


def catalog_by_path(config: dict) -> tuple[dict, dict[str, dict]]:
    catalog_root = Path(
        config.get(
            "firebase_catalog_dir",
            "/var/lib/tronner-racing/firebase-catalog",
        )
    )
    manifest_path = catalog_root / "current" / ".catalog.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    maps = {
        str(item.get("resourcePath") or ""): item
        for item in manifest.get("maps", [])
        if isinstance(item, dict) and item.get("resourcePath")
    }
    return manifest, maps


def build_exclusion_plan(
    connection: sqlite3.Connection,
    maps: dict[str, dict],
) -> list[dict]:
    raw_keys = metadata_json(connection, "excluded_map_keys", [])
    raw_reasons = metadata_json(connection, "excluded_map_reasons", {})
    if not isinstance(raw_keys, list) or not isinstance(raw_reasons, dict):
        raise RuntimeError("exclusion metadata has an unexpected shape")
    plan = []
    for resource_path in sorted({str(item) for item in raw_keys}):
        map_record = maps.get(resource_path)
        if not map_record or not map_record.get("mapId"):
            raise RuntimeError(
                f"excluded map is missing from the current catalog: {resource_path}"
            )
        plan.append(
            {
                "mapId": str(map_record["mapId"]),
                "resourcePath": resource_path,
                "authorName": str(map_record.get("authorName") or ""),
                "mapName": str(map_record.get("mapName") or ""),
                "mapVersion": str(map_record.get("mapVersion") or ""),
                "manifestStatus": str(map_record.get("status") or ""),
                "reason": str(raw_reasons.get(resource_path) or ""),
            }
        )
    return plan


def inspect_firestore_targets(
    client: FirebaseCatalogClient,
    plan: list[dict],
) -> dict:
    statuses = {"active": 0, "inactive": 0}
    linked = 0
    for item in plan:
        current = client.get_document("maps", item["mapId"])
        if current.get("resourcePath") != item["resourcePath"]:
            raise FirebaseCatalogError(
                f"catalog resource path changed for {item['resourcePath']}"
            )
        status = str(current.get("status") or "")
        if status not in statuses:
            raise FirebaseCatalogError(
                f"map {item['resourcePath']} has unsupported status {status!r}"
            )
        statuses[status] += 1
        linked += bool(current.get("reviewSubmissionId"))
    return {"statuses": statuses, "alreadyLinked": linked}


def backup_database(database: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"records-before-exclusion-review-{stamp}.sqlite3"
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as backup:
        source.backup(backup)
    os.chmod(destination, 0o600)
    return destination


def wait_for_catalog_manifest(
    client: FirebaseCatalogClient,
    *,
    previous_state: dict,
    resource_paths: set[str],
    timeout: float,
) -> dict:
    deadline = time.monotonic() + timeout
    previous_version = int(previous_state.get("catalogVersion") or 0)
    previous_generation = str(previous_state.get("generation") or "")
    last_state = previous_state
    while time.monotonic() < deadline:
        last_state = client.get_catalog_state()
        version = int(last_state.get("catalogVersion") or 0)
        generation = str(last_state.get("generation") or "")
        if version > previous_version and generation != previous_generation:
            manifest_maps, verified_generation = client._manifest_maps(last_state)
            status_by_path = {
                str(item.get("resourcePath") or ""): item.get("status")
                for item in manifest_maps
            }
            if all(status_by_path.get(path) == "inactive" for path in resource_paths):
                return {
                    "catalogVersion": version,
                    "generation": verified_generation,
                }
        time.sleep(2)
    raise TimeoutError(
        "the Firebase catalog manifest did not include every migrated map before "
        f"the timeout (last version {last_state.get('catalogVersion')}, "
        f"generation {last_state.get('generation')})"
    )


def clear_migrated_exclusions(
    database: Path,
    resource_paths: set[str],
) -> tuple[int, int]:
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current_keys = {
            str(item)
            for item in metadata_json(connection, "excluded_map_keys", [])
        }
        current_reasons = metadata_json(
            connection,
            "excluded_map_reasons",
            {},
        )
        if not isinstance(current_reasons, dict):
            raise RuntimeError("excluded_map_reasons has an unexpected shape")
        removed = current_keys & resource_paths
        remaining = current_keys - resource_paths
        remaining_reasons = {
            str(key): str(value)
            for key, value in current_reasons.items()
            if str(key) in remaining
        }
        values = {
            "excluded_map_keys": sorted(remaining),
            "excluded_map_reasons": remaining_reasons,
        }
        for key, value in values.items():
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )
        connection.commit()
    return len(removed), len(remaining)


def write_report(path: Path | None, report: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    arguments = parse_arguments()
    config = json.loads(arguments.config.read_text("utf-8"))
    database = Path(config["database"])
    manifest, maps = catalog_by_path(config)
    with sqlite3.connect(database) as connection:
        plan = build_exclusion_plan(connection, maps)
    if arguments.expected_count is not None and len(plan) != arguments.expected_count:
        raise RuntimeError(
            f"expected {arguments.expected_count} exclusions but found {len(plan)}"
        )

    client = FirebaseCatalogClient(config)
    inspection = inspect_firestore_targets(client, plan)
    existing_reviews = client.list_map_reviews()
    report = {
        "mode": "apply" if arguments.apply else "dry-run",
        "catalogVersionBefore": manifest.get("catalogVersion"),
        "generationBefore": manifest.get("generation"),
        "excludedCount": len(plan),
        "existingReviewCount": len(existing_reviews),
        **inspection,
        "maps": plan,
    }
    if not arguments.apply:
        write_report(arguments.report, report)
        print(json.dumps({key: value for key, value in report.items() if key != "maps"}, indent=2))
        return 0

    before_state = client.get_catalog_state()
    backup_path = backup_database(database, arguments.backup_dir)
    reviews = client.submit_excluded_map_reviews(plan)
    resource_paths = {item["resourcePath"] for item in plan}
    live_reviews = client.list_map_reviews()
    live_review_paths = {
        str(item.get("sourceResourcePath") or "")
        for item in live_reviews
    }
    missing_reviews = resource_paths - live_review_paths
    if missing_reviews:
        raise RuntimeError(
            f"{len(missing_reviews)} migrated maps are missing from the review queue"
        )
    catalog_after = wait_for_catalog_manifest(
        client,
        previous_state=before_state,
        resource_paths=resource_paths,
        timeout=arguments.manifest_timeout,
    )
    removed, remaining = clear_migrated_exclusions(database, resource_paths)
    report.update(
        {
            "backupPath": str(backup_path),
            "reviewCountCreatedOrConfirmed": len(reviews),
            "reviewCountAfter": len(live_reviews),
            "removedExclusionCount": removed,
            "remainingExclusionCount": remaining,
            "catalogVersionAfter": catalog_after["catalogVersion"],
            "generationAfter": catalog_after["generation"],
        }
    )
    write_report(arguments.report, report)
    print(json.dumps({key: value for key, value in report.items() if key != "maps"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
