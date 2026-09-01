"""Route-aware final-countdown progress checks.

The game keeps moving cycles without player input, so neither input-idle time
nor raw movement is sufficient to decide whether a remaining racer is making
a good-faith attempt to finish.  This module turns the current map into a
bounded navigation field and measures progress through that field.

The field is deliberately approximate.  It is used to recognize sustained
non-progress, never to steer a cycle or validate a finish.  Static map walls
and death zones are obstacles, win zones are goals, and moves are quantized to
the map's configured axes.
"""

from __future__ import annotations

import collections
import dataclasses
import heapq
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Sequence


Point = tuple[float, float]
TimedProgressSample = tuple[float, float, float, float]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _angular_difference(first: float, second: float) -> float:
    return abs((first - second + math.pi) % (2 * math.pi) - math.pi)


def _point_segment_distance(point: Point, start: Point, end: Point) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-12:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    amount = max(
        0.0,
        min(
            1.0,
            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy)
            / length_squared,
        ),
    )
    nearest = (start[0] + amount * dx, start[1] + amount * dy)
    return math.hypot(point[0] - nearest[0], point[1] - nearest[1])


def _point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current[1] > point[1]) != (previous[1] > point[1]):
            crossed_x = (
                (previous[0] - current[0])
                * (point[1] - current[1])
                / (previous[1] - current[1])
                + current[0]
            )
            if point[0] < crossed_x:
                inside = not inside
        previous = current
    return inside


@dataclasses.dataclass(frozen=True)
class Circle:
    center: Point
    radius: float


@dataclasses.dataclass(frozen=True)
class MapGeometry:
    axis_directions: tuple[Point, ...]
    wall_segments: tuple[tuple[Point, Point], ...]
    death_circles: tuple[Circle, ...]
    death_polygons: tuple[tuple[Point, ...], ...]
    win_circles: tuple[Circle, ...]
    win_polygons: tuple[tuple[Point, ...], ...]
    spawns: tuple[Point, ...]

    @property
    def has_goal(self) -> bool:
        return bool(self.win_circles or self.win_polygons)


def _node_direction(node: ET.Element) -> Point:
    if "angle" in node.attrib:
        angle = math.radians(float(node.attrib["angle"]))
        return math.cos(angle), math.sin(angle)
    return (
        float(node.attrib.get("xdir", node.attrib.get("x", "0"))),
        float(node.attrib.get("ydir", node.attrib.get("y", "0"))),
    )


def parse_map_geometry(path: Path) -> MapGeometry:
    root = ET.parse(path).getroot()
    axes_node = next(
        (node for node in root.iter() if _local_name(node.tag) == "Axes"),
        None,
    )
    axis_count = 4
    explicit_directions: list[Point] = []
    if axes_node is not None:
        axis_count = max(1, int(axes_node.attrib.get("number", "4")))
        for child in axes_node:
            if _local_name(child.tag) not in {"Axis", "Point"}:
                continue
            direction = _node_direction(child)
            if math.hypot(*direction) > 1e-9:
                explicit_directions.append(direction)
    if explicit_directions:
        # The game initializes omitted custom directions to (1, 0).
        explicit_directions.extend(
            [(1.0, 0.0)] * max(0, axis_count - len(explicit_directions))
        )
        directions = explicit_directions[:axis_count]
    else:
        directions = [
            (
                math.cos(2 * math.pi * index / axis_count),
                math.sin(2 * math.pi * index / axis_count),
            )
            for index in range(axis_count)
        ]
    normalized_directions = []
    for x, y in directions:
        length = math.hypot(x, y)
        if length > 1e-9:
            normalized_directions.append((x / length, y / length))

    wall_segments: list[tuple[Point, Point]] = []
    spawns: list[Point] = []
    death_circles: list[Circle] = []
    death_polygons: list[tuple[Point, ...]] = []
    win_circles: list[Circle] = []
    win_polygons: list[tuple[Point, ...]] = []

    for node in root.iter():
        name = _local_name(node.tag)
        if name == "Wall":
            points = [
                (float(child.attrib["x"]), float(child.attrib["y"]))
                for child in node
                if _local_name(child.tag) == "Point"
                and "x" in child.attrib
                and "y" in child.attrib
            ]
            wall_segments.extend(zip(points, points[1:]))
        elif name == "Spawn" and "x" in node.attrib and "y" in node.attrib:
            spawns.append((float(node.attrib["x"]), float(node.attrib["y"])))
        elif name == "Zone":
            effect = node.attrib.get("effect", "").casefold()
            if effect not in {"death", "win"}:
                continue
            circles = win_circles if effect == "win" else death_circles
            polygons = win_polygons if effect == "win" else death_polygons
            for shape in node:
                shape_name = _local_name(shape.tag)
                points = [
                    (float(point.attrib["x"]), float(point.attrib["y"]))
                    for point in shape
                    if _local_name(point.tag) == "Point"
                    and "x" in point.attrib
                    and "y" in point.attrib
                ]
                if shape_name == "ShapeCircle" and points:
                    circles.append(
                        Circle(points[0], abs(float(shape.attrib.get("radius", "0"))))
                    )
                elif shape_name == "ShapePolygon" and len(points) >= 3:
                    polygons.append(tuple(points))

    return MapGeometry(
        axis_directions=tuple(normalized_directions),
        wall_segments=tuple(wall_segments),
        death_circles=tuple(death_circles),
        death_polygons=tuple(death_polygons),
        win_circles=tuple(win_circles),
        win_polygons=tuple(win_polygons),
        spawns=tuple(spawns),
    )


