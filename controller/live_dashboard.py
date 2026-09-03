"""Cost-bounded Firebase publisher for the public Tronner racing dashboard."""

from __future__ import annotations

import collections
import datetime as dt
import gzip
import hashlib
import json
import math
import re
import time
import urllib.parse

from firebase_catalog import FirebaseCatalogClient, FirestoreTimestamp


SCHEMA_VERSION = 1
CHAT_LIMIT = 250
ACTIVITY_LIMIT = 250
EVENT_PRUNE_INTERVAL = 25
EVENT_PRUNE_SECONDS = 300.0
MAP_ENTRY_LIMIT = 100
OVERALL_ENTRY_LIMIT = 100
HISTORY_ENTRY_LIMIT = 2500
ADMIN_COMMAND_LIMIT = 10
ADMIN_HISTORY_LIMIT = 200
ADMIN_CONSOLE_BATCH_LIMIT = 12
ADMIN_AUDIT_LIMIT = 1000


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def payload_hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def leaderboard_document_id(map_key: str) -> str:
    return "map_" + hashlib.sha256(map_key.encode("utf-8")).hexdigest()


def history_document_id(map_key: str, player_id: str, server_id: str) -> str:
    map_digest = hashlib.sha256(map_key.encode("utf-8")).hexdigest()
    server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)[:64]
    return f"history_{map_digest}_{player_id}_{server}"


def replay_storage_path(server_id: str, run_id: int) -> str:
    server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)[:64]
    return f"_racing/replays/{server}/{int(run_id)}.json.gz"


def settings_storage_path(server_id: str, fingerprint: str) -> str:
    server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)[:64]
    return f"_racing/settings/{server}/{fingerprint}.json.gz"


def inferred_map_metadata(map_key: str) -> dict[str, object]:
    parts = map_key.split("/")
    author = parts[0] if len(parts) >= 3 else "Unknown"
    filename = parts[-1].removesuffix(".aamap.xml") if parts else map_key
    match = re.match(r"^(.*)-(v?\d+(?:\.\d+)*)$", filename, re.IGNORECASE)
    return {
        "mapId": "",
        "name": match.group(1) if match else filename,
        "author": author,
        "version": match.group(2) if match else "",
        "storagePath": "",
    }


def map_rating_fields(metadata: dict[str, object]) -> dict[str, object]:
    try:
        rating = float(metadata.get("rating"))
        count = int(metadata.get("ratingCount", 0))
    except (TypeError, ValueError):
        return {"rating": None, "ratingCount": 0}
    if not math.isfinite(rating) or not 1 <= rating <= 5 or count < 1:
        return {"rating": None, "ratingCount": 0}
    return {"rating": round(rating, 4), "ratingCount": count}


def public_player_id(identity_key: str) -> str:
    return hashlib.sha256(("racing:" + identity_key).encode("utf-8")).hexdigest()[:24]


