import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from final_countdown_guard import (
    AccelerationCapability,
    accelerated_travel_distance,
    assess_progress,
    build_route_model,
)
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
    def build(
        self,
        field: str,
        axes: str = '<Axes number="4"/>',
        size_multiplier: float = 1.0,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "guard.aamap.xml"
        path.write_text(test_map(field, axes), encoding="utf-8")
        model = build_route_model(
            path,
            maximum_cells=30_000,
            minimum_cell_size=1.0,
            size_multiplier=size_multiplier,
            narrow_passage_guides=True,
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
        trajectory = (
            (0.0, 80.0, 50.0, model.distance_at((80.0, 50.0)), 0.0),
            (1.0, 80.0, 60.0, model.distance_at((80.0, 60.0)), 10.0),
            (2.0, 80.0, 70.0, model.distance_at((80.0, 70.0)), 20.0),
        )
        self.assertIsNone(
            assess_progress(
                trajectory,
                remaining_seconds=40,
            )
        )

    def test_wall_between_grid_centers_cannot_disappear(self):
        model = self.build(
            '''
            <Zone effect="win"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>
            <Wall><Point x="0" y="0"/><Point x="100" y="0"/><Point x="100" y="100"/><Point x="0" y="100"/><Point x="0" y="0"/></Wall>
            <Wall><Point x="50.5" y="0"/><Point x="50.5" y="100"/></Wall>
            '''
        )

        self.assertTrue(math.isinf(model.distance_at((80.0, 50.0))))

    def test_every_spawn_must_have_a_certified_route(self):
        model = self.build(
            '''
            <Spawn x="30" y="50" xdir="1" ydir="0"/>
            <Zone effect="win"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>
            <Wall><Point x="0" y="0"/><Point x="100" y="0"/><Point x="100" y="100"/><Point x="0" y="100"/><Point x="0" y="0"/></Wall>
            <Wall><Point x="50" y="0"/><Point x="50" y="100"/></Wall>
            '''
        )

        self.assertTrue(math.isfinite(model.distance_at((30.0, 50.0))))
        self.assertTrue(math.isinf(model.distance_at((80.0, 50.0))))
        self.assertEqual(model.reference_distance, 0.0)

    def test_narrow_wall_to_wall_corridor_remains_reachable(self):
        model = self.build(
            '''
            <Zone effect="win"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>
            <Wall><Point x="0" y="0"/><Point x="100" y="0"/><Point x="100" y="100"/><Point x="0" y="100"/><Point x="0" y="0"/></Wall>
            <Wall><Point x="0" y="49.4"/><Point x="100" y="49.4"/></Wall>
            <Wall><Point x="0" y="50.6"/><Point x="100" y="50.6"/></Wall>
            '''
        )

        self.assertTrue(math.isfinite(model.distance_at((80.0, 50.0))))

    def test_subcell_wall_to_wall_corridor_uses_sparse_passage_guide(self):
        model = self.build(
            '''
            <Spawn x="250" y="0.137" xdir="-1" ydir="0"/>
            <Zone effect="win"><ShapeCircle radius="0.01"><Point x="-250" y="0.137"/></ShapeCircle></Zone>
            <Wall><Point x="-320" y="-160"/><Point x="320" y="-160"/><Point x="320" y="160"/><Point x="-320" y="160"/><Point x="-320" y="-160"/></Wall>
            <Wall><Point x="-300" y="0.087"/><Point x="300" y="0.087"/></Wall>
            <Wall><Point x="-300" y="0.187"/><Point x="300" y="0.187"/></Wall>
            '''
        )

        self.assertTrue(math.isfinite(model.distance_at((250.0, 0.137))))
        self.assertTrue(model.guide_points)

    def test_narrow_zone_to_zone_corridor_remains_reachable(self):
        model = self.build(
            '''
            <Zone effect="win"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>
            <Zone effect="death"><ShapeCircle radius="3.5"><Point x="50" y="46"/></ShapeCircle></Zone>
            <Zone effect="death"><ShapeCircle radius="3.5"><Point x="50" y="54"/></ShapeCircle></Zone>
            <Wall><Point x="0" y="0"/><Point x="100" y="0"/><Point x="100" y="100"/><Point x="0" y="100"/><Point x="0" y="0"/></Wall>
            '''
        )

        self.assertTrue(math.isfinite(model.distance_at((80.0, 50.0))))

    def test_subcell_zone_to_zone_corridor_uses_sparse_passage_guide(self):
        model = self.build(
            '''
            <Zone effect="win"><ShapeCircle radius="0.05"><Point x="20" y="50.137"/></ShapeCircle></Zone>
            <Zone effect="death"><ShapeCircle radius="3.85"><Point x="50" y="46.137"/></ShapeCircle></Zone>
            <Zone effect="death"><ShapeCircle radius="3.85"><Point x="50" y="54.137"/></ShapeCircle></Zone>
            <Wall><Point x="0" y="42.287"/><Point x="100" y="42.287"/></Wall>
            <Wall><Point x="0" y="57.987"/><Point x="100" y="57.987"/></Wall>
            '''
        )

        self.assertTrue(math.isfinite(model.distance_at((80.0, 50.137))))

    def test_narrow_wall_to_zone_corridor_remains_reachable(self):
        model = self.build(
            '''
            <Zone effect="win"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>
            <Zone effect="death"><ShapeCircle radius="3"><Point x="50" y="53.2"/></ShapeCircle></Zone>
            <Wall><Point x="0" y="0"/><Point x="100" y="0"/><Point x="100" y="100"/><Point x="0" y="100"/><Point x="0" y="0"/></Wall>
            <Wall><Point x="0" y="49.5"/><Point x="100" y="49.5"/></Wall>
            '''
        )

        self.assertTrue(math.isfinite(model.distance_at((80.0, 50.0))))

    def test_subcell_wall_to_zone_corridor_uses_sparse_passage_guide(self):
        model = self.build(
            '''
            <Zone effect="win"><ShapeCircle radius="0.05"><Point x="20" y="50"/></ShapeCircle></Zone>
            <Zone effect="death"><ShapeCircle radius="3.85"><Point x="50" y="54"/></ShapeCircle></Zone>
            <Wall><Point x="0" y="49.85"/><Point x="100" y="49.85"/></Wall>
            '''
        )

        self.assertTrue(math.isfinite(model.distance_at((80.0, 50.0))))

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

    def test_target_zone_is_treated_as_finish_goal(self):
        model = self.build(
            '<Zone effect="target"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>'
        )

        self.assertTrue(math.isfinite(model.distance_at((80.0, 50.0))))
        self.assertEqual(len(model.geometry.win_circles), 1)

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

    def test_absolute_teleport_is_a_route_edge_through_a_dividing_wall(self):
        model = self.build(
            '''
            <Zone effect="win"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>
            <Zone effect="teleport"><ShapeCircle radius="5"><Point x="80" y="50"/><Teleport destX="40" destY="50" modes="abs" reloc="0"/></ShapeCircle></Zone>
            <Wall><Point x="0" y="0"/><Point x="100" y="0"/><Point x="100" y="100"/><Point x="0" y="100"/><Point x="0" y="0"/></Wall>
            <Wall><Point x="50" y="0"/><Point x="50" y="100"/></Wall>
            '''
        )

        self.assertEqual(len(model.geometry.teleports), 1)
        self.assertTrue(math.isfinite(model.distance_at((80.0, 50.0))))
        self.assertLess(model.distance_at((80.0, 50.0)), 30.0)
        self.assertLess(
            model.observed_travel_distance((90.0, 50.0), (42.0, 50.0)),
            10.0,
        )

    def test_relative_and_cycle_teleports_are_modelled(self):
        for mode, destination in (("rel", -60), ("cycle", 60)):
            with self.subTest(mode=mode):
                model = self.build(
                    f'''
                    <Zone effect="win"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>
                    <Zone effect="teleport"><ShapeCircle radius="5"><Point x="80" y="50"/><Teleport destX="{destination}" destY="0" modes="{mode}" reloc="1"/></ShapeCircle></Zone>
                    <Wall><Point x="0" y="0"/><Point x="100" y="0"/><Point x="100" y="100"/><Point x="0" y="100"/><Point x="0" y="0"/></Wall>
                    <Wall><Point x="50" y="0"/><Point x="50" y="100"/></Wall>
                    '''
                )
                self.assertTrue(math.isfinite(model.distance_at((90.0, 50.0))))

    def test_map_size_multiplier_scales_teleport_geometry_and_destination(self):
        model = self.build(
            '''
            <Zone effect="win"><ShapeCircle radius="3"><Point x="20" y="50"/></ShapeCircle></Zone>
            <Zone effect="teleport"><ShapeCircle radius="5"><Point x="80" y="50"/><Teleport destX="40" destY="50" modes="abs"/></ShapeCircle></Zone>
            ''',
            size_multiplier=2.0,
        )

        teleport = model.geometry.teleports[0]
        self.assertEqual(teleport.entrance.center, (160.0, 100.0))
        self.assertEqual(teleport.entrance.radius, 10.0)
        self.assertEqual(teleport.destination, (80.0, 100.0))


class ProgressAssessmentTests(unittest.TestCase):
    def test_fast_circle_can_reach_but_fails_constant_progress(self):
        assessment = assess_progress(
            (
                (0.0, 0.0, 0.0, 50.0, 0.0),
                (1.0, 10.0, 0.0, 50.0, 10.0),
                (2.0, 10.0, 10.0, 50.0, 20.0),
                (3.0, 0.0, 10.0, 50.0, 30.0),
            ),
            remaining_seconds=20,
            route_slack_distance=0,
        )
        self.assertIsNotNone(assessment)
        self.assertTrue(assessment.can_finish)
        self.assertFalse(assessment.making_progress)
        self.assertEqual(
            assessment.reason,
            "you are not making consistent progress toward the winzone",
        )

    def test_current_position_and_speed_determine_whether_finish_is_possible(self):
        samples = (
            (0.0, 0.0, 0.0, 22.0, 0.0),
            (2.0, 4.0, 0.0, 18.0, 4.0),
        )
        enough_time = assess_progress(
            samples,
            remaining_seconds=10,
            route_slack_distance=0,
        )
        too_late = assess_progress(
            samples,
            remaining_seconds=8,
            route_slack_distance=0,
        )
        self.assertIsNone(enough_time)
        self.assertIsNotNone(too_late)
        self.assertFalse(too_late.can_finish)
        self.assertTrue(too_late.making_progress)
        self.assertAlmostEqual(too_late.projected_seconds, 9.0)

    def test_slow_forward_progress_fails_reachability_only(self):
        assessment = assess_progress(
            (
                (0.0, 0.0, 0.0, 50.0, 0.0),
                (2.0, 2.0, 0.0, 48.0, 2.0),
            ),
            remaining_seconds=10,
            route_slack_distance=0,
        )
        self.assertIsNotNone(assessment)
        self.assertFalse(assessment.can_finish)
        self.assertTrue(assessment.making_progress)
        self.assertEqual(
            assessment.reason,
            "your projected pace cannot reach the winzone before time expires",
        )

    def test_base_speed_recovery_can_make_a_run_reachable(self):
        samples = (
            (0.0, 0.0, 0.0, 900.0, 0.0),
            (2.0, 100.0, 0.0, 800.0, 100.0),
        )
        self.assertIsNotNone(
            assess_progress(
                samples,
                remaining_seconds=10,
                route_slack_distance=0,
            )
        )
        capability = AccelerationCapability.from_settings(
            {
                "CYCLE_SPEED": "125",
                "CYCLE_SPEED_DECAY_BELOW": "0.2",
                "CYCLE_SPEED_DECAY_ABOVE": "0.2",
                "CYCLE_ACCEL": "0",
                "REAL_CYCLE_SPEED_FACTOR": "1",
            }
        )
        expected_distance = 1250 + (50 - 125) * (1 - math.exp(-2)) / 0.2
        self.assertAlmostEqual(
            accelerated_travel_distance(50, 10, capability),
            expected_distance,
        )
        self.assertIsNone(
            assess_progress(
                samples,
                remaining_seconds=10,
                route_slack_distance=0,
                acceleration_capability=capability,
            )
        )

    def test_wall_acceleration_envelope_uses_engine_settings(self):
        capability = AccelerationCapability.from_settings(
            {
                "CYCLE_SPEED": "10",
                "CYCLE_ACCEL": "10",
                "CYCLE_ACCEL_SELF": "1",
                "CYCLE_ACCEL_TEAM": "1",
                "CYCLE_ACCEL_ENEMY": "1",
                "CYCLE_ACCEL_RIM": "0",
                "CYCLE_ACCEL_SLINGSHOT": "1",
                "CYCLE_ACCEL_TUNNEL": "1",
                "CYCLE_ACCEL_OFFSET": "2",
                "CYCLE_WALL_NEAR": "6",
            }
        )
        self.assertAlmostEqual(capability.external_acceleration, 7.5)

    def test_controller_reads_active_engine_acceleration_snapshot(self):
        class SettingsStore:
            @staticmethod
            def replay_settings_ref(identifier):
                return 7 if identifier == "active" else None

            @staticmethod
            def dashboard_replay_settings(settings_ref):
                self.assertEqual(settings_ref, 7)
                return {
                    "settings": [
                        ["CYCLE_SPEED", "125"],
                        ["CYCLE_SPEED_DECAY_BELOW", "0.2"],
                        ["CYCLE_SPEED_DECAY_ABOVE", "0.2"],
                        ["CYCLE_ACCEL", "0"],
                        ["REAL_CYCLE_SPEED_FACTOR", "1"],
                    ]
                }

        controller = object.__new__(TronnerRacing)
        controller.active_replay_settings_identifier = "active"
        controller.store = SettingsStore()
        capability = controller._active_acceleration_capability()

        self.assertIsNotNone(capability)
        self.assertEqual(capability.base_speed, 125)
        self.assertEqual(capability.decay_below, 0.2)


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

    def observed_travel_distance(self, start, end):
        return math.dist(start, end)


class LinearRouteModel:
    cell_size = 1.0
    reference_distance = 50.0

    def distance_at(self, position):
        return max(0.0, 50.0 - position[0])

    def observed_travel_distance(self, start, end):
        return math.dist(start, end)


class CountdownEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_insufficient_finish_pace_is_warned_then_killed(self):
        controller = object.__new__(TronnerRacing)
        controller.config = {
            "final_countdown_grief_detection_enabled": True,
            "final_countdown_grief_early_window_seconds": 2,
            "final_countdown_grief_late_window_seconds": 2,
            "final_countdown_grief_early_grace_seconds": 1,
            "final_countdown_grief_late_grace_seconds": 1,
            "final_countdown_grief_route_slack_distance": 0,
        }
        controller.current = SimpleNamespace(key="Test/maps/Pace-v1.aamap.xml")
        controller.final_countdown_active = True
        controller.final_countdown_end_epoch = 20.0
        controller.final_countdown_duration_seconds = 20.0
        controller.final_countdown_route_model = LinearRouteModel()
        controller.final_countdown_route_map_key = controller.current.key
        controller.final_countdown_progress_states = {}
        controller.sink = Sink()
        messages = []

        async def private(_player, message):
            messages.append(message)

        controller.private = private
        player = Player("racer", "Racer", connected=True, active=True, alive=True)
        controller.finalists = {id(player)}

        with mock.patch("TronnerRacing.time.time", return_value=8.0) as clock:
            await controller._record_final_countdown_progress(player, 0.0, (0.0, 0.0))
            clock.return_value = 9.0
            await controller._record_final_countdown_progress(player, 1.0, (1.0, 0.0))
            clock.return_value = 10.0
            await controller._record_final_countdown_progress(player, 2.0, (2.0, 0.0))
            clock.return_value = 11.0
            await controller._record_final_countdown_progress(player, 3.0, (3.0, 0.0))

        self.assertTrue(
            any("projected pace cannot reach" in message for message in messages)
        )
        self.assertEqual(controller.sink.commands, ["KILL_SILENT racer"])

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
