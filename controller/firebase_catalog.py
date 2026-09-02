"""Firebase-backed immutable map catalog for Tronner Racing."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


MAP_SUFFIX = ".aamap.xml"


class FirebaseCatalogError(RuntimeError):
    """Raised when a Firebase catalog operation cannot be completed safely."""


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _resource_id(resource_path: str) -> str:
    normalized = unicodedata.normalize("NFKC", resource_path)
    return f"resource_{_base64url(normalized.encode('utf-8'))}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class FirestoreTimestamp(str):
    """JSON-compatible marker that must remain a Firestore timestamp."""


def _timestamp() -> FirestoreTimestamp:
    return FirestoreTimestamp(
        dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _firestore_value(value: Any) -> dict:
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, FirestoreTimestamp):
        return {"timestampValue": str(value)}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (tuple, list)):
        return {"arrayValue": {"values": [_firestore_value(item) for item in value]}}
    if isinstance(value, dict):
        return {
            "mapValue": {
                "fields": {
                    str(key): _firestore_value(item)
                    for key, item in value.items()
                }
            }
        }
    raise TypeError(f"unsupported Firestore value: {type(value).__name__}")


def _decode_firestore_value(value: dict) -> Any:
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "stringValue" in value:
        return str(value["stringValue"])
    if "timestampValue" in value:
        return FirestoreTimestamp(value["timestampValue"])
    if "arrayValue" in value:
        return [
            _decode_firestore_value(item)
            for item in value["arrayValue"].get("values", [])
        ]
    if "mapValue" in value:
        return {
            key: _decode_firestore_value(item)
            for key, item in value["mapValue"].get("fields", {}).items()
        }
    raise FirebaseCatalogError("unknown Firestore value type")


def _decode_document(document: dict) -> dict:
    data = {
        key: _decode_firestore_value(value)
        for key, value in document.get("fields", {}).items()
    }
    data["_name"] = document.get("name", "")
    data["_id"] = data["_name"].rsplit("/", 1)[-1]
    data["_update_time"] = document.get("updateTime", "")
    return data


def _document(project: str, collection: str, document_id: str, data: dict) -> dict:
    return {
        "name": (
            f"projects/{project}/databases/(default)/documents/"
            f"{collection}/{document_id}"
        ),
        "fields": {
            key: _firestore_value(value)
            for key, value in data.items()
            if not key.startswith("_")
        },
    }


class ServiceAccountTokenProvider:
    """Small service-account OAuth provider with no ambient SDK dependency."""

    def __init__(self, credential_file: Path):
        try:
            self.credentials = json.loads(credential_file.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FirebaseCatalogError(
                f"unable to read Firebase service account {credential_file}: {exc}"
            ) from exc
        for key in ("client_email", "private_key"):
            if not self.credentials.get(key):
                raise FirebaseCatalogError(f"service account is missing {key}")
        self.token_uri = self.credentials.get(
            "token_uri", "https://oauth2.googleapis.com/token"
        )
        self._access_token = ""
        self._expires_at = 0.0

    def access_token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        now = int(time.time())
        header = _base64url(json.dumps(
            {"alg": "RS256", "typ": "JWT"}, separators=(",", ":")
        ).encode())
        claims = _base64url(json.dumps(
            {
                "iss": self.credentials["client_email"],
                "scope": " ".join(
                    (
                        "https://www.googleapis.com/auth/cloud-platform",
                        "https://www.googleapis.com/auth/firebase.database",
                        "https://www.googleapis.com/auth/userinfo.email",
                    )
                ),
                "aud": self.token_uri,
                "iat": now,
                "exp": now + 3600,
            },
            separators=(",", ":"),
        ).encode())
        signing_input = f"{header}.{claims}".encode("ascii")
        private_key = serialization.load_pem_private_key(
            self.credentials["private_key"].encode("ascii"), password=None
        )
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        assertion = f"{header}.{claims}.{_base64url(signature)}"
        body = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
        ).encode("ascii")
        request = urllib.request.Request(
            self.token_uri,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.load(response)
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise FirebaseCatalogError(f"service account token exchange failed: {exc}") from exc
        self._access_token = str(result["access_token"])
        self._expires_at = time.time() + int(result.get("expires_in", 3600))
        return self._access_token


class FirebaseCatalogClient:
    def __init__(self, config: dict):
        self.project = str(config["firebase_project_id"])
        self.bucket = str(config["firebase_storage_bucket"])
        credential_file = Path(config["firebase_service_account_file"])
        self.tokens = ServiceAccountTokenProvider(credential_file)
        self.server_id = str(config.get("firebase_server_id", "tronner-racing"))
        self.timeout = float(config.get("firebase_request_timeout_seconds", 20))

    @property
    def firestore_root(self) -> str:
        return (
            f"https://firestore.googleapis.com/v1/projects/{self.project}/"
            "databases/(default)/documents"
        )

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        content_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> bytes:
        headers = {"Authorization": f"Bearer {self.tokens.access_token()}"}
        if content_type:
            headers["Content-Type"] = content_type
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status not in expected:
                    raise FirebaseCatalogError(
                        f"Firebase request returned HTTP {response.status}"
                    )
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise FirebaseCatalogError(
                f"Firebase {method} failed ({exc.code}): {detail}"
            ) from exc
        except OSError as exc:
            raise FirebaseCatalogError(f"Firebase {method} failed: {exc}") from exc

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict | None = None,
    ) -> dict:
        body = None if data is None else json.dumps(data, separators=(",", ":")).encode()
        raw = self._request(
            url,
            method=method,
            body=body,
            content_type="application/json" if body is not None else None,
        )
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise FirebaseCatalogError("Firebase returned malformed JSON") from exc

    def get_document(self, collection: str, document_id: str) -> dict:
        encoded_id = urllib.parse.quote(document_id, safe="")
        return _decode_document(
            self._request_json(f"{self.firestore_root}/{collection}/{encoded_id}")
        )

    def set_document(self, collection: str, document_id: str, data: dict) -> dict:
        """Create or replace one trusted-server Firestore document."""
        encoded_id = urllib.parse.quote(document_id, safe="")
        return _decode_document(self._request_json(
            f"{self.firestore_root}/{collection}/{encoded_id}",
            method="PATCH",
            data=_document(self.project, collection, document_id, data),
        ))

    def list_documents(self, collection: str) -> list[dict]:
        page_token = ""
        documents: list[dict] = []
        while True:
            query = {"pageSize": "300"}
            if page_token:
                query["pageToken"] = page_token
            url = f"{self.firestore_root}/{collection}?{urllib.parse.urlencode(query)}"
            response = self._request_json(url)
            documents.extend(
                _decode_document(document)
                for document in response.get("documents", [])
            )
            page_token = response.get("nextPageToken", "")
            if not page_token:
                return documents

    def query_documents(self, collection: str, field: str, value: Any) -> list[dict]:
        """Run a single-field equality query without scanning a collection."""
        response = self._request_json(
            f"{self.firestore_root}:runQuery",
            method="POST",
            data={
                "structuredQuery": {
                    "from": [{"collectionId": collection}],
                    "where": {
                        "fieldFilter": {
                            "field": {"fieldPath": field},
                            "op": "EQUAL",
                            "value": _firestore_value(value),
                        }
                    },
                }
            },
        )
        if not isinstance(response, list):
            raise FirebaseCatalogError("Firestore query returned malformed JSON")
        return [
            _decode_document(item["document"])
            for item in response
            if isinstance(item, dict) and isinstance(item.get("document"), dict)
        ]

    def get_catalog_state(self) -> dict:
        """Return the tiny invalidation document watched by game servers."""
        return self.get_document("catalogState", "current")

    def _manifest_maps(self, state: dict) -> tuple[list[dict], str]:
        storage_path = str(state.get("serverManifestPath") or "")
        expected_sha256 = str(state.get("serverManifestSha256") or "")
        if not storage_path or not expected_sha256:
            raise FirebaseCatalogError("catalog state has no server manifest")
        raw = self.download_object(storage_path, accept_gzip=True)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise FirebaseCatalogError("catalog manifest checksum mismatch")
        try:
            decoded = gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw
            manifest = json.loads(decoded)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FirebaseCatalogError("catalog manifest is malformed") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("maps"), list):
            raise FirebaseCatalogError("catalog manifest contains no map list")
        generation = str(manifest.get("generation") or state.get("generation") or "")
        if not generation or generation != str(state.get("generation") or ""):
            raise FirebaseCatalogError("catalog manifest generation mismatch")
        maps = []
        for item in manifest["maps"]:
            if not isinstance(item, dict):
                raise FirebaseCatalogError("catalog manifest contains a malformed map")
            document = dict(item)
            document.setdefault("_id", document.get("mapId"))
            maps.append(document)
        return maps, generation

    def download_object(self, storage_path: str, *, accept_gzip: bool = False) -> bytes:
        encoded_bucket = urllib.parse.quote(self.bucket, safe="")
        encoded_object = urllib.parse.quote(storage_path, safe="")
        return self._request(
            "https://storage.googleapis.com/download/storage/v1/b/"
            f"{encoded_bucket}/o/{encoded_object}?alt=media",
            extra_headers={"Accept-Encoding": "gzip"} if accept_gzip else None,
        )

    @staticmethod
    def _validate_map_bytes(data: bytes, expected: dict) -> None:
        try:
            resource = ET.fromstring(data)
        except ET.ParseError as exc:
            raise FirebaseCatalogError(
                f"{expected.get('resourcePath')}: malformed XML: {exc}"
            ) from exc
        if _local_name(resource.tag) != "Resource":
            resource = next(
                (node for node in resource.iter() if _local_name(node.tag) == "Resource"),
                None,
            )
        if resource is None:
            raise FirebaseCatalogError("map has no Resource element")
        identity = {
            "authorName": resource.attrib.get("author", "").strip(),
            "category": resource.attrib.get("category", "").strip("/"),
            "mapName": resource.attrib.get("name", "").strip(),
            "mapVersion": resource.attrib.get("version", "").strip(),
        }
        for key, value in identity.items():
            if value != expected.get(key):
                raise FirebaseCatalogError(
                    f"{expected.get('resourcePath')}: XML {key} does not match catalog"
                )
        key = "/".join(
            [
                identity["authorName"],
                *identity["category"].split("/"),
                f"{identity['mapName']}-{identity['mapVersion']}{MAP_SUFFIX}",
            ]
        )
        if key != expected.get("resourcePath"):
            raise FirebaseCatalogError(f"resource path mismatch: {key}")
        if not any(_local_name(node.tag) == "Spawn" for node in resource.iter()):
            raise FirebaseCatalogError(f"{key}: map has no spawn points")

    def sync_snapshot(
        self,
        root: Path,
        *,
        require_ready: bool = True,
        catalog_state: dict | None = None,
        force_firestore: bool = False,
    ) -> dict:
        settings = self.get_document("catalogSettings", "current")
        if require_ready and settings.get("ready") is not True:
            raise FirebaseCatalogError("Firebase catalog is not marked ready")
        state = catalog_state or self.get_catalog_state()
        if state.get("serverManifestPath") and not force_firestore:
            maps, generation = self._manifest_maps(state)
        else:
            # Backward-compatible bootstrap path for catalogs created before
            # versioned manifests were deployed.
            maps = self.list_documents("maps")
            generation = ""
        maps = [
            document
            for document in maps
            if document.get("status") in {"active", "inactive"}
        ]
        if not maps:
            raise FirebaseCatalogError("Firebase catalog contains no maps")
        required = {
            "mapId", "status", "authorId", "authorName", "category", "mapName",
            "mapVersion", "activeRevisionId", "storagePath", "resourcePath", "sha256",
        }
        for document in maps:
            missing = required - document.keys()
            if missing:
                raise FirebaseCatalogError(
                    f"map {document.get('_id')} is missing {', '.join(sorted(missing))}"
                )
            if document["mapId"] != document["_id"]:
                raise FirebaseCatalogError("map document ID does not match mapId")
            resource = Path(document["resourcePath"])
            if resource.is_absolute() or ".." in resource.parts or len(resource.parts) < 3:
                raise FirebaseCatalogError(f"unsafe resource path {document['resourcePath']!r}")

        if not generation:
            digest = hashlib.sha256()
            digest.update(b"catalog-ready\0")
            digest.update(b"1" if settings.get("ready") is True else b"0")
            for document in sorted(maps, key=lambda item: item["mapId"]):
                digest.update(json.dumps(
                    {
                        key: document.get(key)
                        for key in sorted(required | {"recordKey", "ratingKey"})
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode())
            generation = digest.hexdigest()[:24]
        snapshots = root / "snapshots"
        snapshot = snapshots / generation
        manifest_path = snapshot / ".catalog.json"
        if not manifest_path.is_file():
            try:
                previous_root = (root / "current").resolve(strict=True)
            except OSError:
                previous_root = root / ".no-current-snapshot"
            previous_maps: dict[str, dict] = {}
            try:
                previous_manifest = json.loads(
                    (previous_root / ".catalog.json").read_text("utf-8")
                )
                previous_maps = {
                    str(item.get("mapId")): item
                    for item in previous_manifest.get("maps", [])
                    if isinstance(item, dict) and item.get("mapId")
                }
            except (OSError, json.JSONDecodeError):
                pass
            temporary = snapshots / f".{generation}.{os.getpid()}.tmp"
            temporary.mkdir(parents=True, exist_ok=False)
            try:
                for document in maps:
                    destination = temporary / document["resourcePath"]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    previous = previous_maps.get(str(document["mapId"]))
                    previous_path = (
                        previous_root / str(previous.get("resourcePath", ""))
                        if previous
                        else None
                    )
                    reusable = bool(
                        previous
                        and previous.get("storagePath") == document["storagePath"]
                        and previous.get("resourcePath") == document["resourcePath"]
                        and previous.get("sha256") == document["sha256"]
                        and previous_path
                        and previous_path.is_file()
                    )
                    data = previous_path.read_bytes() if reusable else self.download_object(
                        document["storagePath"]
                    )
                    if hashlib.sha256(data).hexdigest() != document["sha256"]:
                        raise FirebaseCatalogError(
                            f"checksum mismatch for {document['resourcePath']}"
                        )
                    self._validate_map_bytes(data, document)
                    if reusable:
                        try:
                            os.link(previous_path, destination)
                        except OSError:
                            with destination.open("xb") as handle:
                                handle.write(data)
                                handle.flush()
                                os.fsync(handle.fileno())
                    else:
                        with destination.open("xb") as handle:
                            handle.write(data)
                            handle.flush()
                            os.fsync(handle.fileno())
                manifest = {
                    "schemaVersion": 2 if state.get("serverManifestPath") else 1,
                    "generation": generation,
                    "ready": settings.get("ready") is True,
                    "settingsUpdatedAt": settings.get("updatedAt"),
                    "catalogVersion": state.get("catalogVersion"),
                    "sourceManifestSha256": state.get("serverManifestSha256"),
                    "maps": [
                        {
                            key: document.get(key)
                            for key in (
                                "mapId", "status", "authorId", "authorName", "category",
                                "mapName", "mapVersion", "activeRevisionId", "storagePath",
                                "resourcePath", "recordKey", "ratingKey", "sha256",
                            )
                        }
                        for document in maps
                    ],
                }
                with (temporary / ".catalog.json").open("x", encoding="utf-8") as handle:
                    json.dump(manifest, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, snapshot)
            except Exception:
                # The incomplete directory is never linked as current. Keep it
                # for forensic inspection; a new PID uses a different path.
                raise
        manifest = json.loads(manifest_path.read_text("utf-8"))
        root.mkdir(parents=True, exist_ok=True)
        next_link = root / f".current.{os.getpid()}.tmp"
        with __import__("contextlib").suppress(FileNotFoundError):
            next_link.unlink()
        next_link.symlink_to(Path("snapshots") / generation)
        os.replace(next_link, root / "current")
        return manifest

    def publish_server_catalog_state(
        self,
        *,
        catalog_state: dict,
        generation: str,
        map_count: int,
    ) -> None:
        """Acknowledge a catalog generation only after it is locally usable."""
        data = {
            "serverId": self.server_id,
            "status": "ready",
            "appliedCatalogVersion": int(catalog_state.get("catalogVersion") or 0),
            "appliedGeneration": generation,
            "mapCount": int(map_count),
            "updatedAt": _timestamp(),
        }
        self._commit([
            self._update_write(_document(
                self.project,
                "serverCatalogState",
                self.server_id,
                data,
            ))
        ])

    def upload_immutable_object(
        self,
        storage_path: str,
        data: bytes,
        metadata: dict,
        *,
        content_type: str = "application/octet-stream",
        content_encoding: str = "",
        allow_existing: bool = False,
    ) -> bool:
        """Create one immutable object, returning False when it already exists."""
        boundary = f"tronner_{uuid.uuid4().hex}"
        object_fields = {
            "name": storage_path,
            "contentType": content_type,
            "cacheControl": "public, max-age=31536000, immutable",
            "metadata": {key: str(value) for key, value in metadata.items()},
        }
        if content_encoding:
            object_fields["contentEncoding"] = content_encoding
        object_metadata = json.dumps(
            object_fields,
            separators=(",", ":"),
        ).encode()
        body = b"".join(
            [
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
                object_metadata,
                f"\r\n--{boundary}\r\nContent-Type: {content_type}\r\n\r\n".encode(),
                data,
                f"\r\n--{boundary}--".encode(),
            ]
        )
        bucket = urllib.parse.quote(self.bucket, safe="")
        try:
            self._request(
                "https://storage.googleapis.com/upload/storage/v1/b/"
                f"{bucket}/o?uploadType=multipart&ifGenerationMatch=0",
                method="POST",
                body=body,
                content_type=f"multipart/related; boundary={boundary}",
            )
        except FirebaseCatalogError as exc:
            if allow_existing and "(412)" in str(exc):
                return False
            raise
        return True

    def _upload_object(self, storage_path: str, data: bytes, metadata: dict) -> None:
        self.upload_immutable_object(
            storage_path,
            data,
            metadata,
            content_type="application/xml; charset=UTF-8",
        )

    def _commit(self, writes: list[dict]) -> None:
        self._request_json(
            f"{self.firestore_root}:commit", method="POST", data={"writes": writes}
        )

    @staticmethod
    def _update_write(document: dict, *, update_time: str = "") -> dict:
        write = {
            "update": document,
            "updateMask": {"fieldPaths": sorted(document.get("fields", {}))},
        }
        if update_time:
            write["currentDocument"] = {"updateTime": update_time}
        return write

    @staticmethod
    def _create_write(document: dict) -> dict:
        return {
            "update": document,
            "updateMask": {"fieldPaths": sorted(document.get("fields", {}))},
            "currentDocument": {"exists": False},
        }

    def publish_size_revision(
        self,
        *,
        map_id: str,
        expected_revision_id: str,
        data: bytes,
        identity: dict,
        size_factor: float,
    ) -> dict:
        current = self.get_document("maps", map_id)
        if current.get("status") != "active":
            raise FirebaseCatalogError("cannot revise an inactive map")
        if current.get("activeRevisionId") != expected_revision_id:
            raise FirebaseCatalogError("map changed in Firebase; reload before changing size")
        resource_path = "/".join(
            [
                identity["authorName"],
                *identity["category"].split("/"),
                f"{identity['mapName']}-{identity['mapVersion']}{MAP_SUFFIX}",
            ]
        )
        candidate = {**identity, "resourcePath": resource_path}
        self._validate_map_bytes(data, candidate)
        revision_id = f"server_{uuid.uuid4().hex}"
        storage_path = f"_revisions/server/{revision_id}"
        sha256 = hashlib.sha256(data).hexdigest()
        self._upload_object(
            storage_path,
            data,
            {
                "ownerUid": "server",
                "submissionId": revision_id,
                "authorId": current["authorId"],
                "authorName": identity["authorName"],
                "category": identity["category"],
                "mapName": identity["mapName"],
                "mapVersion": identity["mapVersion"],
                "operation": "size",
                "sha256": sha256,
            },
        )
        now = _timestamp()
        submission = {
            "submissionId": revision_id,
            "mapId": map_id,
            "operation": "size",
            "status": "approved",
            "submittedBy": f"server:{self.server_id}",
            "submittedByName": self.server_id,
            "authorId": current["authorId"],
            **identity,
            "storagePath": storage_path,
            "sourceRevisionId": current["activeRevisionId"],
            "sourceMapId": map_id,
            "sha256": sha256,
            "contentBytes": len(data),
            "sizeFactor": size_factor,
            "createdAt": now,
            "updatedAt": now,
            "reviewedAt": now,
            "reviewedBy": f"server:{self.server_id}",
            "reviewReason": "Approved server /size command",
            "historyVisible": True,
        }
        updated_map = {
            key: value for key, value in current.items() if not key.startswith("_")
        }
        updated_map.update(
            {
                **identity,
                "activeRevisionId": revision_id,
                "storagePath": storage_path,
                "resourcePath": resource_path,
                "previousRevisionId": current["activeRevisionId"],
                "recordKey": resource_path,
                "sha256": sha256,
                "sizeFactor": size_factor,
                "updatedAt": now,
            }
        )
        audit_id = f"server_{uuid.uuid4().hex}"
        audit = {
            "actorUid": f"server:{self.server_id}",
            "actorName": self.server_id,
            "action": "map.size",
            "targetType": "map",
            "targetId": map_id,
            "before": {
                "revisionId": current["activeRevisionId"],
                "resourcePath": current["resourcePath"],
                "sizeFactor": current.get("sizeFactor"),
            },
            "after": {
                "revisionId": revision_id,
                "resourcePath": resource_path,
                "sizeFactor": size_factor,
            },
            "createdAt": now,
        }
        resource_id = _resource_id(resource_path)
        resource_reservation = {
            "resourceId": resource_id,
            "resourcePath": resource_path,
            "mapId": map_id,
            "revisionId": revision_id,
            "createdAt": now,
            "updatedAt": now,
        }
        self._commit(
            [
                self._update_write(_document(
                    self.project, "mapSubmissions", revision_id, submission
                )),
                self._update_write(
                    _document(self.project, "maps", map_id, updated_map),
                    update_time=current["_update_time"],
                ),
                self._create_write(_document(
                    self.project,
                    "resourcePaths",
                    resource_id,
                    resource_reservation,
                )),
                self._update_write(_document(
                    self.project, "auditEvents", audit_id, audit
                )),
            ]
        )
        return {
            "mapId": map_id,
            "revisionId": revision_id,
            "storagePath": storage_path,
            "resourcePath": resource_path,
            "sha256": sha256,
        }

    def set_map_status(self, map_id: str, status: str, reason: str) -> None:
        if status not in {"active", "inactive"}:
            raise ValueError("map status must be active or inactive")
        current = self.get_document("maps", map_id)
        if current.get("status") == status:
            return
        now = _timestamp()
        updated = {key: value for key, value in current.items() if not key.startswith("_")}
        updated.update(
            {
                "status": status,
                "statusReason": reason,
                "statusUpdatedBy": f"server:{self.server_id}",
                "statusUpdatedAt": now,
                "updatedAt": now,
            }
        )
        audit_id = f"server_{uuid.uuid4().hex}"
        audit = {
            "actorUid": f"server:{self.server_id}",
            "actorName": self.server_id,
            "action": "map.activate" if status == "active" else "map.deactivate",
            "targetType": "map",
            "targetId": map_id,
            "reason": reason,
            "before": {"status": current.get("status")},
            "after": {"status": status},
            "createdAt": now,
        }
        self._commit(
            [
                self._update_write(
                    _document(self.project, "maps", map_id, updated),
                    update_time=current["_update_time"],
                ),
                self._update_write(_document(
                    self.project, "auditEvents", audit_id, audit
                )),
            ]
        )

    def list_map_reviews(self) -> list[dict]:
        """Return server-origin reviews that still keep a map out of rotation."""
        candidates = [
            document
            for document in self.query_documents(
                "mapSubmissions", "operation", "server-review"
            )
            if document.get("status") in {"pending", "denied"}
        ]
        maps_by_id = {
            str(document.get("mapId") or document.get("_id") or ""): document
            for document in self.query_documents("maps", "status", "inactive")
        }
        reviews = []
        for review in candidates:
            current = maps_by_id.get(str(review.get("mapId") or ""))
            review_id = str(review.get("_id") or review.get("submissionId") or "")
            # A denied review may remain immutable history after a corrected
            # revision is approved. Only the review still linked from an
            # inactive map belongs in the live server's actionable queue.
            if not current or current.get("status") != "inactive":
                continue
            if str(current.get("reviewSubmissionId") or "") != review_id:
                continue
            reviews.append(review)
        return sorted(
            reviews,
            key=lambda item: (
                str(item.get("mapName", "")).casefold(),
                str(item.get("authorName", "")).casefold(),
                str(item.get("mapVersion", "")).casefold(),
                str(item.get("_id", "")),
            ),
        )

    def submit_excluded_map_reviews(self, exclusions: list[dict]) -> list[dict]:
        """Atomically move locally excluded published maps into Vectron review."""
        if not exclusions:
            return []
        unique_map_ids: set[str] = set()
        prepared: list[dict] = []
        writes: list[dict] = []
        now = _timestamp()
        server_actor = f"server:{self.server_id}"
        for exclusion in exclusions:
            map_id = str(exclusion.get("mapId") or "").strip()
            resource_path = str(exclusion.get("resourcePath") or "").strip()
            reason = str(exclusion.get("reason") or "").strip()
            if not map_id or not resource_path:
                raise FirebaseCatalogError(
                    "each excluded map requires mapId and resourcePath"
                )
            if map_id in unique_map_ids:
                raise FirebaseCatalogError(f"duplicate excluded map: {map_id}")
            unique_map_ids.add(map_id)
            if len(reason) > 1000:
                raise FirebaseCatalogError(
                    f"exclusion reason for {resource_path} exceeds 1,000 characters"
                )
            current = self.get_document("maps", map_id)
            if current.get("resourcePath") != resource_path:
                raise FirebaseCatalogError(
                    f"catalog resource path changed for {resource_path}"
                )
            if current.get("status") not in {"active", "inactive"}:
                raise FirebaseCatalogError(
                    f"map {resource_path} is not a published catalog map"
                )
            linked_review_id = str(current.get("reviewSubmissionId") or "")
            if linked_review_id:
                review = self.get_document("mapSubmissions", linked_review_id)
                if (
                    review.get("operation") != "server-review"
                    or review.get("status") not in {"pending", "denied"}
                    or review.get("mapId") != map_id
                    or review.get("sourceResourcePath") != resource_path
                ):
                    raise FirebaseCatalogError(
                        f"map {resource_path} is linked to an incompatible review"
                    )
                prepared.append(review)
                continue

            review_reason = reason or "Moved from the server exclusion list for Vectron review"
            review_id = f"server_review_{uuid.uuid4().hex}"
            submission = {
                "submissionId": review_id,
                "mapId": map_id,
                "operation": "server-review",
                "status": "pending",
                "submittedBy": server_actor,
                "submittedByName": self.server_id,
                "authorId": current["authorId"],
                "authorName": current["authorName"],
                "category": current["category"],
                "mapName": current["mapName"],
                "mapVersion": current["mapVersion"],
                "storagePath": current["storagePath"],
                "sourceRevisionId": current["activeRevisionId"],
                "sourceMapId": map_id,
                "sourceResourcePath": resource_path,
                "sha256": current["sha256"],
                "submissionReason": review_reason,
                "createdAt": now,
                "updatedAt": now,
            }
            updated_map = {
                key: value
                for key, value in current.items()
                if not key.startswith("_")
            }
            updated_map.update(
                {
                    "status": "inactive",
                    "statusReason": review_reason,
                    "statusUpdatedBy": server_actor,
                    "statusUpdatedAt": now,
                    "reviewSubmissionId": review_id,
                    "updatedAt": now,
                }
            )
            audit_id = f"server_{uuid.uuid4().hex}"
            audit = {
                "actorUid": server_actor,
                "actorName": self.server_id,
                "action": "map.review.submit",
                "targetType": "mapSubmission",
                "targetId": review_id,
                "mapId": map_id,
                "reason": review_reason,
                "before": {
                    "status": current.get("status"),
                    "excluded": True,
                },
                "after": {"status": "inactive", "reviewStatus": "pending"},
                "createdAt": now,
            }
            writes.extend(
                [
                    self._create_write(
                        _document(
                            self.project,
                            "mapSubmissions",
                            review_id,
                            submission,
                        )
                    ),
                    self._update_write(
                        _document(self.project, "maps", map_id, updated_map),
                        update_time=current["_update_time"],
                    ),
                    self._create_write(
                        _document(self.project, "auditEvents", audit_id, audit)
                    ),
                ]
            )
            prepared.append({**submission, "_id": review_id})
        if len(writes) > 500:
            raise FirebaseCatalogError(
                "excluded-map migration exceeds Firestore's 500-write commit limit"
            )
        if writes:
            self._commit(writes)
        return prepared

    def submit_map_review(self, map_id: str, reason: str) -> dict:
        """Atomically create a Vectron review and deactivate its published map."""
        current = self.get_document("maps", map_id)
        if current.get("status") != "active":
            raise FirebaseCatalogError("only an active map can be submitted for review")
        existing = next(
            (
                item
                for item in self.list_map_reviews()
                if item.get("mapId") == map_id
            ),
            None,
        )
        if existing:
            raise FirebaseCatalogError("this map is already in the review list")

        now = _timestamp()
        review_id = f"server_review_{uuid.uuid4().hex}"
        server_actor = f"server:{self.server_id}"
        submission = {
            "submissionId": review_id,
            "mapId": map_id,
            "operation": "server-review",
            "status": "pending",
            "submittedBy": server_actor,
            "submittedByName": self.server_id,
            "authorId": current["authorId"],
            "authorName": current["authorName"],
            "category": current["category"],
            "mapName": current["mapName"],
            "mapVersion": current["mapVersion"],
            "storagePath": current["storagePath"],
            "sourceRevisionId": current["activeRevisionId"],
            "sourceMapId": map_id,
            "sourceResourcePath": current["resourcePath"],
            "sha256": current["sha256"],
            "submissionReason": reason,
            "createdAt": now,
            "updatedAt": now,
        }
        updated_map = {
            key: value for key, value in current.items() if not key.startswith("_")
        }
        updated_map.update(
            {
                "status": "inactive",
                "statusReason": reason,
                "statusUpdatedBy": server_actor,
                "statusUpdatedAt": now,
                "reviewSubmissionId": review_id,
                "updatedAt": now,
            }
        )
        audit_id = f"server_{uuid.uuid4().hex}"
        audit = {
            "actorUid": server_actor,
            "actorName": self.server_id,
            "action": "map.review.submit",
            "targetType": "mapSubmission",
            "targetId": review_id,
            "mapId": map_id,
            "reason": reason,
            "before": {"status": current.get("status")},
            "after": {"status": "inactive", "reviewStatus": "pending"},
            "createdAt": now,
        }
        self._commit(
            [
                self._create_write(_document(
                    self.project, "mapSubmissions", review_id, submission
                )),
                self._update_write(
                    _document(self.project, "maps", map_id, updated_map),
                    update_time=current["_update_time"],
                ),
                self._update_write(_document(
                    self.project, "auditEvents", audit_id, audit
                )),
            ]
        )
        return {**submission, "_id": review_id}

    def cancel_map_review(self, review_id: str, reason: str) -> dict:
        """Cancel one server review and restore its unchanged source revision."""
        review = self.get_document("mapSubmissions", review_id)
        if review.get("operation") != "server-review" or review.get("status") not in {
            "pending",
            "denied",
        }:
            raise FirebaseCatalogError("that server review is no longer removable")
        map_id = str(review.get("mapId", ""))
        current = self.get_document("maps", map_id)
        if current.get("activeRevisionId") != review.get("sourceRevisionId"):
            raise FirebaseCatalogError(
                "the map changed during review and cannot be restored automatically"
            )
        if current.get("status") != "inactive":
            raise FirebaseCatalogError("the reviewed map is not inactive")

        now = _timestamp()
        server_actor = f"server:{self.server_id}"
        updated_review = {
            key: value for key, value in review.items() if not key.startswith("_")
        }
        updated_review.update(
            {
                "status": "cancelled",
                "reviewedAt": now,
                "reviewedBy": server_actor,
                "reviewReason": reason,
                "updatedAt": now,
            }
        )
        updated_map = {
            key: value for key, value in current.items() if not key.startswith("_")
        }
        updated_map.update(
            {
                "status": "active",
                "statusReason": reason,
                "statusUpdatedBy": server_actor,
                "statusUpdatedAt": now,
                "reviewSubmissionId": "",
                "updatedAt": now,
            }
        )
        writes = [
            self._update_write(
                _document(self.project, "mapSubmissions", review_id, updated_review),
                update_time=review["_update_time"],
            ),
            self._update_write(
                _document(self.project, "maps", map_id, updated_map),
                update_time=current["_update_time"],
            ),
        ]
        draft_id = str(review.get("reviewRevisionId", ""))
        if draft_id:
            draft = self.get_document("mapSubmissions", draft_id)
            if draft.get("status") == "review-draft":
                updated_draft = {
                    key: value for key, value in draft.items() if not key.startswith("_")
                }
                updated_draft.update({"status": "cancelled", "updatedAt": now})
                writes.append(self._update_write(
                    _document(
                        self.project, "mapSubmissions", draft_id, updated_draft
                    ),
                    update_time=draft["_update_time"],
                ))
        audit_id = f"server_{uuid.uuid4().hex}"
        writes.append(self._update_write(_document(
            self.project,
            "auditEvents",
            audit_id,
            {
                "actorUid": server_actor,
                "actorName": self.server_id,
                "action": "map.review.cancel",
                "targetType": "mapSubmission",
                "targetId": review_id,
                "mapId": map_id,
                "reason": reason,
                "before": {"reviewStatus": review.get("status"), "mapStatus": "inactive"},
                "after": {"reviewStatus": "cancelled", "mapStatus": "active"},
                "createdAt": now,
            },
        )))
        self._commit(writes)
        return {**updated_review, "_id": review_id}


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and validate a Firebase map-catalog snapshot"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--catalog-dir", type=Path)
    parser.add_argument(
        "--allow-unready",
        action="store_true",
        help="validate a staged catalog before catalogSettings/current.ready is true",
    )
    return parser.parse_args()


def _cli_main() -> int:
    args = _parse_cli_args()
    try:
        config = json.loads(args.config.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FirebaseCatalogError(f"unable to read config {args.config}: {exc}") from exc
    root = args.catalog_dir or Path(
        config.get("firebase_catalog_dir", "/var/lib/tronner-racing/firebase-catalog")
    )
    client = FirebaseCatalogClient(config)
    manifest = client.sync_snapshot(root, require_ready=not args.allow_unready)
    maps = manifest.get("maps", [])
    active = sum(item.get("status") == "active" for item in maps)
    print(json.dumps({
        "generation": manifest.get("generation"),
        "ready": manifest.get("ready") is True,
        "maps": len(maps),
        "active": active,
        "inactive": len(maps) - active,
        "catalogDir": str(root.resolve()),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