def map_leaderboard(map_key: str, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    ordered = sorted(
        (row for row in rows if row["mapKey"] == map_key),
        key=lambda row: (
            float(row["bestSeconds"]),
            math.inf if row.get("bestTurns") is None else int(row["bestTurns"]),
            float(row["achievedAt"]),
        ),
    )[:MAP_ENTRY_LIMIT]
    return [
        {
            "rank": index,
            "playerId": public_player_id(str(row["identityKey"])),
            "name": str(row["username"])[:128],
            "seconds": round(float(row["bestSeconds"]), 6),
            "turns": row.get("bestTurns"),
            "authenticated": bool(row.get("authenticated")),
            "achievedAt": int(float(row["achievedAt"]) * 1000),
            "hasReplay": bool(row.get("hasReplay")),
        }
        for index, row in enumerate(ordered, 1)
    ]


def overall_leaderboard(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Score authenticated racers: 100 points for first down to 1 for 100th."""
    maps: dict[str, list[dict[str, object]]] = collections.defaultdict(list)
    for row in rows:
        if row.get("authenticated"):
            maps[str(row["mapKey"])].append(row)
    totals: dict[str, dict[str, object]] = {}
    for map_rows in maps.values():
        ordered = sorted(
            map_rows,
            key=lambda row: (
                float(row["bestSeconds"]),
                math.inf if row.get("bestTurns") is None else int(row["bestTurns"]),
                float(row["achievedAt"]),
            ),
        )[:MAP_ENTRY_LIMIT]
        for rank, row in enumerate(ordered, 1):
            identity = str(row["identityKey"])
            total = totals.setdefault(identity, {
                "name": str(row["username"])[:128],
                "points": 0,
                "maps": 0,
                "wins": 0,
            })
            total["name"] = str(row["username"])[:128]
            total["points"] = int(total["points"]) + (MAP_ENTRY_LIMIT + 1 - rank)
            total["maps"] = int(total["maps"]) + 1
            total["wins"] = int(total["wins"]) + int(rank == 1)
    ordered_totals = sorted(
        totals.items(),
        key=lambda item: (
            -int(item[1]["points"]),
            -int(item[1]["wins"]),
            -int(item[1]["maps"]),
            str(item[1]["name"]).casefold(),
        ),
    )[:OVERALL_ENTRY_LIMIT]
    return [
        {
            "rank": rank,
            "playerId": public_player_id(identity),
            **total,
        }
        for rank, (identity, total) in enumerate(ordered_totals, 1)
    ]


class FirebaseLiveDashboardPublisher:
    def __init__(
        self,
        firebase: FirebaseCatalogClient,
        database_url: str,
        store: object,
    ):
        self.firebase = firebase
        self.database_url = database_url.rstrip("/")
        if not self.database_url.startswith("https://"):
            raise ValueError("live dashboard database URL must be HTTPS")
        self.store = store
        saved = store.get_json("live_dashboard_leaderboard_hashes", {})
        self.leaderboard_hashes = saved if isinstance(saved, dict) else {}
        saved_profiles = store.get_json("live_dashboard_profile_hashes", {})
        self.profile_hashes = saved_profiles if isinstance(saved_profiles, dict) else {}
        saved_catalog = store.get_json("live_dashboard_map_catalog", {})
        self.map_catalog = saved_catalog if isinstance(saved_catalog, dict) else {}
        self.live_hash = ""
        self.live_written_at = 0.0
        self.event_writes: dict[str, int] = collections.defaultdict(int)
        self.event_pruned_at: dict[str, float] = {}

    def _rtdb(
        self,
        path: str,
        method: str,
        value: object | None = None,
        *,
        query: dict[str, str] | None = None,
    ) -> object:
        encoded_path = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/") if part)
        access_token = self.firebase.tokens.access_token()
        parameters = {"access_token": access_token, **(query or {})}
        url = f"{self.database_url}/{encoded_path}.json?{urllib.parse.urlencode(parameters)}"
        body = None if value is None else _canonical(value)
        raw = self.firebase._request(  # Reuse the catalog's cached OAuth token.
            url,
            method=method,
            body=body,
            content_type="application/json" if body is not None else None,
            expected=(200, 204),
        )
        return json.loads(raw) if raw else None

    def publish_live(self, state: dict[str, object], *, force_after: float = 60.0) -> bool:
        stable = {"schemaVersion": SCHEMA_VERSION, **state}
        digest = payload_hash(stable)
        now = time.time()
        if digest == self.live_hash and now - self.live_written_at < force_after:
            return False
        self._rtdb("racing/live", "PUT", {**stable, "updatedAt": int(now * 1000)})
        self.live_hash = digest
        self.live_written_at = now
        return True

    def _publish_bounded_event(
        self,
        path: str,
        timestamp_field: str,
        message: dict[str, object],
        limit: int,
    ) -> str:
        now_ms = int(time.time() * 1000)
        nonce = hashlib.sha256(_canonical([now_ms, message, time.monotonic_ns()])).hexdigest()[:12]
        key = f"{now_ms:013d}-{nonce}"
        self._rtdb(f"{path}/{key}", "PUT", {
            "schemaVersion": SCHEMA_VERSION,
            "id": key,
            timestamp_field: now_ms,
            **message,
        })
        self.event_writes[path] += 1
        now = time.monotonic()
        should_prune = (
            path not in self.event_pruned_at
            or self.event_writes[path] >= EVENT_PRUNE_INTERVAL
            or now - self.event_pruned_at[path] >= EVENT_PRUNE_SECONDS
        )
        if should_prune:
            # Both servers publish directly. A shallow key read keeps retention
            # globally bounded without downloading event bodies. Pruning on the
            # first write and then in batches avoids one read per chat/finish.
            existing = self._rtdb(path, "GET", query={"shallow": "true"})
            keys = sorted(existing) if isinstance(existing, dict) else []
            for expired_key in keys[:-limit]:
                self._rtdb(f"{path}/{expired_key}", "DELETE")
            self.event_writes[path] = 0
            self.event_pruned_at[path] = now
        return key

    def publish_chat(self, message: dict[str, object]) -> str:
        return self._publish_bounded_event(
            "racing/chat", "sentAt", message, CHAT_LIMIT
        )

    def publish_activity(self, finish: dict[str, object]) -> str:
        return self._publish_bounded_event(
            "racing/activity", "finishedAt", finish, ACTIVITY_LIMIT
        )

    def publish_admin_status(self, server_id: str, state: dict[str, object]) -> None:
        """Publish the private operations snapshot for one server."""
        server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)[:64]
        if not server:
            raise ValueError("admin status requires a server identifier")
        now_ms = int(time.time() * 1000)
        self._rtdb(f"racing/admin/status/{server}", "PUT", {
            "schemaVersion": SCHEMA_VERSION,
            **state,
            "serverId": server,
            "updatedAt": now_ms,
        })

    def publish_admin_console(
        self,
        server_id: str,
        entries: list[dict[str, object]],
    ) -> str:
        """Publish one bounded, admin-only batch of sanitized console lines."""
        server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)[:64]
        if not server:
            raise ValueError("admin console requires a server identifier")
        bounded = []
        for entry in entries[:25]:
            message = str(entry.get("message", ""))[:600]
            if not message:
                continue
            bounded.append({
                "sequence": max(0, int(entry.get("sequence", 0))),
                "at": max(0, int(entry.get("at", 0))),
                "message": message,
            })
        if not bounded:
            raise ValueError("admin console batch is empty")
        return self._publish_bounded_event(
            f"racing/admin/console/{server}",
            "publishedAt",
            {"serverId": server, "entries": bounded},
            ADMIN_CONSOLE_BATCH_LIMIT,
        )

    def publish_admin_audit(
        self,
        server_id: str,
        event: dict[str, object],
    ) -> str:
        """Publish one bounded private player/audit event.

        Only an explicit field allowlist crosses this boundary.  In particular,
        intercepted command arguments and server credentials must never be
        copied into the browser-readable audit stream.
        """
        server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)[:64]
        if not server:
            raise ValueError("admin audit event requires a server identifier")
        text_fields = {
            "action": 48,
            "region": 16,
            "playerId": 64,
            "logName": 128,
            "displayName": 128,
            "previousName": 128,
            "authName": 128,
            "websiteUid": 128,
            "websiteName": 80,
            "ipAddress": 128,
            "message": 512,
            "command": 64,
            "target": 128,
            "result": 512,
            "mapKey": 1024,
            "mapName": 128,
            "queuedBy": 128,
            "queuedVia": 32,
        }
        payload: dict[str, object] = {
            "source": "server",
            "serverId": server,
        }
        for field, maximum in text_fields.items():
            value = str(event.get(field, "")).replace("\x00", "").strip()
            if value:
                payload[field] = value[:maximum]
        for field in ("active", "authenticated", "personalBest", "queued"):
            if field in event:
                payload[field] = bool(event[field])
        for field in ("seconds", "turns", "rank"):
            value = event.get(field)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                payload[field] = value
        if not payload.get("action"):
            raise ValueError("admin audit event requires an action")
        return self._publish_bounded_event(
            "racing/admin/audit/events",
            "occurredAt",
            payload,
            ADMIN_AUDIT_LIMIT,
        )

    def queued_admin_commands(
        self,
        server_id: str,
        limit: int = ADMIN_COMMAND_LIMIT,
    ) -> list[tuple[str, dict[str, object]]]:
        """Read only the bounded queued-command index for one server."""
        server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)[:64]
        if not server:
            return []
        result = self._rtdb(
            f"racing/admin/commands/{server}",
            "GET",
            query={
                "orderBy": json.dumps("state"),
                "equalTo": json.dumps("queued"),
                "limitToFirst": str(max(1, min(int(limit), ADMIN_COMMAND_LIMIT))),
            },
        )
        if not isinstance(result, dict):
            return []
        commands = [
            (str(command_id), command)
            for command_id, command in result.items()
            if isinstance(command, dict) and command.get("state") == "queued"
        ]
        return sorted(commands, key=lambda item: (
            int(item[1].get("requestedAt", 0) or 0), item[0]
        ))[:ADMIN_COMMAND_LIMIT]

    def update_admin_command(
        self,
        server_id: str,
        command_id: str,
        state: str,
        *,
        result: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)[:64]
        command = re.sub(r"[^A-Za-z0-9_-]", "_", command_id)[:128]
        if not server or not command or state not in {
            "running", "succeeded", "failed", "expired"
        }:
            raise ValueError("invalid admin command update")
        now_ms = int(time.time() * 1000)
        payload: dict[str, object] = {
            "state": state,
            "result": str(result)[:1000],
            "updatedAt": now_ms,
        }
        if state == "running":
            payload["startedAt"] = now_ms
        else:
            payload["completedAt"] = now_ms
        if details:
            payload["details"] = {
                str(key)[:64]: value
                for key, value in details.items()
                if isinstance(value, (bool, int, float, str))
            }
        self._rtdb(
            f"racing/admin/commands/{server}/{command}",
            "PATCH",
            payload,
        )

    def prune_admin_commands(
        self,
        server_id: str,
        keep: int = ADMIN_HISTORY_LIMIT,
    ) -> int:
        """Retain a bounded audit window without downloading command bodies."""
        server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)[:64]
        if not server:
            return 0
        existing = self._rtdb(
            f"racing/admin/commands/{server}",
            "GET",
            query={"shallow": "true"},
        )
        keys = sorted(existing) if isinstance(existing, dict) else []
        expired = keys[:-max(25, min(int(keep), ADMIN_HISTORY_LIMIT))]
        for command_id in expired:
            self._rtdb(
                f"racing/admin/commands/{server}/{command_id}",
                "DELETE",
            )
        return len(expired)

    def publish_leaderboards(
        self,
        rows: list[dict[str, object]],
        maps_by_record_key: dict[str, dict[str, object]],
    ) -> int:
        writes = 0
        if not self.map_catalog and callable(getattr(self.firebase, "list_documents", None)):
            try:
                for existing in self.firebase.list_documents("racingLeaderboards"):
                    map_key = str(existing.get("mapKey", ""))
                    if not map_key:
                        continue
                    self.map_catalog[map_key] = {
                        "mapKey": map_key,
                        "mapId": str(existing.get("mapId", "")),
                        "name": str(existing.get("name", "")),
                        "author": str(existing.get("author", "")),
                        "version": str(existing.get("version", "")),
                        "storagePath": str(existing.get("storagePath", "")),
                        "leaderboardId": leaderboard_document_id(map_key),
                        "entryCount": max(0, int(existing.get("entryCount", 0) or 0)),
                    }
            except Exception:
                # Catalog discovery is a one-time compatibility enhancement;
                # current and future versions still publish without it.
                pass
        grouped: dict[str, list[dict[str, object]]] = collections.defaultdict(list)
        for row in rows:
            grouped[str(row["mapKey"])].append(row)
        for map_key, map_rows in grouped.items():
            metadata = maps_by_record_key.get(map_key) or self.map_catalog.get(map_key)
            metadata = {**inferred_map_metadata(map_key), **(metadata or {})}
            entries = map_leaderboard(map_key, map_rows)
            stable = {
                "schemaVersion": SCHEMA_VERSION,
                "mapKey": map_key,
                "mapId": str(metadata.get("mapId", "")),
                "name": str(metadata.get("name", "")),
                "author": str(metadata.get("author", "")),
                "version": str(metadata.get("version", "")),
                "storagePath": str(metadata.get("storagePath", "")),
                "ratingKey": str(metadata.get("ratingKey", "")),
                **map_rating_fields(metadata),
                "entryCount": len(map_rows),
                "entries": entries,
            }
            digest = payload_hash(stable)
            document_id = leaderboard_document_id(map_key)
            self.map_catalog[map_key] = {
                "mapKey": map_key,
                "mapId": stable["mapId"],
                "name": stable["name"],
                "author": stable["author"],
                "version": stable["version"],
                "storagePath": stable["storagePath"],
                "ratingKey": stable["ratingKey"],
                "rating": stable["rating"],
                "ratingCount": stable["ratingCount"],
                "leaderboardId": document_id,
                "entryCount": len(map_rows),
                "record": entries[0] if entries else None,
            }
            if self.leaderboard_hashes.get(document_id) == digest:
                continue
            self.firebase.set_document("racingLeaderboards", document_id, {
                **stable,
                "payloadHash": digest,
                "updatedAt": FirestoreTimestamp(
                    dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
                ),
            })
            self.leaderboard_hashes[document_id] = digest
            writes += 1

        # A map can receive ratings before anyone finishes it. Keep every
        # active repository map in the bounded catalog even when it has no
        # leaderboard rows yet, so repository clients can still show its vote
        # state without scanning another collection.
        for map_key, supplied_metadata in maps_by_record_key.items():
            if map_key in grouped:
                continue
            metadata = {**inferred_map_metadata(map_key), **supplied_metadata}
            self.map_catalog[map_key] = {
                "mapKey": map_key,
                "mapId": str(metadata.get("mapId", "")),
                "name": str(metadata.get("name", "")),
                "author": str(metadata.get("author", "")),
                "version": str(metadata.get("version", "")),
                "storagePath": str(metadata.get("storagePath", "")),
                "ratingKey": str(metadata.get("ratingKey", "")),
                **map_rating_fields(metadata),
                "leaderboardId": leaderboard_document_id(map_key),
                "entryCount": 0,
                "record": None,
            }

        overall = overall_leaderboard(rows)
        stable_overall = {
            "schemaVersion": SCHEMA_VERSION,
            "scoring": "Top 100 on each map earn 100 points down to 1 point.",
            "entries": overall,
            "entryCount": len(overall),
            "mapCount": len(grouped),
        }
        overall_hash = payload_hash(stable_overall)
        if self.leaderboard_hashes.get("overall") != overall_hash:
            self.firebase.set_document("racingOverall", "current", {
                **stable_overall,
                "payloadHash": overall_hash,
                "updatedAt": FirestoreTimestamp(
                    dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
                ),
            })
            self.leaderboard_hashes["overall"] = overall_hash
            writes += 1

        catalog = sorted(
            self.map_catalog.values(),
            key=lambda item: (
                str(item.get("name", "")).casefold(),
                str(item.get("author", "")).casefold(),
                str(item.get("version", "")),
                str(item.get("mapKey", "")),
            ),
        )
        stable_catalog = {
            "schemaVersion": SCHEMA_VERSION,
            "entryCount": len(catalog),
            "maps": catalog,
        }
        catalog_hash = payload_hash(stable_catalog)
        if self.leaderboard_hashes.get("catalog") != catalog_hash:
            self.firebase.set_document("racingCatalog", "current", {
                **stable_catalog,
                "payloadHash": catalog_hash,
                "updatedAt": FirestoreTimestamp(
                    dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
                ),
            })
            self.leaderboard_hashes["catalog"] = catalog_hash
            writes += 1

        profiles: dict[str, dict[str, object]] = {}
        for map_key in sorted(grouped):
            metadata = self.map_catalog.get(map_key) or {
                "mapKey": map_key,
                **inferred_map_metadata(map_key),
            }
            ranked_rows = sorted(
                grouped[map_key],
                key=lambda row: (
                    float(row["bestSeconds"]),
                    math.inf if row.get("bestTurns") is None else int(row["bestTurns"]),
                    float(row["achievedAt"]),
                ),
            )
            for rank, row in enumerate(ranked_rows, 1):
                if not row.get("authenticated"):
                    continue
                player_id = public_player_id(str(row["identityKey"]))
                profile = profiles.setdefault(player_id, {
                    "schemaVersion": SCHEMA_VERSION,
                    "playerId": player_id,
                    "name": str(row["username"])[:128],
                    "maps": [],
                })
                profile["name"] = str(row["username"])[:128]
                profile["maps"].append({
                    "mapKey": map_key,
                    "mapId": str(metadata.get("mapId", "")),
                    "name": str(metadata.get("name", "")),
                    "author": str(metadata.get("author", "")),
                    "version": str(metadata.get("version", "")),
                    "rank": rank,
                    "seconds": round(float(row["bestSeconds"]), 6),
                    "turns": row.get("bestTurns"),
                    "achievedAt": int(float(row["achievedAt"]) * 1000),
                })
        overall_by_player = {str(entry["playerId"]): entry for entry in overall}
        active_profile_ids = set()
        for player_id, stable_profile in profiles.items():
            overall_entry = overall_by_player.get(player_id, {})
            stable_profile["overall"] = {
                key: overall_entry.get(key, 0)
                for key in ("rank", "points", "maps", "wins")
            }
            stable_profile["maps"].sort(key=lambda item: (
                int(item["rank"]), str(item["name"]).casefold(), str(item["mapKey"])
            ))
            profile_hash = payload_hash(stable_profile)
            active_profile_ids.add(player_id)
            if self.profile_hashes.get(player_id) == profile_hash:
                continue
            self.firebase.set_document("racingProfiles", player_id, {
                **stable_profile,
                "payloadHash": profile_hash,
                "updatedAt": FirestoreTimestamp(
                    dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
                ),
            })
            self.profile_hashes[player_id] = profile_hash
            writes += 1
        self.profile_hashes = {
            key: value for key, value in self.profile_hashes.items()
            if key in active_profile_ids
        }
        return writes

    def _publish_settings(self, server_id: str, fingerprint: str, settings: dict) -> str:
        path = settings_storage_path(server_id, fingerprint)
        encoded = gzip.compress(_canonical(settings), compresslevel=9, mtime=0)
        self.firebase.upload_immutable_object(
            path,
            encoded,
            {
                "kind": "racing-replay-settings",
                "serverId": server_id,
                "fingerprint": fingerprint,
            },
            content_type="application/json; charset=UTF-8",
            content_encoding="gzip",
            allow_existing=True,
        )
        return path

    def publish_replay_batch(self, server_id: str, limit: int = 40) -> int:
        """Publish new finished runs and exact-read per-player map histories."""
        cursor_key = f"live_dashboard_replay_cursor_{server_id}"
        cursor = int(self.store.get_json(cursor_key, 0) or 0)
        rows = self.store.dashboard_finished_replays_after(cursor, limit)
        if not rows:
            return 0
        affected: dict[tuple[str, str], dict[str, object]] = {}
        published = 0
        for row in rows:
            payload = self.store.dashboard_replay_payload(int(row["runId"]))
            if not payload:
                continue
            fingerprints = {
                str(payload.get("settingsFingerprint", "")),
                *(
                    str(transition[1])
                    for transition in payload.get("settingsTransitions", [])
                    if isinstance(transition, list) and len(transition) == 2
                ),
            } - {""}
            settings_paths = {}
            for fingerprint in sorted(fingerprints):
                settings = self.store.dashboard_replay_settings_by_fingerprint(fingerprint)
                if settings:
                    settings_paths[fingerprint] = self._publish_settings(
                        server_id, fingerprint, settings
                    )
            payload["serverId"] = server_id
            payload["settingsPaths"] = settings_paths
            path = replay_storage_path(server_id, int(row["runId"]))
            encoded = gzip.compress(_canonical(payload), compresslevel=9, mtime=0)
            self.firebase.upload_immutable_object(
                path,
                encoded,
                {
                    "kind": "racing-replay",
                    "serverId": server_id,
                    "runId": row["runId"],
                    "mapKeySha256": hashlib.sha256(
                        str(row["mapKey"]).encode("utf-8")
                    ).hexdigest(),
                    "playerId": row["playerId"],
                },
                content_type="application/json; charset=UTF-8",
                content_encoding="gzip",
                allow_existing=True,
            )
            affected[(str(row["identityKey"]), str(row["mapKey"]))] = row
            published += 1

        now = FirestoreTimestamp(
            dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        )
        for (identity_key, map_key), row in affected.items():
            history = self.store.dashboard_player_map_history(
                identity_key, map_key, HISTORY_ENTRY_LIMIT
            )
            entries = [
                {
                    key: value for key, value in entry.items()
                    if key != "settingsRef"
                } | {
                    "replayPath": replay_storage_path(server_id, int(entry["runId"]))
                }
                for entry in history
            ]
            player_id = str(row["playerId"])
            document_id = history_document_id(map_key, player_id, server_id)
            stable = {
                "schemaVersion": SCHEMA_VERSION,
                "serverId": server_id,
                "mapKey": map_key,
                "mapId": str(row["mapId"]),
                "revisionId": str(row["revisionId"]),
                "playerId": player_id,
                "name": str(row["username"])[:128],
                "authenticated": bool(row["authenticated"]),
                "entryCount": len(entries),
                "entries": entries,
            }
            self.firebase.set_document("racingRunHistories", document_id, {
                **stable,
                "payloadHash": payload_hash(stable),
                "updatedAt": now,
            })

        self.store.set_json(cursor_key, max(int(row["runId"]) for row in rows))
        return published
