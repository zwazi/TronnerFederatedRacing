import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path

from TronnerRacing import MapEntry, StateStore, TronnerRacing


class Repository:
    def __init__(self, entry):
        self.entry = entry

    def find_by_spec(self, spec):
        if spec == self.entry.key:
            return self.entry
        return None


class TransitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_current_map_revokes_stale_restart_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = MapEntry(
                "Tester/maps/Bad-v1.aamap.xml",
                "Bad",
                "Tester",
                "v1",
                "maps",
                "Bad",
                root / "Bad-v1.aamap.xml",
                (),
            )
            fallback = MapEntry(
                "Tester/maps/Good-v1.aamap.xml",
                "Good",
                "Tester",
                "v1",
                "maps",
                "Good",
                root / "Good-v1.aamap.xml",
                (),
            )

            class MultiRepository:
                def find_by_spec(self, spec):
                    return {target.key: target, fallback.key: fallback}.get(spec)

            controller = object.__new__(TronnerRacing)
            controller.config = {"map_duration_seconds": 300}
            controller.repository = MultiRepository()
            controller.store = StateStore(root / "state.sqlite3")
            controller.players = {}
            controller.current = target
            controller.current_spec = target.key
            controller.current_size_factor = 1.0
            controller.deadline_epoch = 1.0
            controller.restoring_saved_map = False
            controller.transitioning = True
            controller.transition_target_key = target.key
            controller.transition_map_confirmed = True
            controller.transition_observed_key = target.key
            controller.extend_votes = set()
            controller.skip_votes = set()
            controller.respawn_tasks = {}
            controller.freeze_tasks = {}
            controller.center_clear_tasks = {}
            controller.race_finish_paths = {}
            controller.store.set_json("current_key", target.key)

            await controller._handle_current_map(f"0 1 {fallback.key}")

            self.assertEqual(controller.current, fallback)
            self.assertEqual(controller.transition_observed_key, fallback.key)
            self.assertFalse(controller.transition_map_confirmed)
            self.assertTrue(controller.transitioning)
            controller.store.close()

    async def test_restart_completes_transition_when_target_round_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = MapEntry(
                "Tester/maps/Race-v2.aamap.xml",
                "Race",
                "Tester",
                "v2",
                "maps",
                "Race",
                root / "Race-v2.aamap.xml",
                (),
            )
            online = root / "online_players.txt"
            online.write_text(entry.key + "\n", encoding="utf-8")
            ladderlog = root / "ladderlog.txt"
            ladderlog.write_text(
                "CURRENT_MAP 1 1.41421 " + entry.key + "\n"
                "ROUND_STARTED 2026-08-26 00:00:10 UTC\n"
                "GAME_TIME 25 25.0\n",
                encoding="utf-8",
            )

            controller = object.__new__(TronnerRacing)
            controller.config = {
                "online_players_file": str(online),
                "ladderlog": str(ladderlog),
            }
            controller.repository = Repository(entry)
            controller.store = StateStore(root / "state.sqlite3")
            controller.players = {}
            controller.aliases = {}
            controller.online_snapshot_misses = {}
            controller.finalists = set()
            controller.respawn_tasks = {}
            controller.freeze_tasks = {}
            controller.center_clear_tasks = {}
            controller.current = None
            controller.current_spec = None
            controller.round_active = False
            controller.transitioning = True
            controller.transition_target_key = entry.key
            controller.transition_map_confirmed = False
            controller.final_countdown_active = False
            controller.final_countdown_map_key = None
            controller.last_game_time = None
            controller.last_game_monotonic = None

            controller._restore_runtime_context()

            self.assertEqual(controller.current, entry)
            self.assertTrue(controller.round_active)
            self.assertFalse(controller.transitioning)
            self.assertIsNone(controller.transition_target_key)
            self.assertFalse(
                controller.store.get_json("transitioning", True)
            )
            self.assertIsNone(
                controller.store.get_json("transition_target_key", "missing")
            )
            self.assertEqual(controller.last_game_time, 25.0)
            controller.store.close()

    async def test_stale_round_start_cannot_finish_pending_map_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = object.__new__(TronnerRacing)
            entry = MapEntry(
                "Tester/maps/Race-v2.aamap.xml",
                "Race",
                "Tester",
                "v2",
                "maps",
                "Race",
                Path(tmp) / "Race-v2.aamap.xml",
                (),
            )
            controller.config = {"map_duration_seconds": 300}
            controller.store = StateStore(Path(tmp) / "state.sqlite3")
            controller.repository = Repository(entry)
            controller.current = entry
            controller.current_spec = entry.key
            controller.current_size_factor = 1.0
            controller.deadline_epoch = 1.0
            controller.players = {}
            controller.round_active = False
            controller.restoring_saved_map = False
            controller.extend_votes = set()
            controller.skip_votes = set()
            controller.respawn_tasks = {}
            controller.freeze_tasks = {}
            controller.center_clear_tasks = {}
            controller.race_finish_paths = {}
            controller._display_task = None
            controller._begin_helpful_message_round = lambda: None

            async def delayed_round_display(**_kwargs):
                await asyncio.sleep(60)

            controller._delayed_round_display = delayed_round_display
            controller.store.set_json("current_key", entry.key)

            controller._begin_map_transition(entry.key)

            # This is the old map's event, queued while /size was creating and
            # caching the revised map.
            await controller.handle_line("ROUND_STARTED 2026-08-26 00:00:00 UTC")
            self.assertTrue(controller.transitioning)
            self.assertFalse(controller.round_active)
            self.assertFalse(controller.transition_round_started_pending)

            # The target round can start before an explicit map probe responds.
            # Remember that event, but do not activate it until CURRENT_MAP
            # confirms the requested revision.
            target_started = datetime.datetime.now(
                datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
            await controller.handle_line(f"ROUND_STARTED {target_started}")
            self.assertTrue(controller.transitioning)
            self.assertTrue(controller.transition_round_started_pending)

            await controller.handle_line(f"CURRENT_MAP 1 1.41421 {entry.key}")
            self.assertFalse(controller.transitioning)
            self.assertTrue(controller.round_active)
            self.assertGreater(controller.deadline_epoch, 1.0)

            controller._display_task.cancel()
            await asyncio.gather(controller._display_task, return_exceptions=True)
            controller.store.close()


if __name__ == "__main__":
    unittest.main()
