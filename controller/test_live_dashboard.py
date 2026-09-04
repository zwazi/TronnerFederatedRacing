import gzip
import json
import unittest

from live_dashboard import (
    FirebaseLiveDashboardPublisher,
    history_document_id,
    leaderboard_document_id,
    map_leaderboard,
    map_rating_entries,
    map_rating_fields,
    overall_leaderboard,
)


class FakeStore:
    def get_json(self, _key, default):
        return default


class FakeFirebase:
    pass


class LeaderboardFirebase:
    def __init__(self):
        self.documents = []

    def list_documents(self, _collection):
        return []

    def set_document(self, collection, document_id, payload):
        self.documents.append((collection, document_id, payload))


class ReplayStore(FakeStore):
    def __init__(self):
        self.saved = {}

    def get_json(self, key, default):
        return self.saved.get(key, default)

    def set_json(self, key, value):
        self.saved[key] = value

    def dashboard_finished_replays_after(self, cursor, limit):
        if cursor:
            return []
        return [{
            "runId": 7, "identityKey": "auth:racer", "playerId": "player-id",
            "username": "Racer", "authenticated": True, "mapKey": "author/map-1.xml",
            "mapId": "map", "revisionId": "rev"
        }]

    def dashboard_replay_payload(self, run_id):
        return {
            "schemaVersion": 1, "runId": run_id, "playerId": "player-id",
            "name": "Racer", "mapKey": "author/map-1.xml", "settingsFingerprint": "abc",
            "settingsTransitions": [], "start": {"x": 1, "y": 2},
            "seconds": 8.25, "events": [[1000, 1]]
        }

    def dashboard_replay_settings_by_fingerprint(self, fingerprint):
        return {"fingerprint": fingerprint, "settings": [["CYCLE_SPEED", "30"]]}

    def dashboard_player_map_history(self, identity_key, map_key, limit):
        return [{
            "runId": 7, "recordedAt": 1000, "endedAt": 9000,
            "seconds": 8.25, "turns": 3, "personalBest": True,
            "eventCount": 1, "settingsRef": 1, "mapId": "map", "revisionId": "rev"
        }]

    def dashboard_replay_history_groups_after(self, map_key, identity_key, limit):
        if map_key or identity_key:
            return []
        return [{
            "identityKey": "auth:racer", "playerId": "player-id",
            "username": "Racer", "authenticated": True,
            "mapKey": "author/map-1.xml", "mapId": "map", "revisionId": "rev",
        }]


class ReplayFirebase:
    def __init__(self):
        self.objects = []
        self.documents = []

    def upload_immutable_object(self, path, data, metadata, **options):
        self.objects.append((path, data, metadata, options))
        return True

    def set_document(self, collection, document_id, payload):
        self.documents.append((collection, document_id, payload))

    def get_document(self, collection, document_id):
        if collection != "mapSubmissions" or document_id != "rev":
            raise AssertionError("unexpected replay map lookup")
        return {
            "storagePath": "_revisions/builder/rev/map.xml",
            "resourcePath": "builder/map/rev/map.aamap.xml",
        }


