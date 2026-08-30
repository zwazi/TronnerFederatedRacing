import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from firebase_catalog import FirebaseCatalogClient, _decode_document


def map_bytes():
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Resource type="aamap" author="Tester" category="maps" '
        'name="Race" version="v1"><Map><World><Field>'
        '<Spawn x="0" y="0" xdir="1" ydir="0"/>'
        '</Field></World></Map></Resource>'
    ).encode()


def map_document(data, *, status="active"):
    return {
        "_id": "map-id",
        "mapId": "map-id",
        "status": status,
        "authorId": "author-id",
        "authorName": "Tester",
        "category": "maps",
        "mapName": "Race",
        "mapVersion": "v1",
        "activeRevisionId": "revision-id",
        "storagePath": "_revisions/test/revision-id",
        "resourcePath": "Tester/maps/Race-v1.aamap.xml",
        "recordKey": "Tester/maps/Race-v1.aamap.xml",
        "ratingKey": "tester/maps/race",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


class FakeCatalog(FirebaseCatalogClient):
    def __init__(self, maps, objects, state):
        self.project = "project"
        self.bucket = "bucket"
        self.server_id = "test-server"
        self.maps = maps
        self.objects = objects
        self.state = state
        self.queries = []
        self.commits = []

    def get_document(self, collection, document_id):
        if collection == "catalogSettings":
            return {"ready": True}
        if collection == "catalogState":
            return dict(self.state)
        raise AssertionError((collection, document_id))

    def list_documents(self, collection):
        raise AssertionError(f"unexpected collection scan: {collection}")

    def query_documents(self, collection, field, value):
        self.queries.append((collection, field, value))
        source = self.maps if collection == "maps" else []
        return [dict(item) for item in source if item.get(field) == value]

    def download_object(self, storage_path, *, accept_gzip=False):
        return self.objects[storage_path]

    def _commit(self, writes):
        self.commits.append(writes)


class FirebaseCatalogCostTests(unittest.TestCase):
    def test_versioned_manifest_avoids_map_collection_scan(self):
        data = map_bytes()
        document = map_document(data)
        manifest = {
            "schemaVersion": 2,
            "generation": "generation-1",
            "maps": [{key: value for key, value in document.items() if key != "_id"}],
        }
        packed = gzip.compress(json.dumps(manifest).encode(), mtime=0)
        state = {
            "catalogVersion": 1,
            "generation": "generation-1",
            "serverManifestPath": "_catalog/server/generation-1.json.gz",
            "serverManifestSha256": hashlib.sha256(packed).hexdigest(),
        }
        client = FakeCatalog(
            [document],
            {state["serverManifestPath"]: packed, document["storagePath"]: data},
            state,
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = client.sync_snapshot(Path(temporary) / "catalog")
        self.assertEqual(result["generation"], "generation-1")
        self.assertEqual(result["catalogVersion"], 1)
        self.assertEqual(result["maps"][0]["mapId"], "map-id")

    def test_server_review_lookup_uses_targeted_queries(self):
        data = map_bytes()
        inactive = map_document(data, status="inactive")
        inactive["reviewSubmissionId"] = "review-id"
        state = {}
        client = FakeCatalog([inactive], {}, state)
        review = {
            "_id": "review-id",
            "submissionId": "review-id",
            "mapId": "map-id",
            "operation": "server-review",
            "status": "pending",
            "mapName": "Race",
            "authorName": "Tester",
            "mapVersion": "v1",
        }

        def query(collection, field, value):
            client.queries.append((collection, field, value))
            if collection == "mapSubmissions":
                return [review]
            return [inactive]

        client.query_documents = query
        self.assertEqual(client.list_map_reviews(), [review])
        self.assertEqual(client.queries, [
            ("mapSubmissions", "operation", "server-review"),
            ("maps", "status", "inactive"),
        ])

    def test_acknowledgement_is_one_small_document_write(self):
        client = FakeCatalog([], {}, {})
        client.publish_server_catalog_state(
            catalog_state={"catalogVersion": 9},
            generation="generation-9",
            map_count=446,
        )
        self.assertEqual(len(client.commits), 1)
        self.assertEqual(len(client.commits[0]), 1)
        written = _decode_document(client.commits[0][0]["update"])
        self.assertEqual(written["appliedCatalogVersion"], 9)
        self.assertEqual(written["appliedGeneration"], "generation-9")
        self.assertEqual(written["mapCount"], 446)


if __name__ == "__main__":
    unittest.main()