def _axis_grid_moves(
    directions: Iterable[Point], maximum_component: int = 4
) -> tuple[tuple[int, int, float], ...]:
    candidates = [
        (dx, dy)
        for dx in range(-maximum_component, maximum_component + 1)
        for dy in range(-maximum_component, maximum_component + 1)
        if (dx or dy) and math.gcd(abs(dx), abs(dy)) == 1
    ]
    selected: dict[tuple[int, int], float] = {}
    for x, y in directions:
        angle = math.atan2(y, x)
        dx, dy = min(
            candidates,
            key=lambda item: (
                _angular_difference(math.atan2(item[1], item[0]), angle),
                math.hypot(*item),
            ),
        )
        selected[(dx, dy)] = math.hypot(dx, dy)
    return tuple((dx, dy, cost) for (dx, dy), cost in selected.items())


@dataclasses.dataclass
class RouteModel:
    geometry: MapGeometry
    origin_x: float
    origin_y: float
    cell_size: float
    width: int
    height: int
    blocked: bytearray
    distances: list[float]

    def _index(self, x: int, y: int) -> int:
        return y * self.width + x

    def _grid_point(self, x: int, y: int) -> Point:
        return (
            self.origin_x + x * self.cell_size,
            self.origin_y + y * self.cell_size,
        )

    def _nearest_cell(self, position: Point) -> tuple[int, int]:
        return (
            int(round((position[0] - self.origin_x) / self.cell_size)),
            int(round((position[1] - self.origin_y) / self.cell_size)),
        )

    def direct_distance(self, position: Point) -> float:
        candidates = [
            max(
                0.0,
                math.hypot(
                    position[0] - circle.center[0],
                    position[1] - circle.center[1],
                )
                - circle.radius,
            )
            for circle in self.geometry.win_circles
        ]
        for polygon in self.geometry.win_polygons:
            if _point_in_polygon(position, polygon):
                candidates.append(0.0)
            else:
                candidates.append(
                    min(
                        _point_segment_distance(position, first, second)
                        for first, second in zip(polygon, (*polygon[1:], polygon[0]))
                    )
                )
        return min(candidates) if candidates else math.inf

    def distance_at(self, position: Point, search_radius: int = 4) -> float:
        cell_x, cell_y = self._nearest_cell(position)
        best = math.inf
        for radius in range(search_radius + 1):
            found = False
            for y in range(cell_y - radius, cell_y + radius + 1):
                for x in range(cell_x - radius, cell_x + radius + 1):
                    if not (0 <= x < self.width and 0 <= y < self.height):
                        continue
                    if radius and max(abs(x - cell_x), abs(y - cell_y)) != radius:
                        continue
                    value = self.distances[self._index(x, y)]
                    if not math.isfinite(value):
                        continue
                    found = True
                    grid_point = self._grid_point(x, y)
                    best = min(
                        best,
                        value
                        + math.hypot(
                            position[0] - grid_point[0],
                            position[1] - grid_point[1],
                        ),
                    )
            if found:
                return best
        # A raster cell can become disconnected when a very narrow passage is
        # smaller than the bounded grid resolution.  Returning straight-line
        # distance here would reintroduce the exact false positive this model
        # is meant to avoid: a legitimate detour could appear to head away.
        return math.inf

    @property
    def reference_distance(self) -> float:
        values = sorted(
            value
            for value in (self.distance_at(spawn) for spawn in self.geometry.spawns)
            if math.isfinite(value) and value > 0
        )
        if not values:
            return 0.0
        return values[len(values) // 2]


def build_route_model(
    path: Path,
    *,
    maximum_cells: int = 100_000,
    minimum_cell_size: float = 1.0,
    wall_clearance_cells: float = 0.30,
) -> RouteModel | None:
    geometry = parse_map_geometry(path)
    if not geometry.has_goal:
        return None

    boundary_points: list[Point] = list(geometry.spawns)
    for start, end in geometry.wall_segments:
        boundary_points.extend((start, end))
    for circle in (*geometry.death_circles, *geometry.win_circles):
        boundary_points.extend(
            (
                (circle.center[0] - circle.radius, circle.center[1] - circle.radius),
                (circle.center[0] + circle.radius, circle.center[1] + circle.radius),
            )
        )
    for polygon in (*geometry.death_polygons, *geometry.win_polygons):
        boundary_points.extend(polygon)
    if not boundary_points:
        return None

    minimum_x = min(point[0] for point in boundary_points)
    maximum_x = max(point[0] for point in boundary_points)
    minimum_y = min(point[1] for point in boundary_points)
    maximum_y = max(point[1] for point in boundary_points)
    extent_x = max(minimum_cell_size, maximum_x - minimum_x)
    extent_y = max(minimum_cell_size, maximum_y - minimum_y)
    maximum_cells = min(500_000, max(1_000, int(maximum_cells)))
    cell_size = max(
        0.1,
        float(minimum_cell_size),
        math.sqrt(extent_x * extent_y / maximum_cells),
    )
    # Account for a two-cell margin without exceeding the configured bound.
    while True:
        width = int(math.ceil(extent_x / cell_size)) + 5
        height = int(math.ceil(extent_y / cell_size)) + 5
        if width * height <= maximum_cells * 1.05:
            break
        cell_size *= 1.05
    origin_x = minimum_x - 2 * cell_size
    origin_y = minimum_y - 2 * cell_size
    blocked = bytearray(width * height)

    def index(x: int, y: int) -> int:
        return y * width + x

    def grid_point(x: int, y: int) -> Point:
        return origin_x + x * cell_size, origin_y + y * cell_size

    def grid_bounds(
        low_x: float, low_y: float, high_x: float, high_y: float
    ) -> tuple[range, range]:
        start_x = max(0, int(math.floor((low_x - origin_x) / cell_size)))
        end_x = min(width - 1, int(math.ceil((high_x - origin_x) / cell_size)))
        start_y = max(0, int(math.floor((low_y - origin_y) / cell_size)))
        end_y = min(height - 1, int(math.ceil((high_y - origin_y) / cell_size)))
        return range(start_x, end_x + 1), range(start_y, end_y + 1)

    clearance = max(0.0, wall_clearance_cells) * cell_size
    for start, end in geometry.wall_segments:
        xs, ys = grid_bounds(
            min(start[0], end[0]) - clearance,
            min(start[1], end[1]) - clearance,
            max(start[0], end[0]) + clearance,
            max(start[1], end[1]) + clearance,
        )
        # Long diagonal walls can have very large bounding boxes.  Sampling the
        # segment is bounded by its length while still covering every crossed
        # cell and its immediate clearance neighborhood.
        if len(xs) * len(ys) > max(64, 8 * (len(xs) + len(ys))):
            steps = max(1, int(math.ceil(math.dist(start, end) / (cell_size * 0.25))))
            candidates: set[tuple[int, int]] = set()
            radius = max(1, int(math.ceil(clearance / cell_size)) + 1)
            for step in range(steps + 1):
                amount = step / steps
                px = start[0] + (end[0] - start[0]) * amount
                py = start[1] + (end[1] - start[1]) * amount
                center_x = int(round((px - origin_x) / cell_size))
                center_y = int(round((py - origin_y) / cell_size))
                for y in range(center_y - radius, center_y + radius + 1):
                    for x in range(center_x - radius, center_x + radius + 1):
                        if 0 <= x < width and 0 <= y < height:
                            candidates.add((x, y))
            cells = candidates
        else:
            cells = ((x, y) for y in ys for x in xs)
        for x, y in cells:
            if _point_segment_distance(grid_point(x, y), start, end) <= clearance:
                blocked[index(x, y)] = 1

    for circle in geometry.death_circles:
        radius = circle.radius + clearance
        xs, ys = grid_bounds(
            circle.center[0] - radius,
            circle.center[1] - radius,
            circle.center[0] + radius,
            circle.center[1] + radius,
        )
        for y in ys:
            for x in xs:
                point = grid_point(x, y)
                if math.hypot(point[0] - circle.center[0], point[1] - circle.center[1]) <= radius:
                    blocked[index(x, y)] = 1

    for polygon in geometry.death_polygons:
        xs, ys = grid_bounds(
            min(point[0] for point in polygon) - clearance,
            min(point[1] for point in polygon) - clearance,
            max(point[0] for point in polygon) + clearance,
            max(point[1] for point in polygon) + clearance,
        )
        edges = tuple(zip(polygon, (*polygon[1:], polygon[0])))
        for y in ys:
            for x in xs:
                point = grid_point(x, y)
                if _point_in_polygon(point, polygon) or any(
                    _point_segment_distance(point, start, end) <= clearance
                    for start, end in edges
                ):
                    blocked[index(x, y)] = 1

    goals: set[int] = set()
    for circle in geometry.win_circles:
        xs, ys = grid_bounds(
            circle.center[0] - circle.radius,
            circle.center[1] - circle.radius,
            circle.center[0] + circle.radius,
            circle.center[1] + circle.radius,
        )
        for y in ys:
            for x in xs:
                point = grid_point(x, y)
                if (
                    not blocked[index(x, y)]
                    and math.hypot(point[0] - circle.center[0], point[1] - circle.center[1])
                    <= circle.radius
                ):
                    goals.add(index(x, y))
    for polygon in geometry.win_polygons:
        xs, ys = grid_bounds(
            min(point[0] for point in polygon),
            min(point[1] for point in polygon),
            max(point[0] for point in polygon),
            max(point[1] for point in polygon),
        )
        for y in ys:
            for x in xs:
                if not blocked[index(x, y)] and _point_in_polygon(grid_point(x, y), polygon):
                    goals.add(index(x, y))
    if not goals:
        # Tiny zones may fall between cell centers.  Seed the nearest safe cell.
        centers = [circle.center for circle in geometry.win_circles]
        centers.extend(
            (
                sum(point[0] for point in polygon) / len(polygon),
                sum(point[1] for point in polygon) / len(polygon),
            )
            for polygon in geometry.win_polygons
        )
        for center in centers:
            center_x = int(round((center[0] - origin_x) / cell_size))
            center_y = int(round((center[1] - origin_y) / cell_size))
            for radius in range(5):
                candidates = [
                    (x, y)
                    for y in range(center_y - radius, center_y + radius + 1)
                    for x in range(center_x - radius, center_x + radius + 1)
                    if 0 <= x < width
                    and 0 <= y < height
                    and not blocked[index(x, y)]
                ]
                if candidates:
                    nearest = min(
                        candidates,
                        key=lambda cell: math.dist(grid_point(*cell), center),
                    )
                    goals.add(index(*nearest))
                    break
    if not goals:
        return None

    moves = _axis_grid_moves(geometry.axis_directions)
    distances = [math.inf] * (width * height)
    queue: list[tuple[float, int]] = []
    for goal in goals:
        distances[goal] = 0.0
        heapq.heappush(queue, (0.0, goal))

    def clear_grid_edge(start_x: int, start_y: int, end_x: int, end_y: int) -> bool:
        steps = max(abs(end_x - start_x), abs(end_y - start_y)) * 2
        for step in range(1, max(1, steps) + 1):
            amount = step / max(1, steps)
            x = int(round(start_x + (end_x - start_x) * amount))
            y = int(round(start_y + (end_y - start_y) * amount))
            if blocked[index(x, y)]:
                return False
        return True

    while queue:
        distance, current = heapq.heappop(queue)
        if distance != distances[current]:
            continue
        current_x = current % width
        current_y = current // width
        # Reverse the allowed movement edges to compute distance-to-goal.
        for move_x, move_y, move_cost in moves:
            previous_x = current_x - move_x
            previous_y = current_y - move_y
            if not (0 <= previous_x < width and 0 <= previous_y < height):
                continue
            previous = index(previous_x, previous_y)
            if blocked[previous] or not clear_grid_edge(
                previous_x, previous_y, current_x, current_y
            ):
                continue
            candidate = distance + move_cost * cell_size
            if candidate + 1e-9 < distances[previous]:
                distances[previous] = candidate
                heapq.heappush(queue, (candidate, previous))

    return RouteModel(
        geometry=geometry,
        origin_x=origin_x,
        origin_y=origin_y,
        cell_size=cell_size,
        width=width,
        height=height,
        blocked=blocked,
        distances=distances,
    )


@dataclasses.dataclass(frozen=True)
class ProgressAssessment:
    reason: str
    ground_speed: float
    route_speed: float
    required_speed: float
    route_efficiency: float


@dataclasses.dataclass
class PlayerProgressState:
    samples: collections.deque[TimedProgressSample] = dataclasses.field(
        default_factory=collections.deque
    )
    warned_at: float | None = None
    violation_started_at: float | None = None
    last_reason: str = ""
    killed: bool = False

    def clear_violation(self) -> None:
        self.warned_at = None
        self.violation_started_at = None
        self.last_reason = ""


def assess_progress(
    samples: Sequence[TimedProgressSample],
    *,
    remaining_seconds: float,
    total_seconds: float,
    reference_distance: float,
    reference_seconds: float,
    early_required_fraction: float = 0.30,
    late_required_fraction: float = 0.90,
    early_minimum_ground_fraction: float = 0.12,
    late_minimum_ground_fraction: float = 0.35,
    early_minimum_efficiency: float = 0.03,
    late_minimum_efficiency: float = 0.15,
    route_slack_distance: float = 2.0,
) -> ProgressAssessment | None:
    if len(samples) < 2:
        return None
    duration = samples[-1][0] - samples[0][0]
    if duration <= 0:
        return None
    total_seconds = max(remaining_seconds, total_seconds, 1e-6)
    elapsed_fraction = min(1.0, max(0.0, 1.0 - remaining_seconds / total_seconds))

    path_distance = sum(
        math.hypot(current[1] - previous[1], current[2] - previous[2])
        for previous, current in zip(samples, samples[1:])
    )
    ground_speed = path_distance / duration

    # A least-squares slope is more resistant to one raster-cell jump than a
    # first-to-last comparison.  Negative distance slope means goal progress.
    mean_time = sum(sample[0] for sample in samples) / len(samples)
    mean_distance = sum(sample[3] for sample in samples) / len(samples)
    time_variance = sum((sample[0] - mean_time) ** 2 for sample in samples)
    distance_covariance = sum(
        (sample[0] - mean_time) * (sample[3] - mean_distance)
        for sample in samples
    )
    route_speed = (
        -distance_covariance / time_variance if time_variance > 1e-9 else 0.0
    )
    net_progress = samples[0][3] - samples[-1][3]
    route_efficiency = net_progress / max(path_distance, 1e-9)

    required_fraction = early_required_fraction + (
        late_required_fraction - early_required_fraction
    ) * elapsed_fraction
    required_speed = (
        max(0.0, samples[-1][3])
        / max(1.0, remaining_seconds)
        * max(0.0, required_fraction)
    )
    nominal_speed = (
        reference_distance / reference_seconds
        if reference_distance > 0 and reference_seconds > 0
        else required_speed
    )
    minimum_ground_fraction = early_minimum_ground_fraction + (
        late_minimum_ground_fraction - early_minimum_ground_fraction
    ) * elapsed_fraction
    minimum_ground_speed = max(0.0, nominal_speed * minimum_ground_fraction)
    minimum_efficiency = early_minimum_efficiency + (
        late_minimum_efficiency - early_minimum_efficiency
    ) * elapsed_fraction
    slack_speed = max(0.0, route_slack_distance) / duration

    if route_speed < -slack_speed:
        reason = "driving away from the winzone route"
    elif (
        ground_speed >= max(minimum_ground_speed * 1.5, nominal_speed * 0.25)
        and route_speed < max(slack_speed, required_speed * 0.25)
        and route_efficiency < minimum_efficiency
    ):
        reason = "moving without making route progress"
    elif ground_speed + slack_speed < minimum_ground_speed:
        reason = "moving too slowly"
    elif route_speed + slack_speed < required_speed:
        reason = "not progressing fast enough to finish"
    else:
        return None
    return ProgressAssessment(
        reason=reason,
        ground_speed=ground_speed,
        route_speed=route_speed,
        required_speed=required_speed,
        route_efficiency=route_efficiency,
    )