class LiveDashboardTest(unittest.TestCase):
    def rows(self):
        return [
            {"mapKey": "map-a", "identityKey": "one", "username": "One", "authenticated": True, "bestSeconds": 8.0, "bestTurns": 3, "achievedAt": 2},
            {"mapKey": "map-a", "identityKey": "two", "username": "Two", "authenticated": True, "bestSeconds": 7.0, "bestTurns": 4, "achievedAt": 1, "hasReplay": True},
            {"mapKey": "map-b", "identityKey": "one", "username": "One", "authenticated": True, "bestSeconds": 4.0, "bestTurns": None, "achievedAt": 3},
            {"mapKey": "map-b", "identityKey": "guest", "username": "Guest", "authenticated": False, "bestSeconds": 3.0, "bestTurns": None, "achievedAt": 4},
        ]

    def test_map_ranks_are_precomputed_and_bounded(self):
        entries = map_leaderboard("map-a", self.rows())
        self.assertEqual([entry["name"] for entry in entries], ["Two", "One"])
        self.assertEqual([entry["rank"] for entry in entries], [1, 2])
        self.assertEqual([entry["hasReplay"] for entry in entries], [True, False])
        self.assertEqual([entry["achievedAt"] for entry in entries], [1000, 2000])
        self.assertNotIn("identityKey", entries[0])

    def test_overall_ignores_guests_and_uses_map_rank_points(self):
        entries = overall_leaderboard(self.rows())
        self.assertEqual(entries[0]["name"], "One")
        self.assertEqual(entries[0]["points"], 199)
        self.assertEqual(entries[0]["maps"], 2)
        self.assertEqual(entries[1]["name"], "Two")

    def test_document_ids_are_stable_and_hide_long_map_keys(self):
        first = leaderboard_document_id("author/maps/name-version.aamap.xml")
        self.assertEqual(first, leaderboard_document_id("author/maps/name-version.aamap.xml"))
        self.assertRegex(first, r"^map_[0-9a-f]{64}$")

    def test_rating_fields_reject_invalid_or_incomplete_averages(self):
        self.assertEqual(
            map_rating_fields({"rating": 4.333333, "ratingCount": 3}),
            {"rating": 4.3333, "ratingCount": 3},
        )

    def test_individual_rating_entries_are_sanitized_and_bounded(self):
        self.assertEqual(map_rating_entries({"ratings": [{
            "playerId": "a" * 24,
            "name": "Racer",
            "authenticated": True,
            "racingProfile": True,
            "rating": 4,
            "ratedAt": 123000,
        }, {"playerId": "unsafe", "rating": 8, "ratedAt": 0}]}), [{
            "playerId": "a" * 24,
            "name": "Racer",
            "authenticated": True,
            "racingProfile": True,
            "rating": 4,
            "ratedAt": 123000,
        }])
        self.assertEqual(
            map_rating_fields({"rating": None, "ratingCount": 0}),
            {"rating": None, "ratingCount": 0},
        )
        self.assertEqual(
            map_rating_fields({"rating": 6, "ratingCount": 1}),
            {"rating": None, "ratingCount": 0},
        )

    def test_catalog_carries_each_current_versions_record_count(self):
        firebase = LeaderboardFirebase()
        publisher = FirebaseLiveDashboardPublisher(
            firebase, "https://example.firebaseio.com", FakeStore()
        )
        publisher.publish_leaderboards(self.rows(), {
            "map-a": {"name": "Map A", "author": "Builder", "version": "1"},
            "map-b": {"name": "Map B", "author": "Builder", "version": "2"},
        })

        catalog = next(
            payload for collection, document_id, payload in firebase.documents
            if collection == "racingCatalog" and document_id == "current"
        )
        counts = {entry["mapKey"]: entry["entryCount"] for entry in catalog["maps"]}
        self.assertEqual(counts, {"map-a": 2, "map-b": 2})

    def test_catalog_includes_rated_map_without_a_finish(self):
        firebase = LeaderboardFirebase()
        publisher = FirebaseLiveDashboardPublisher(
            firebase, "https://example.firebaseio.com", FakeStore()
        )
        publisher.publish_leaderboards([], {
            "map-unfinished": {
                "mapId": "map-id",
                "name": "Unfinished",
                "author": "Builder",
                "version": "1",
                "ratingKey": "builder/maps/unfinished",
                "rating": 4.5,
                "ratingCount": 2,
            }
        })

        catalog = next(
            payload for collection, document_id, payload in firebase.documents
            if collection == "racingCatalog" and document_id == "current"
        )
        self.assertEqual(catalog["maps"], [{
            "mapKey": "map-unfinished",
            "mapId": "map-id",
            "name": "Unfinished",
            "author": "Builder",
            "version": "1",
            "storagePath": "",
            "ratingKey": "builder/maps/unfinished",
            "rating": 4.5,
            "ratingCount": 2,
            "leaderboardId": leaderboard_document_id("map-unfinished"),
            "entryCount": 0,
            "record": None,
        }])
        leaderboard = next(
            payload for collection, document_id, payload in firebase.documents
            if collection == "racingLeaderboards"
            and document_id == leaderboard_document_id("map-unfinished")
        )
        self.assertEqual(leaderboard["entries"], [])
        self.assertEqual(leaderboard["rating"], 4.5)

    def test_rating_command_poll_and_updates_are_bounded(self):
        publisher = FirebaseLiveDashboardPublisher(
            FakeFirebase(), "https://example.firebaseio.com", FakeStore()
        )
        calls = []

        def request(path, method, value=None, *, query=None):
            calls.append((path, method, value, query))
            return {
                "later": {"state": "queued", "requestedAt": 20},
                "ignored": {"state": "running", "requestedAt": 1},
                "first": {"state": "queued", "requestedAt": 10},
            } if method == "GET" else None

        publisher._rtdb = request
        commands = publisher.queued_rating_commands("nyc1", 500)
        self.assertEqual([command_id for command_id, _ in commands], ["first", "later"])
        self.assertEqual(calls[0], (
            "racing/ratingCommands/nyc1",
            "GET",
            None,
            {
                "orderBy": json.dumps("state"),
                "equalTo": json.dumps("queued"),
                "limitToFirst": "20",
            },
        ))
        publisher.update_rating_command(
            "nyc1", "first", "succeeded", result="Rated map 4/5."
        )
        self.assertEqual(calls[1][0:2], (
            "racing/ratingCommands/nyc1/first", "PATCH"
        ))
        self.assertEqual(calls[1][2]["state"], "succeeded")

    def test_chat_retention_uses_shallow_global_keys(self):
        publisher = FirebaseLiveDashboardPublisher(
            FakeFirebase(), "https://example.firebaseio.com", FakeStore()
        )
        calls = []

        def request(path, method, value=None, *, query=None):
            calls.append((path, method, value, query))
            if method == "GET":
                return {f"{index:013d}-key": True for index in range(252)}
            return None

        publisher._rtdb = request
        key = publisher.publish_chat({"message": "Hello"})

        self.assertRegex(key, r"^[0-9]{13}-[0-9a-f]{12}$")
        self.assertEqual(calls[1], ("racing/chat", "GET", None, {"shallow": "true"}))
        self.assertEqual(
            [(path, method) for path, method, _value, _query in calls[2:]],
            [
                ("racing/chat/0000000000000-key", "DELETE"),
                ("racing/chat/0000000000001-key", "DELETE"),
            ],
        )

    def test_finish_activity_is_publication_bounded_and_timestamped(self):
        publisher = FirebaseLiveDashboardPublisher(
            FakeFirebase(), "https://example.firebaseio.com", FakeStore()
        )
        calls = []

        def request(path, method, value=None, *, query=None):
            calls.append((path, method, value, query))
            return {} if method == "GET" else None

        publisher._rtdb = request
        key = publisher.publish_activity({"name": "Racer", "seconds": 8.25})

        self.assertEqual(calls[0][0:2], (f"racing/activity/{key}", "PUT"))
        self.assertIn("finishedAt", calls[0][2])
        self.assertEqual(
            calls[1],
            ("racing/activity", "GET", None, {"shallow": "true"}),
        )

        publisher.publish_activity({"name": "Racer", "seconds": 8.0})
        self.assertEqual(
            [method for _path, method, _value, _query in calls].count("GET"),
            1,
        )

    def test_admin_command_poll_is_indexed_and_bounded(self):
        publisher = FirebaseLiveDashboardPublisher(
            FakeFirebase(), "https://example.firebaseio.com", FakeStore()
        )
        calls = []

        def request(path, method, value=None, *, query=None):
            calls.append((path, method, value, query))
            return {
                "later": {"state": "queued", "requestedAt": 20},
                "ignored": {"state": "running", "requestedAt": 1},
                "first": {"state": "queued", "requestedAt": 10},
            }

        publisher._rtdb = request
        commands = publisher.queued_admin_commands("region-a", 500)

        self.assertEqual([command_id for command_id, _ in commands], ["first", "later"])
        self.assertEqual(calls, [(
            "racing/admin/commands/region-a",
            "GET",
            None,
            {
                "orderBy": json.dumps("state"),
                "equalTo": json.dumps("queued"),
                "limitToFirst": "10",
            },
        )])

    def test_admin_status_and_audit_updates_stay_in_private_paths(self):
        publisher = FirebaseLiveDashboardPublisher(
            FakeFirebase(), "https://example.firebaseio.com", FakeStore()
        )
        calls = []
        publisher._rtdb = lambda path, method, value=None, *, query=None: calls.append(
            (path, method, value, query)
        )

        publisher.publish_admin_status("region-a", {"online": True, "players": []})
        publisher.update_admin_command(
            "region-a", "command-1", "succeeded",
            result="Announcement delivered.", details={"scope": "local"}
        )

        self.assertEqual(calls[0][0:2], ("racing/admin/status/region-a", "PUT"))
        self.assertTrue(calls[0][2]["online"])
        self.assertEqual(calls[0][2]["serverId"], "region-a")
        self.assertEqual(calls[1][0:2], (
            "racing/admin/commands/region-a/command-1", "PATCH"
        ))
        self.assertEqual(calls[1][2]["state"], "succeeded")
        self.assertEqual(calls[1][2]["details"], {"scope": "local"})

    def test_admin_console_batches_are_private_sanitized_and_bounded(self):
        publisher = FirebaseLiveDashboardPublisher(
            FakeFirebase(), "https://example.firebaseio.com", FakeStore()
        )
        calls = []
        publisher._rtdb = lambda path, method, value=None, *, query=None: calls.append(
            (path, method, value, query)
        ) or ({} if method == "GET" else None)

        key = publisher.publish_admin_console("region-a", [
            {"sequence": 7, "at": 1234, "message": "[0] map loaded"},
        ])

        self.assertTrue(key)
        self.assertTrue(calls[0][0].startswith("racing/admin/console/region-a/"))
        self.assertEqual(calls[0][1], "PUT")
        self.assertEqual(calls[0][2]["serverId"], "region-a")
        self.assertEqual(calls[0][2]["entries"][0]["sequence"], 7)
        self.assertEqual(
            calls[1],
            ("racing/admin/console/region-a", "GET", None, {"shallow": "true"}),
        )

    def test_admin_console_caps_each_upload_to_twenty_five_lines(self):
        publisher = FirebaseLiveDashboardPublisher(
            FakeFirebase(), "https://example.firebaseio.com", FakeStore()
        )
        calls = []
        publisher._rtdb = lambda path, method, value=None, *, query=None: calls.append(
            (path, method, value, query)
        ) or ({} if method == "GET" else None)

        publisher.publish_admin_console("region-b", [
            {"sequence": index, "at": index, "message": f"line {index}"}
            for index in range(30)
        ])

        self.assertEqual(len(calls[0][2]["entries"]), 25)

    def test_private_user_audit_is_allowlisted_timestamped_and_bounded(self):
        publisher = FirebaseLiveDashboardPublisher(
            FakeFirebase(), "https://example.firebaseio.com", FakeStore()
        )
        calls = []
        publisher._rtdb = lambda path, method, value=None, *, query=None: calls.append(
            (path, method, value, query)
        ) or ({} if method == "GET" else None)

        key = publisher.publish_admin_audit("nyc1", {
            "action": "chat",
            "displayName": "Racer",
            "authName": "racer@tronner",
            "ipAddress": "203.0.113.9",
            "message": "hello",
            "secret": "must not cross the boundary",
        })

        self.assertTrue(calls[0][0].endswith(key))
        self.assertEqual(calls[0][1], "PUT")
        self.assertEqual(calls[0][2]["source"], "server")
        self.assertEqual(calls[0][2]["serverId"], "nyc1")
        self.assertEqual(calls[0][2]["ipAddress"], "203.0.113.9")
        self.assertNotIn("secret", calls[0][2])
        self.assertIn("occurredAt", calls[0][2])
        self.assertEqual(
            calls[1],
            ("racing/admin/audit/events", "GET", None, {"shallow": "true"}),
        )

    def test_admin_audit_pruning_uses_shallow_keys_and_caps_retention(self):
        publisher = FirebaseLiveDashboardPublisher(
            FakeFirebase(), "https://example.firebaseio.com", FakeStore()
        )
        calls = []

        def request(path, method, value=None, *, query=None):
            calls.append((path, method, value, query))
            if method == "GET":
                return {f"key-{index:03d}": True for index in range(205)}
            return None

        publisher._rtdb = request
        self.assertEqual(publisher.prune_admin_commands("region-a"), 5)
        self.assertEqual(calls[0], (
            "racing/admin/commands/region-a", "GET", None, {"shallow": "true"}
        ))
        self.assertEqual(
            [(path, method) for path, method, _value, _query in calls[1:]],
            [(f"racing/admin/commands/region-a/key-{index:03d}", "DELETE") for index in range(5)],
        )

    def test_finished_replays_publish_once_with_exact_read_history(self):
        store = ReplayStore()
        firebase = ReplayFirebase()
        publisher = FirebaseLiveDashboardPublisher(
            firebase, "https://example.firebaseio.com", store
        )

        self.assertEqual(publisher.publish_replay_batch("region-a"), 1)
        self.assertEqual(
            [item[0] for item in firebase.objects],
            ["_racing/settings/region-a/abc.json.gz", "_racing/replays/region-a/7.json.gz"],
        )
        replay = json.loads(gzip.decompress(firebase.objects[1][1]))
        self.assertEqual(replay["settingsPaths"]["abc"], "_racing/settings/region-a/abc.json.gz")
        self.assertEqual(firebase.documents[0][0], "racingRunHistories")
        self.assertEqual(
            firebase.documents[0][1],
            history_document_id("author/map-1.xml", "player-id", "region-a"),
        )
        history = firebase.documents[0][2]
        self.assertNotIn("identityKey", history)
        self.assertEqual(history["entries"][0]["replayPath"], "_racing/replays/region-a/7.json.gz")
        self.assertEqual(
            history["entries"][0]["mapStoragePath"],
            "_revisions/builder/rev/map.xml",
        )
        self.assertEqual(
            history["entries"][0]["mapResourcePath"],
            "builder/map/rev/map.aamap.xml",
        )
        self.assertNotIn("settingsRef", history["entries"][0])
        self.assertEqual(store.saved["live_dashboard_replay_cursor_region-a"], 7)

        self.assertEqual(publisher.publish_replay_batch("region-a"), 0)
        self.assertEqual(len(firebase.objects), 2)

    def test_replay_history_backfill_does_not_rewrite_replay_objects(self):
        store = ReplayStore()
        firebase = ReplayFirebase()
        publisher = FirebaseLiveDashboardPublisher(
            firebase, "https://example.firebaseio.com", store
        )

        self.assertEqual(
            publisher.publish_replay_history_backfill_batch("region-a"), 1
        )
        self.assertEqual(firebase.objects, [])
        self.assertEqual(firebase.documents[0][0], "racingRunHistories")
        self.assertEqual(
            firebase.documents[0][2]["entries"][0]["mapStoragePath"],
            "_revisions/builder/rev/map.xml",
        )
        self.assertEqual(
            publisher.publish_replay_history_backfill_batch("region-a"), 0
        )
        self.assertTrue(
            store.saved["live_dashboard_replay_history_backfill_v2_region-a"]["complete"]
        )


if __name__ == "__main__":
    unittest.main()
