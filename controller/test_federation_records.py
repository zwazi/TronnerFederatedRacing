import asyncio
import concurrent.futures
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from TronnerRacing import Player, StateStore, TronnerRacing as Controller


def federation_event(server_id: str, payload: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "server_id": server_id,
            "boot_id": "test-boot",
            "sequence": 1,
            "sent_ns": time.time_ns(),
            "kind": "records_delta",
            "payload": payload,
        }
    ).encode("utf-8")


def record_controller(
    store: StateStore,
    *,
    local_server_id: str,
    remote_server_id: str,
) -> Controller:
    controller = Controller.__new__(Controller)
    controller.store = store
    controller.config = {"maximum_record_seconds": 7200}
    controller.federation_role = "leader"
    controller.federation_local_server_id = local_server_id
    controller.federation_remote_server_id = remote_server_id
    controller.federation_remote_regions = {remote_server_id: "REMOTE"}
    controller.federation_leader_server_id = local_server_id
    controller.federation_last_sent_ns = 0
    controller.federation_last_state_sent_ns = {}
    controller.federation_last_boot_id = ""
    controller.federation_peer_last_received_monotonic = {}
    controller._publish_federation_control = AsyncMock(return_value=True)
    return controller


class FederationRecordStoreTests(unittest.TestCase):
    def test_peer_snapshot_is_authenticated_stable_and_paginated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "records.sqlite3")
            first = Player("first", "First", auth_name="First@forums")
            second = Player("second", "Second", auth_name="Second@forums")
            guest = Player("guest", "Guest")
            store.add_finish("a-map", first, 10.0, 12)
            store.add_finish("b-map", second, 11.0, 14)
            store.add_finish("c-map", guest, 9.0, 10)

            first_page = store.federation_record_snapshot("region-a", 1, 0)
            second_page = store.federation_record_snapshot("region-a", 1, 1)
            repeated = store.federation_record_snapshot("region-a", 1, 0)

            self.assertEqual(len(first_page), 1)
            self.assertEqual(len(second_page), 1)
            self.assertEqual(first_page, repeated)
            self.assertNotEqual(
                first_page[0]["identity_key"], second_page[0]["identity_key"]
            )
            self.assertTrue(str(first_page[0]["identity_key"]).startswith("auth:"))
            self.assertTrue(str(second_page[0]["identity_key"]).startswith("auth:"))
            store.close()

    def test_dashboard_metadata_uses_a_worker_local_sqlite_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "records.sqlite3")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as worker:
                worker.submit(store.set_json, "dashboard-cursor", 42).result()
                self.assertEqual(
                    worker.submit(store.get_json, "dashboard-cursor", 0).result(),
                    42,
                )
                self.assertEqual(worker.submit(store.dashboard_record_rows).result(), [])
            self.assertEqual(store.get_json("dashboard-cursor", 0), 42)
            store.close()

    def test_snapshot_queues_only_authenticated_records_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.sqlite3"
            store = StateStore(path)
            store.add_finish(
                "author/maps/race.xml",
                Player("account", "Account", auth_name="Account@forums"),
                10.0,
                5,
            )
            store.add_finish(
                "author/maps/race.xml",
                Player("guest", "Guest"),
                9.0,
                4,
            )

            self.assertEqual(store.seed_federation_record_outbox("region-a"), 1)
            queued = store.pending_federation_records(20)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0]["identity_key"], "auth:account@forums")
            store.close()

            reopened = StateStore(path)
            self.assertEqual(reopened.pending_federation_records(20), queued)
            reopened.close()

    def test_merge_keeps_best_time_then_fewest_turns_without_finish_duplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "records.sqlite3")
            player = Player("racer", "Racer", auth_name="Racer@forums")
            store.add_finish("map", player, 14.4, 50)

            self.assertFalse(
                store.apply_federated_record(
                    map_key="map",
                    identity_key=player.identity_key,
                    username=player.record_name,
                    best_seconds=14.479,
                    best_turns=40,
                    achieved_at=time.time(),
                )
            )
            self.assertTrue(
                store.apply_federated_record(
                    map_key="map",
                    identity_key=player.identity_key,
                    username=player.record_name,
                    best_seconds=14.4,
                    best_turns=45,
                    achieved_at=time.time(),
                )
            )
            self.assertTrue(
                store.apply_federated_record(
                    map_key="map",
                    identity_key=player.identity_key,
                    username=player.record_name,
                    best_seconds=14.1,
                    best_turns=60,
                    achieved_at=time.time(),
                )
            )
            record = store.records("map")[0]
            self.assertEqual((record.best_seconds, record.best_turns), (14.1, 60))
            self.assertEqual(
                store.connection.execute("SELECT COUNT(*) FROM finishes").fetchone()[0],
                1,
            )
            store.close()

    def test_imported_personal_best_is_not_fabricated_as_finish_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.sqlite3"
            store = StateStore(path)
            store.apply_federated_record(
                map_key="map",
                identity_key="auth:racer@forums",
                username="Racer@forums",
                best_seconds=12.5,
                best_turns=30,
                achieved_at=time.time(),
            )
            store.close()

            reopened = StateStore(path)
            self.assertEqual(reopened.records("map")[0].best_seconds, 12.5)
            self.assertEqual(
                reopened.connection.execute(
                    "SELECT COUNT(*) FROM finishes"
                ).fetchone()[0],
                0,
            )
            reopened.close()

    def test_old_ack_cannot_delete_a_newer_pending_personal_best(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "records.sqlite3")
            player = Player("racer", "Racer", auth_name="Racer@forums")
            store.add_finish("map", player, 20.0, 10)
            self.assertTrue(
                store.queue_federation_record("region-b", "map", player.identity_key)
            )
            old_event_id = str(
                store.pending_federation_records(20)[0]["event_id"]
            )

            store.add_finish("map", player, 19.0, 12)
            self.assertTrue(
                store.queue_federation_record("region-b", "map", player.identity_key)
            )
            new_event_id = str(
                store.pending_federation_records(20)[0]["event_id"]
            )
            self.assertNotEqual(old_event_id, new_event_id)
            self.assertEqual(store.acknowledge_federation_records([old_event_id]), 0)
            self.assertEqual(
                store.pending_federation_records(20)[0]["event_id"],
                new_event_id,
            )
            self.assertEqual(store.acknowledge_federation_records([new_event_id]), 1)
            self.assertEqual(store.pending_federation_records(20), [])
            store.close()

    def test_replay_availability_converges_with_the_bounded_pb_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = StateStore(Path(tmp) / "source.sqlite3")
            peer = StateStore(Path(tmp) / "peer.sqlite3")
            player = Player("racer", "Racer", auth_name="Racer@forums")
            source.add_finish("map", player, 12.5, 30)
            self.assertTrue(source.queue_federation_record(
                "region-b", "map", player.identity_key
            ))
            without_replay = source.pending_federation_records(1)[0]
            self.assertFalse(without_replay["has_replay"])

            self.assertTrue(source.mark_replay_available("map", player.identity_key))
            self.assertTrue(source.queue_federation_record(
                "region-b", "map", player.identity_key
            ))
            with_replay = source.pending_federation_records(1)[0]
            self.assertTrue(with_replay["has_replay"])
            self.assertNotEqual(without_replay["event_id"], with_replay["event_id"])

            peer.apply_federated_record(
                map_key="map",
                identity_key=player.identity_key,
                username=player.record_name,
                best_seconds=12.5,
                best_turns=30,
                achieved_at=time.time(),
                has_replay=True,
            )
            self.assertTrue(peer.dashboard_record_rows()[0]["hasReplay"])
            source.close()
            peer.close()


class FederationRecordControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_leader_serves_a_snapshot_only_to_requesting_peer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "records.sqlite3")
            player = Player("racer", "Racer", auth_name="Racer@forums")
            store.add_finish("map", player, 10.0, 12)
            controller = record_controller(
                store,
                local_server_id="region-a",
                remote_server_id="region-b",
            )

            await controller._handle_federation_records_delta(
                "region-c",
                {"operation": "snapshot_request"},
            )

            controller._publish_federation_control.assert_awaited_once()
            kind, payload = controller._publish_federation_control.await_args.args
            self.assertEqual(kind, "records_delta")
            self.assertEqual(payload["operation"], "upsert")
            self.assertEqual(payload["target_server_id"], "region-c")
            self.assertEqual(payload["snapshot_offset"], 0)
            self.assertEqual(payload["snapshot_next_offset"], 1)
            self.assertTrue(payload["snapshot_complete"])
            self.assertEqual(len(payload["records"]), 1)
            store.close()

    async def test_non_target_follower_ignores_targeted_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "records.sqlite3")
            controller = record_controller(
                store,
                local_server_id="region-b",
                remote_server_id="region-a",
            )
            controller.federation_role = "follower"
            record = {
                "event_id": "a" * 64,
                "map_key": "map",
                "identity_key": "auth:racer@forums",
                "username": "Racer@forums",
                "best_seconds": 10.0,
                "best_turns": 12,
                "achieved_at": time.time(),
                "has_replay": False,
            }

            await controller._handle_federation_records_delta(
                "region-a",
                {
                    "operation": "upsert",
                    "target_server_id": "region-c",
                    "records": [record],
                },
            )

            self.assertEqual(store.records("map"), [])
            controller._publish_federation_control.assert_not_awaited()
            store.close()

    async def test_follower_ignores_another_followers_snapshot_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "records.sqlite3")
            controller = record_controller(
                store,
                local_server_id="region-b",
                remote_server_id="region-a",
            )
            controller.federation_role = "follower"

            await controller._handle_federation_records_delta(
                "region-c",
                {"operation": "snapshot_request", "offset": 20},
            )

            controller._publish_federation_control.assert_not_awaited()
            store.close()

    async def test_follower_requests_authority_snapshot_on_sync_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "records.sqlite3")
            controller = record_controller(
                store,
                local_server_id="region-b",
                remote_server_id="region-a",
            )
            controller.federation_role = "follower"
            controller.federation_leader_server_id = "region-a"
            controller._federation_record_wakeup = asyncio.Event()

            task = asyncio.create_task(controller._federation_record_snapshot_sync())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            controller._publish_federation_control.assert_awaited_once_with(
                "records_delta",
                {"operation": "snapshot_request", "offset": 0},
            )
            store.close()

    async def test_acknowledgments_advance_snapshot_without_retry_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "records.sqlite3")
            for number in range(21):
                player = Player(
                    f"racer{number}",
                    f"Racer {number}",
                    auth_name=f"racer{number}@forums",
                )
                store.add_finish("map", player, 10.0 + number, number)
            controller = record_controller(
                store,
                local_server_id="region-a",
                remote_server_id="region-b",
            )
            batches: list[list[dict[str, object]]] = []
            complete = asyncio.Event()

            async def publish(_kind, payload):
                records = payload["records"]
                batches.append(records)
                store.acknowledge_federation_records(
                    [str(record["event_id"]) for record in records]
                )
                controller._federation_record_wakeup.set()
                if not store.pending_federation_records(20):
                    complete.set()
                return True

            controller._publish_federation_control = publish
            task = asyncio.create_task(controller.federation_record_sync())
            await asyncio.wait_for(complete.wait(), timeout=1)
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual([len(batch) for batch in batches], [20, 1])
            store.close()

    async def test_leader_personal_best_prevents_false_follower_personal_best(self):
        with tempfile.TemporaryDirectory() as tmp:
            leader_store = StateStore(Path(tmp) / "leader.sqlite3")
            follower_store = StateStore(Path(tmp) / "follower.sqlite3")
            player = Player("player", "Player", auth_name="Player@forums")
            map_key = "Gunner/maps/PewPew-v2.aamap.xml"
            leader_store.add_finish(map_key, player, 14.4008141, 50)
            leader_store.seed_federation_record_outbox("region-a")
            leader_batch = leader_store.pending_federation_records(20)

            follower = record_controller(
                follower_store,
                local_server_id="region-b",
                remote_server_id="region-a",
            )
            await follower.handle_federation_datagram(
                federation_event(
                    "region-a",
                    {"operation": "upsert", "records": leader_batch},
                )
            )
            _, improved, previous, _ = follower_store.add_finish(
                map_key, player, 14.4790992, 45
            )
            self.assertFalse(improved)
            self.assertEqual(previous, 14.4008141)
            follower._publish_federation_control.assert_awaited_once_with(
                "records_delta",
                {
                    "operation": "ack",
                    "target_server_id": "region-a",
                    "event_ids": [leader_batch[0]["event_id"]],
                },
            )
            leader_store.close()
            follower_store.close()

    async def test_better_follower_personal_best_reaches_leader_leaderboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            leader_store = StateStore(Path(tmp) / "leader.sqlite3")
            follower_store = StateStore(Path(tmp) / "follower.sqlite3")
            player = Player("player", "Player", auth_name="Player@forums")
            map_key = "Eristan/maps/Nopo-v1.aamap.xml"
            leader_store.add_finish(map_key, player, 24.9528961, 40)
            follower_store.add_finish(map_key, player, 24.1144104, 42)
            follower_store.seed_federation_record_outbox("region-b")
            follower_batch = follower_store.pending_federation_records(20)

            leader = record_controller(
                leader_store,
                local_server_id="region-a",
                remote_server_id="region-b",
            )
            await leader.handle_federation_datagram(
                federation_event(
                    "region-b",
                    {"operation": "upsert", "records": follower_batch},
                )
            )
            record = leader_store.records(map_key)[0]
            self.assertEqual(record.best_seconds, 24.1144104)
            self.assertEqual(record.best_turns, 42)
            leader._publish_federation_control.assert_awaited_once()
            leader_store.close()
            follower_store.close()


if __name__ == "__main__":
    unittest.main()
