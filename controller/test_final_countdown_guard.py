import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from final_countdown_guard import assess_progress, build_route_model
from TronnerRacing import Player, TronnerRacing


def test_map(field: str, axes: str = '<Axes number="4"/>') -> str:
    return f'''<?xml version="1.0"?>
<Resource type="aamap" name="Guard" version="v1" author="Test" category="maps">
  <Map version="0.2.8"><World><Field>
    {axes}
    <Spawn x="80" y="50" xdir="0" ydir="1"/>
    {field}
  </Field></World></Map>
</Resource>'''


class RouteModelTests(unittest.TestCase):
    def build(self, field: str, axes: str = '<Axes number="4"/>'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "guard.aamap.xml"
        path.write_text(test_map(field, axes), encoding="utf-8")
        model = build_route_model(
            path,
            maximum_cells=30_000,
            minimum_cell_size=1.0,
        )
        self.assertIsNotNone(model)
        return model

    def test_route_distance_rewards_required_detour_away_from_winzone(self):
        model = self.build(
            '''
            <Zone effect="win"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>
            <Wall><Point x="0" y="0"/><Point x="100" y="0"/><Point x="100" y="100"/><Point x="0" y="100"/><Point x="0" y="0"/></Wall>
            <Wall><Point x="50" y="0"/><Point x="50" y="80"/></Wall>
            '''
        )

        start = (80.0, 50.0)
        detour = (80.0, 65.0)
        self.assertGreater(
            math.dist(detour, (20.0, 50.0)),
            math.dist(start, (20.0, 50.0)),
        )
        self.assertLess(model.distance_at(detour), model.distance_at(start))
        trajectory = tuple(
            (float(index), point[0], point[1], model.distance_at(point))
            for index, point in enumerate(
                ((80.0, 50.0), (80.0, 60.0), (80.0, 70.0))
            )
        )
        self.assertIsNone(
            assess_progress(
                trajectory,
                remaining_seconds=40,
                total_seconds=60,
                reference_distance=model.reference_distance,
                reference_seconds=20,
            )
        )

    def test_death_zone_is_treated_as_route_obstacle(self):
        model = self.build(
            '''
            <Zone effect="win"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>
            <Zone effect="death"><ShapeCircle radius="12"><Point x="50" y="50"/></ShapeCircle></Zone>
            <Wall><Point x="0" y="0"/><Point x="100" y="0"/><Point x="100" y="100"/><Point x="0" y="100"/><Point x="0" y="0"/></Wall>
            '''
        )

        route_distance = model.distance_at((80.0, 50.0))
        direct_distance = model.direct_distance((80.0, 50.0))
        self.assertGreater(route_distance, direct_distance + 5)

    def test_custom_axes_are_used_by_navigation_field(self):
        model = self.build(
            '<Zone effect="win"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>',
            '''<Axes number="4">
                 <Axis xdir="1" ydir="0"/><Axis xdir="0" ydir="1"/>
                 <Axis xdir="-1" ydir="0"/><Axis xdir="0" ydir="-1"/>
               </Axes>''',
        )
        self.assertEqual(len(model.geometry.axis_directions), 4)
        self.assertTrue(math.isfinite(model.distance_at((80.0, 50.0))))


class ProgressAssessmentTests(unittest.TestCase):
    def test_high_speed_circle_is_a_violation(self):
        assessment = assess_progress(
            (
                (0.0, 0.0, 0.0, 50.0),
                (1.0, 10.0, 0.0, 50.0),
                (2.0, 10.0, 10.0, 50.0),
                (3.0, 0.0, 10.0, 50.0),
            ),
            remaining_seconds=20,
            total_seconds=90,
            reference_distance=100,
            reference_seconds=10,
        )
        self.assertIsNotNone(assessment)
        self.assertEqual(assessment.reason, "moving without making route progress")

    def test_same_pace_becomes_insufficient_late_in_countdown(self):
        samples = (
            (0.0, 0.0, 0.0, 56.0),
            (3.0, 6.0, 0.0, 50.0),
        )
        early = assess_progress(
            samples,
            remaining_seconds=80,
            total_seconds=90,
            reference_distance=100,
            reference_seconds=10,
        )
        late = assess_progress(
            samples,
            remaining_seconds=10,
            total_seconds=90,
            reference_distance=100,
            reference_seconds=10,
        )
        self.assertIsNone(early)
        self.assertIsNotNone(late)


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class ConstantRouteModel:
    cell_size = 1.0
    reference_distance = 100.0

    def distance_at(self, _position):
        return 50.0


class CountdownEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_non_progress_is_warned_then_killed(self):
        controller = object.__new__(TronnerRacing)
        controller.config = {
            "final_countdown_grief_detection_enabled": True,
            "final_countdown_grief_early_window_seconds": 2,
            "final_countdown_grief_late_window_seconds": 2,
            "final_countdown_grief_early_grace_seconds": 1,
            "final_countdown_grief_late_grace_seconds": 1,
            "final_countdown_grief_route_slack_distance": 0,
        }
        controller.current = SimpleNamespace(key="Test/maps/Guard-v1.aamap.xml")
        controller.final_countdown_active = True
        controller.final_countdown_end_epoch = 20.0
        controller.final_countdown_duration_seconds = 20.0
        controller.final_countdown_reference_seconds = 10.0
        controller.final_countdown_route_model = ConstantRouteModel()
        controller.final_countdown_route_map_key = controller.current.key
        controller.final_countdown_progress_states = {}
        controller.sink = Sink()
        messages = []

        async def private(_player, message):
            messages.append(message)

        controller.private = private
        player = Player("racer", "Racer", connected=True, active=True, alive=True)
        controller.finalists = {id(player)}

        with mock.patch("TronnerRacing.time.time", return_value=7.0) as clock:
            await controller._record_final_countdown_progress(player, 0.0, (0.0, 0.0))
            clock.return_value = 8.0
            await controller._record_final_countdown_progress(player, 1.0, (10.0, 0.0))
            clock.return_value = 9.0
            await controller._record_final_countdown_progress(player, 2.0, (10.0, 10.0))
            clock.return_value = 10.0
            await controller._record_final_countdown_progress(player, 3.0, (0.0, 10.0))

        self.assertTrue(any("warning" in message.casefold() for message in messages))
        self.assertEqual(controller.sink.commands, ["KILL_SILENT racer"])

    async def test_incomplete_checkpoint_route_is_not_judged_against_winzone(self):
        controller = object.__new__(TronnerRacing)
        controller.config = {"final_countdown_grief_detection_enabled": True}
        controller.current = SimpleNamespace(
            key="Test/maps/Checkpoint-v1.aamap.xml",
            checkpoint_ids=(1,),
        )
        controller.final_countdown_active = True
        controller.final_countdown_end_epoch = 20.0
        controller.final_countdown_route_map_key = controller.current.key
        controller.final_countdown_route_model = ConstantRouteModel()
        controller.final_countdown_progress_states = {}
        controller.sink = Sink()
        messages = []

        async def private(_player, message):
            messages.append(message)

        controller.private = private
        player = Player("racer", "Racer", connected=True, active=True, alive=True)
        controller.finalists = {id(player)}

        await controller._record_final_countdown_progress(
            player, 1.0, (10.0, 10.0)
        )

        self.assertEqual(controller.sink.commands, [])
        self.assertEqual(messages, [])

    async def test_unmodelled_map_idle_fallback_warns_before_kill(self):
        controller = object.__new__(TronnerRacing)
        controller.config = {
            "final_countdown_grief_detection_enabled": True,
            "final_countdown_idle_seconds": 10,
            "final_countdown_grief_early_grace_seconds": 1,
            "final_countdown_grief_late_grace_seconds": 1,
        }
        controller.current = SimpleNamespace(
            key="Test/maps/Unmodelled-v1.aamap.xml",
            checkpoint_ids=(),
        )
        controller.final_countdown_active = True
        controller.final_countdown_end_epoch = 20.0
        controller.final_countdown_duration_seconds = 20.0
        controller.final_countdown_route_map_key = controller.current.key
        controller.final_countdown_route_model = None
        controller.final_countdown_route_building = False
        controller.final_countdown_progress_states = {}
        controller.sink = Sink()
        messages = []

        async def private(_player, message):
            messages.append(message)

        controller.private = private
        player = Player("racer", "Racer", connected=True, active=True, alive=True)
        controller.finalists = {id(player)}

        with mock.patch("TronnerRacing.time.time", return_value=8.0) as clock:
            await controller._record_final_countdown_progress(
                player, 0.0, (0.0, 0.0), native_idle_seconds=11.0
            )
            self.assertEqual(controller.sink.commands, [])
            clock.return_value = 10.0
            await controller._record_final_countdown_progress(
                player, 2.0, (0.0, 0.0), native_idle_seconds=13.0
            )

        self.assertTrue(any("warning" in message.casefold() for message in messages))
        self.assertEqual(controller.sink.commands, ["KILL_SILENT racer"])


if __name__ == "__main__":
    unittest.main()
