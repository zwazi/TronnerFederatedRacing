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
import contextlib
import dataclasses
import functools
import gzip
import hashlib
import heapq
import json
import math
import os
import pickle
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Mapping, Sequence


Point = tuple[float, float]
TimedProgressSample = tuple[float, float, float, float, float]
ROUTE_MODEL_CACHE_SCHEMA = 1
ROUTE_MODEL_CACHE_SUFFIX = ".route-model.gz"


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


def _closest_point_on_segment(point: Point, start: Point, end: Point) -> Point:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-18:
        return start
    amount = max(
        0.0,
        min(
            1.0,
            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy)
            / length_squared,
        ),
    )
    return start[0] + amount * dx, start[1] + amount * dy


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


def _segments_intersect(
    first_start: Point,
    first_end: Point,
    second_start: Point,
    second_end: Point,
) -> bool:
    """Return whether two closed line segments touch or cross."""

    scale = max(
        1.0,
        *(
            abs(value)
            for point in (first_start, first_end, second_start, second_end)
            for value in point
        ),
    )
    tolerance = 1e-10 * scale

    def orientation(start: Point, end: Point, point: Point) -> float:
        return (
            (end[0] - start[0]) * (point[1] - start[1])
            - (end[1] - start[1]) * (point[0] - start[0])
        )

    def on_segment(start: Point, end: Point, point: Point) -> bool:
        return (
            min(start[0], end[0]) - tolerance
            <= point[0]
            <= max(start[0], end[0]) + tolerance
            and min(start[1], end[1]) - tolerance
            <= point[1]
            <= max(start[1], end[1]) + tolerance
        )

    first_a = orientation(first_start, first_end, second_start)
    first_b = orientation(first_start, first_end, second_end)
    second_a = orientation(second_start, second_end, first_start)
    second_b = orientation(second_start, second_end, first_end)
    if (
        (
            (first_a > tolerance and first_b < -tolerance)
            or (first_a < -tolerance and first_b > tolerance)
        )
        and (
            (second_a > tolerance and second_b < -tolerance)
            or (second_a < -tolerance and second_b > tolerance)
        )
    ):
        return True
    return (
        (
            abs(first_a) <= tolerance
            and on_segment(first_start, first_end, second_start)
        )
        or (
            abs(first_b) <= tolerance
            and on_segment(first_start, first_end, second_end)
        )
        or (
            abs(second_a) <= tolerance
            and on_segment(second_start, second_end, first_start)
        )
        or (
            abs(second_b) <= tolerance
            and on_segment(second_start, second_end, first_end)
        )
    )


@dataclasses.dataclass(frozen=True)
class Circle:
    center: Point
    radius: float


@dataclasses.dataclass(frozen=True)
class Teleport:
    entrance: Circle
    destination: Point
    mode: str
    relocation: float

    def exits_from(
        self,
        position: Point,
        directions: Sequence[Point],
    ) -> tuple[Point, ...]:
        if self.mode == "abs":
            return (self.destination,)
        exits: list[Point] = []
        for direction in directions:
            normal = (-direction[1], direction[0])
            toward_center = (
                (self.entrance.center[0] - position[0]) * direction[0]
                + (self.entrance.center[1] - position[1]) * direction[1]
            )
            crossing_distance = 2.0 * toward_center
            if crossing_distance <= 0:
                continue
            relocation = (
                direction[0] * crossing_distance * self.relocation,
                direction[1] * crossing_distance * self.relocation,
            )
            if self.mode == "cycle":
                offset = (
                    direction[0] * self.destination[0]
                    + normal[0] * self.destination[1],
                    direction[1] * self.destination[0]
                    + normal[1] * self.destination[1],
                )
            else:
                offset = self.destination
            exits.append(
                (
                    position[0] + offset[0] + relocation[0],
                    position[1] + offset[1] + relocation[1],
                )
            )
        return tuple(exits)


@dataclasses.dataclass(frozen=True)
class MapGeometry:
    axis_directions: tuple[Point, ...]
    wall_segments: tuple[tuple[Point, Point], ...]
    death_circles: tuple[Circle, ...]
    death_polygons: tuple[tuple[Point, ...], ...]
    win_circles: tuple[Circle, ...]
    win_polygons: tuple[tuple[Point, ...], ...]
    teleports: tuple[Teleport, ...]
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


def parse_map_geometry(
    path: Path,
    *,
    size_multiplier: float = 1.0,
) -> MapGeometry:
    size_multiplier = max(1e-6, float(size_multiplier))

    def scaled_point(node: ET.Element) -> Point:
        return (
            float(node.attrib["x"]) * size_multiplier,
            float(node.attrib["y"]) * size_multiplier,
        )

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
    teleports: list[Teleport] = []

    for node in root.iter():
        name = _local_name(node.tag)
        if name == "Wall":
            points = [
                scaled_point(child)
                for child in node
                if _local_name(child.tag) == "Point"
                and "x" in child.attrib
                and "y" in child.attrib
            ]
            wall_segments.extend(zip(points, points[1:]))
        elif name == "Spawn" and "x" in node.attrib and "y" in node.attrib:
            spawns.append(scaled_point(node))
        elif name == "Zone":
            effect = node.attrib.get("effect", "").casefold()
            # TARGET_DECLARE_WINNER is enabled by default in Armagetron, so a
            # target zone is a finish goal just like a win zone for routing.
            if effect not in {"death", "target", "teleport", "win"}:
                continue
            for shape in node:
                shape_name = _local_name(shape.tag)
                points = [
                    scaled_point(point)
                    for point in shape
                    if _local_name(point.tag) == "Point"
                    and "x" in point.attrib
                    and "y" in point.attrib
                ]
                if shape_name == "ShapeCircle" and points:
                    circle = Circle(
                        points[0],
                        abs(float(shape.attrib.get("radius", "0")))
                        * size_multiplier,
                    )
                    if effect == "teleport":
                        teleport_node = next(
                            (
                                child
                                for child in shape
                                if _local_name(child.tag) == "Teleport"
                            ),
                            None,
                        )
                        if teleport_node is not None:
                            mode = teleport_node.attrib.get("modes", "abs").casefold()
                            teleports.append(
                                Teleport(
                                    entrance=circle,
                                    destination=(
                                        float(teleport_node.attrib.get("destX", "0"))
                                        * size_multiplier,
                                        float(teleport_node.attrib.get("destY", "0"))
                                        * size_multiplier,
                                    ),
                                    mode=(
                                        mode if mode in {"abs", "cycle", "rel"} else "abs"
                                    ),
                                    relocation=float(
                                        teleport_node.attrib.get("reloc", "1")
                                    ),
                                )
                            )
                    elif effect in {"target", "win"}:
                        win_circles.append(circle)
                    else:
                        death_circles.append(circle)
                elif shape_name == "ShapePolygon" and len(points) >= 3:
                    if effect in {"target", "win"}:
                        win_polygons.append(tuple(points))
                    elif effect == "death":
                        death_polygons.append(tuple(points))

    return MapGeometry(
        axis_directions=tuple(normalized_directions),
        wall_segments=tuple(wall_segments),
        death_circles=tuple(death_circles),
        death_polygons=tuple(death_polygons),
        win_circles=tuple(win_circles),
        win_polygons=tuple(win_polygons),
        teleports=tuple(teleports),
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
    guide_points: tuple[Point, ...] = ()
    guide_distances: tuple[float, ...] = ()

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

    def observed_travel_distance(self, start: Point, end: Point) -> float:
        """Estimate driven distance without charging for a teleport jump."""
        best = math.dist(start, end)
        for teleport in self.geometry.teleports:
            entrance_distance = max(
                0.0,
                math.dist(start, teleport.entrance.center)
                - teleport.entrance.radius,
            )
            entry_points = (
                teleport.entrance.center,
                *(
                    (
                        teleport.entrance.center[0]
                        - direction[0] * teleport.entrance.radius,
                        teleport.entrance.center[1]
                        - direction[1] * teleport.entrance.radius,
                    )
                    for direction in self.geometry.axis_directions
                ),
            )
            for entry in entry_points:
                for destination in teleport.exits_from(
                    entry, self.geometry.axis_directions
                ):
                    best = min(
                        best,
                        entrance_distance + math.dist(destination, end),
                    )
        return best

    def segment_is_clear(self, start: Point, end: Point) -> bool:
        """Check a short connection against the map's exact static obstacles."""
        if any(
            _segments_intersect(start, end, wall_start, wall_end)
            for wall_start, wall_end in self.geometry.wall_segments
        ):
            return False
        if any(
            _point_segment_distance(circle.center, start, end) <= circle.radius
            for circle in self.geometry.death_circles
        ):
            return False
        for polygon in self.geometry.death_polygons:
            if _point_in_polygon(start, polygon) or _point_in_polygon(end, polygon):
                return False
            if any(
                _segments_intersect(start, end, edge_start, edge_end)
                for edge_start, edge_end in zip(
                    polygon, (*polygon[1:], polygon[0])
                )
            ):
                return False
        return True

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
                    grid_point = self._grid_point(x, y)
                    if not self.segment_is_clear(position, grid_point):
                        continue
                    found = True
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
        maximum_guide_distance = (search_radius + 1) * self.cell_size
        for point, value in zip(self.guide_points, self.guide_distances):
            connection = math.dist(position, point)
            if (
                math.isfinite(value)
                and connection <= maximum_guide_distance
                and self.segment_is_clear(position, point)
            ):
                best = min(best, value + connection)
        if math.isfinite(best):
            return best
        # A raster cell can become disconnected when a very narrow passage is
        # smaller than the bounded grid resolution.  Returning straight-line
        # distance here would reintroduce the exact false positive this model
        # is meant to avoid: a legitimate detour could appear to head away.
        return math.inf

    @property
    def reference_distance(self) -> float:
        values = [self.distance_at(spawn) for spawn in self.geometry.spawns]
        if not values or any(not math.isfinite(value) or value <= 0 for value in values):
            return 0.0
        values.sort()
        return values[len(values) // 2]


def build_route_model(
    path: Path,
    *,
    maximum_cells: int = 100_000,
    minimum_cell_size: float = 1.0,
    wall_clearance_cells: float = 0.0,
    size_multiplier: float = 1.0,
    narrow_passage_guides: bool = False,
) -> RouteModel | None:
    geometry = parse_map_geometry(path, size_multiplier=size_multiplier)
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
    for teleport in geometry.teleports:
        radius = teleport.entrance.radius
        boundary_points.extend(
            (
                (
                    teleport.entrance.center[0] - radius,
                    teleport.entrance.center[1] - radius,
                ),
                (
                    teleport.entrance.center[0] + radius,
                    teleport.entrance.center[1] + radius,
                ),
            )
        )
        if teleport.mode == "abs":
            boundary_points.append(teleport.destination)
            continue
        for direction in geometry.axis_directions:
            entry = (
                teleport.entrance.center[0] - direction[0] * radius,
                teleport.entrance.center[1] - direction[1] * radius,
            )
            boundary_points.extend(
                teleport.exits_from(entry, geometry.axis_directions)
            )
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

    # Clearance is optional. Exact continuous edge checks below keep zero-width
    # walls and zone boundaries solid without widening them enough to erase a
    # legitimate narrow passage.
    clearance = max(0.0, wall_clearance_cells) * cell_size
    if clearance > 0:
        for start, end in geometry.wall_segments:
            xs, ys = grid_bounds(
                min(start[0], end[0]) - clearance,
                min(start[1], end[1]) - clearance,
                max(start[0], end[0]) + clearance,
                max(start[1], end[1]) + clearance,
            )
            for y in ys:
                for x in xs:
                    if (
                        _point_segment_distance(grid_point(x, y), start, end)
                        <= clearance
                    ):
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

    # Index exact obstacle boundaries in coarse spatial bins. Navigation edges
    # are short, so each lookup examines only nearby geometry instead of every
    # wall and zone in the map.
    segment_obstacles = list(geometry.wall_segments)
    for polygon in geometry.death_polygons:
        segment_obstacles.extend(zip(polygon, (*polygon[1:], polygon[0])))
    bin_size = max(cell_size * 8.0, 1e-6)
    segment_bins: dict[tuple[int, int], set[int]] = collections.defaultdict(set)
    circle_bins: dict[tuple[int, int], set[int]] = collections.defaultdict(set)

    def spatial_bin(point: Point) -> tuple[int, int]:
        return (
            int(math.floor((point[0] - origin_x) / bin_size)),
            int(math.floor((point[1] - origin_y) / bin_size)),
        )

    for obstacle_id, (start, end) in enumerate(segment_obstacles):
        steps = max(
            1,
            int(math.ceil(math.dist(start, end) / (bin_size * 0.5))),
        )
        for step in range(steps + 1):
            amount = step / steps
            center_x, center_y = spatial_bin(
                (
                    start[0] + (end[0] - start[0]) * amount,
                    start[1] + (end[1] - start[1]) * amount,
                )
            )
            for bin_y in range(center_y - 1, center_y + 2):
                for bin_x in range(center_x - 1, center_x + 2):
                    segment_bins[(bin_x, bin_y)].add(obstacle_id)

    for obstacle_id, circle in enumerate(geometry.death_circles):
        low = spatial_bin(
            (circle.center[0] - circle.radius, circle.center[1] - circle.radius)
        )
        high = spatial_bin(
            (circle.center[0] + circle.radius, circle.center[1] + circle.radius)
        )
        for bin_y in range(low[1], high[1] + 1):
            for bin_x in range(low[0], high[0] + 1):
                circle_bins[(bin_x, bin_y)].add(obstacle_id)

    @functools.lru_cache(maxsize=None)
    def nearby_obstacles(
        low_x: int,
        low_y: int,
        high_x: int,
        high_y: int,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        segment_candidates: set[int] = set()
        circle_candidates: set[int] = set()
        for bin_y in range(low_y - 1, high_y + 2):
            for bin_x in range(low_x - 1, high_x + 2):
                segment_candidates.update(segment_bins.get((bin_x, bin_y), ()))
                circle_candidates.update(circle_bins.get((bin_x, bin_y), ()))
        return tuple(segment_candidates), tuple(circle_candidates)

    def segment_is_clear(start: Point, end: Point) -> bool:
        low = spatial_bin((min(start[0], end[0]), min(start[1], end[1])))
        high = spatial_bin((max(start[0], end[0]), max(start[1], end[1])))
        segment_candidates, circle_candidates = nearby_obstacles(
            low[0], low[1], high[0], high[1]
        )
        if any(
            _segments_intersect(start, end, *segment_obstacles[obstacle_id])
            for obstacle_id in segment_candidates
        ):
            return False
        if any(
            _point_segment_distance(
                geometry.death_circles[obstacle_id].center, start, end
            )
            <= geometry.death_circles[obstacle_id].radius + clearance
            for obstacle_id in circle_candidates
        ):
            return False
        if any(
            _point_in_polygon(start, polygon) or _point_in_polygon(end, polygon)
            for polygon in geometry.death_polygons
        ):
            return False
        return True

    def clear_grid_edge(start_x: int, start_y: int, end_x: int, end_y: int) -> bool:
        return segment_is_clear(
            grid_point(start_x, start_y), grid_point(end_x, end_y)
        )

    def nearest_safe_cell(point: Point) -> tuple[int, int] | None:
        center_x = int(round((point[0] - origin_x) / cell_size))
        center_y = int(round((point[1] - origin_y) / cell_size))
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
                return min(
                    candidates,
                    key=lambda cell: math.dist(grid_point(*cell), point),
                )
        return None

    # A teleport is a directed, zero-travel-distance edge from every safe
    # entrance cell to its engine-defined destination. Store the reverse edges
    # because the distance field grows backward from the winzone.
    teleport_predecessors: dict[int, list[tuple[int, float]]] = {}
    for teleport in geometry.teleports:
        entrance = teleport.entrance
        xs, ys = grid_bounds(
            entrance.center[0] - entrance.radius,
            entrance.center[1] - entrance.radius,
            entrance.center[0] + entrance.radius,
            entrance.center[1] + entrance.radius,
        )
        for y in ys:
            for x in xs:
                source = index(x, y)
                source_point = grid_point(x, y)
                if (
                    blocked[source]
                    or math.dist(source_point, entrance.center) > entrance.radius
                ):
                    continue
                for destination in teleport.exits_from(
                    source_point, geometry.axis_directions
                ):
                    exit_cell = nearest_safe_cell(destination)
                    if exit_cell is None:
                        continue
                    exit_index = index(*exit_cell)
                    teleport_predecessors.setdefault(exit_index, []).append(
                        (
                            source,
                            math.dist(destination, grid_point(*exit_cell)),
                        )
                    )

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
        for previous, transition_cost in teleport_predecessors.get(current, ()):
            candidate = distance + transition_cost
            if candidate + 1e-9 < distances[previous]:
                distances[previous] = candidate
                heapq.heappush(queue, (candidate, previous))

    model = RouteModel(
        geometry=geometry,
        origin_x=origin_x,
        origin_y=origin_y,
        cell_size=cell_size,
        width=width,
        height=height,
        blocked=blocked,
        distances=distances,
    )
    if narrow_passage_guides:
        # A spawn can be reachable through one broad route while a second,
        # legitimate sub-cell route is absent from the raster. Enrich every
        # requested model, not only wholly disconnected ones, so tight maps
        # with multiple route choices retain each real passage.
        _bridge_narrow_passages(
            model,
            moves=moves,
            grid_point=grid_point,
            segment_is_clear=segment_is_clear,
            clear_grid_edge=clear_grid_edge,
            include_zone_portals=True,
            include_circle_rings=False,
        )
        if model.reference_distance <= 0:
            # Full rings around every death circle are substantially more
            # expensive. Reserve them for the maps that remain disconnected
            # after targeted wall/zone passage guides have been merged.
            _bridge_narrow_passages(
                model,
                moves=moves,
                grid_point=grid_point,
                segment_is_clear=segment_is_clear,
                clear_grid_edge=clear_grid_edge,
                include_zone_portals=True,
                include_circle_rings=True,
            )
    return model


def _route_model_cache_key(
    path: Path,
    *,
    maximum_cells: int,
    minimum_cell_size: float,
    wall_clearance_cells: float,
    size_multiplier: float,
    narrow_passage_guides: bool,
) -> str:
    source_digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            source_digest.update(chunk)
    inputs = json.dumps(
        {
            "schema": ROUTE_MODEL_CACHE_SCHEMA,
            "sourceSha256": source_digest.hexdigest(),
            "maximumCells": int(maximum_cells),
            "minimumCellSize": float(minimum_cell_size),
            "wallClearanceCells": float(wall_clearance_cells),
            "sizeMultiplier": float(size_multiplier),
            "narrowPassageGuides": bool(narrow_passage_guides),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(inputs).hexdigest()


def _valid_cached_route_model(
    model: object,
    maximum_cells: int,
) -> bool:
    if model is None:
        return True
    if not isinstance(model, RouteModel):
        return False
    cells = model.width * model.height
    return (
        model.width > 0
        and model.height > 0
        and cells <= max(2_000, int(maximum_cells) * 2)
        and len(model.blocked) == cells
        and len(model.distances) == cells
        and len(model.guide_points) == len(model.guide_distances)
        and len(model.guide_points) <= 6_000
    )


def _prune_route_model_cache(
    cache_directory: Path,
    *,
    maximum_entries: int,
    maximum_bytes: int,
) -> None:
    entries = []
    for path in cache_directory.glob(f"*{ROUTE_MODEL_CACHE_SUFFIX}"):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime_ns, stat.st_size, path))
    entries.sort(reverse=True)
    retained_bytes = 0
    for index, (_, size, path) in enumerate(entries):
        retain = (
            index < max(1, int(maximum_entries))
            and retained_bytes + size <= max(1024 * 1024, int(maximum_bytes))
        )
        if retain:
            retained_bytes += size
            continue
        try:
            path.unlink()
        except OSError:
            continue


def load_or_build_route_model(
    path: Path,
    *,
    cache_directory: Path | None = None,
    cache_maximum_entries: int = 768,
    cache_maximum_bytes: int = 512 * 1024 * 1024,
    maximum_cells: int = 100_000,
    minimum_cell_size: float = 1.0,
    wall_clearance_cells: float = 0.0,
    size_multiplier: float = 1.0,
    narrow_passage_guides: bool = False,
) -> tuple[RouteModel | None, bool]:
    """Load an immutable route field from disk or build and cache it."""
    cache_key = _route_model_cache_key(
        path,
        maximum_cells=maximum_cells,
        minimum_cell_size=minimum_cell_size,
        wall_clearance_cells=wall_clearance_cells,
        size_multiplier=size_multiplier,
        narrow_passage_guides=narrow_passage_guides,
    )
    cache_path = (
        cache_directory / f"{cache_key}{ROUTE_MODEL_CACHE_SUFFIX}"
        if cache_directory is not None
        else None
    )
    if cache_path is not None and cache_path.is_file():
        try:
            if cache_path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("route-model cache entry is too large")
            with gzip.open(cache_path, "rb") as handle:
                payload = pickle.load(handle)
            if (
                not isinstance(payload, dict)
                or payload.get("schema") != ROUTE_MODEL_CACHE_SCHEMA
                or payload.get("key") != cache_key
                or not _valid_cached_route_model(
                    payload.get("model"), maximum_cells
                )
            ):
                raise ValueError("route-model cache entry is invalid")
            with contextlib.suppress(OSError):
                os.utime(cache_path, None)
            return payload.get("model"), True
        except (
            AttributeError,
            EOFError,
            OSError,
            pickle.UnpicklingError,
            TypeError,
            ValueError,
        ):
            try:
                cache_path.unlink()
            except OSError:
                pass

    model = build_route_model(
        path,
        maximum_cells=maximum_cells,
        minimum_cell_size=minimum_cell_size,
        wall_clearance_cells=wall_clearance_cells,
        size_multiplier=size_multiplier,
        narrow_passage_guides=narrow_passage_guides,
    )
    if cache_path is None:
        return model, False

    temporary = cache_path.with_name(
        f".{cache_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        cache_directory.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as raw_handle:
            with gzip.GzipFile(
                fileobj=raw_handle,
                mode="wb",
                compresslevel=3,
                mtime=0,
            ) as compressed:
                pickle.dump(
                    {
                        "schema": ROUTE_MODEL_CACHE_SCHEMA,
                        "key": cache_key,
                        "model": model,
                    },
                    compressed,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
        os.replace(temporary, cache_path)
        _prune_route_model_cache(
            cache_directory,
            maximum_entries=cache_maximum_entries,
            maximum_bytes=cache_maximum_bytes,
        )
    except (OSError, pickle.PickleError, TypeError):
        # The route field remains usable in memory when its optional cache
        # cannot be written.
        pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return model, False


def _bridge_narrow_passages(
    model: RouteModel,
    *,
    moves: tuple[tuple[int, int, float], ...],
    grid_point,
    segment_is_clear,
    clear_grid_edge,
    include_zone_portals: bool = False,
    include_circle_rings: bool = False,
) -> None:
    """Merge sparse sub-cell passage guides into a disconnected raster."""

    geometry = model.geometry
    cell_size = model.cell_size
    guide_epsilon = max(cell_size * 0.02, 1e-7)
    guide_spacing = max(cell_size * 0.45, guide_epsilon * 4)
    guide_radius = cell_size * 0.8
    guide_limit = 6_000
    raw_guides: list[Point] = [*geometry.spawns]
    raw_guides.extend(circle.center for circle in geometry.win_circles)
    raw_guides.extend(
        (
            sum(point[0] for point in polygon) / len(polygon),
            sum(point[1] for point in polygon) / len(polygon),
        )
        for polygon in geometry.win_polygons
    )

    obstacle_segments = list(geometry.wall_segments)
    for polygon in geometry.death_polygons:
        obstacle_segments.extend(zip(polygon, (*polygon[1:], polygon[0])))
    obstacle_complexity = len(obstacle_segments) + 2 * len(geometry.death_circles)
    if obstacle_complexity > 1_000:
        return
    # Only sub-cell and near-sub-cell gaps need supplemental samples. Wider
    # passages are represented by the ordinary raster already.
    portal_limit = cell_size * 1.25
    obstacle_vertices = {
        point for segment in obstacle_segments for point in segment
    }
    narrow_vertices = {
        point
        for point in obstacle_vertices
        if any(
            point != start
            and point != end
            and guide_epsilon * 2
            < _point_segment_distance(point, start, end)
            < portal_limit
            for start, end in obstacle_segments
        )
    }
    endpoint_samples = 16
    endpoint_guide_budget = min(2_000, guide_limit // 3)
    for point in sorted(narrow_vertices):
        if len(raw_guides) + endpoint_samples > endpoint_guide_budget:
            break
        for sample in range(endpoint_samples):
            angle = 2 * math.pi * sample / endpoint_samples
            raw_guides.append(
                (
                    point[0] + guide_epsilon * math.cos(angle),
                    point[1] + guide_epsilon * math.sin(angle),
                )
            )

    def add_portal_line(center: Point, direction: Point, half_length: float) -> None:
        if len(raw_guides) >= guide_limit:
            return
        length = math.hypot(*direction)
        if length <= 1e-12:
            return
        unit = direction[0] / length, direction[1] / length
        samples = max(1, int(math.ceil(half_length / guide_spacing)))
        for sample in range(-samples, samples + 1):
            if len(raw_guides) >= guide_limit:
                break
            amount = half_length * sample / samples
            raw_guides.append(
                (center[0] + unit[0] * amount, center[1] + unit[1] * amount)
            )

    circles = geometry.death_circles if include_zone_portals else ()
    for first_id, first in enumerate(circles):
        for second in circles[first_id + 1 :]:
            center_distance = math.dist(first.center, second.center)
            gap = center_distance - first.radius - second.radius
            if not (guide_epsilon * 2 < gap < portal_limit):
                continue
            direction = (
                (second.center[0] - first.center[0]) / center_distance,
                (second.center[1] - first.center[1]) / center_distance,
            )
            center = (
                first.center[0] + direction[0] * (first.radius + gap / 2),
                first.center[1] + direction[1] * (first.radius + gap / 2),
            )
            add_portal_line(center, (-direction[1], direction[0]), cell_size * 2.5)

    for circle in circles:
        for start, end in obstacle_segments:
            nearest = _closest_point_on_segment(circle.center, start, end)
            center_distance = math.dist(circle.center, nearest)
            gap = center_distance - circle.radius
            if not (guide_epsilon * 2 < gap < portal_limit):
                continue
            direction = end[0] - start[0], end[1] - start[1]
            if math.hypot(*direction) <= 1e-12:
                continue
            amount = (circle.radius + gap / 2) / center_distance
            center = (
                circle.center[0]
                + (nearest[0] - circle.center[0]) * amount,
                circle.center[1]
                + (nearest[1] - circle.center[1]) * amount,
            )
            add_portal_line(center, direction, cell_size * 2.5)

    for first_id, (first_start, first_end) in enumerate(obstacle_segments):
        first_direction = (
            first_end[0] - first_start[0],
            first_end[1] - first_start[1],
        )
        first_length = math.hypot(*first_direction)
        if first_length <= 1e-12:
            continue
        unit = first_direction[0] / first_length, first_direction[1] / first_length
        for second_start, second_end in obstacle_segments[first_id + 1 :]:
            second_direction = (
                second_end[0] - second_start[0],
                second_end[1] - second_start[1],
            )
            second_length = math.hypot(*second_direction)
            if second_length <= 1e-12:
                continue
            parallel = abs(
                unit[0] * second_direction[1] / second_length
                - unit[1] * second_direction[0] / second_length
            )
            if parallel > 0.02:
                continue
            second_low, second_high = sorted(
                (
                    (second_start[0] - first_start[0]) * unit[0]
                    + (second_start[1] - first_start[1]) * unit[1],
                    (second_end[0] - first_start[0]) * unit[0]
                    + (second_end[1] - first_start[1]) * unit[1],
                )
            )
            overlap_low = max(0.0, second_low)
            overlap_high = min(first_length, second_high)
            if overlap_high <= overlap_low:
                continue
            first_mid = (
                first_start[0] + unit[0] * (overlap_low + overlap_high) / 2,
                first_start[1] + unit[1] * (overlap_low + overlap_high) / 2,
            )
            second_mid = _closest_point_on_segment(
                first_mid, second_start, second_end
            )
            gap = math.dist(first_mid, second_mid)
            if not (guide_epsilon * 2 < gap < portal_limit):
                continue
            center = (
                (first_mid[0] + second_mid[0]) / 2,
                (first_mid[1] + second_mid[1]) / 2,
            )
            add_portal_line(
                center,
                unit,
                (overlap_high - overlap_low) / 2 + cell_size * 2,
            )

    # Targeted gap centerlines are more useful than complete obstacle rings,
    # so add the latter last and only within the bounded supplemental graph.
    if include_circle_rings:
        for circle in geometry.death_circles:
            if len(raw_guides) >= guide_limit:
                break
            guide_radius_value = circle.radius + guide_epsilon
            if circle.radius <= 1e-12:
                continue
            maximum_angle = 2 * math.acos(
                min(1.0, circle.radius / guide_radius_value)
            )
            samples = max(
                12,
                int(math.ceil(2 * math.pi / max(maximum_angle * 0.8, 1e-4))),
            )
            for sample in range(samples):
                if len(raw_guides) >= guide_limit:
                    break
                angle = 2 * math.pi * sample / samples
                raw_guides.append(
                    (
                        circle.center[0] + guide_radius_value * math.cos(angle),
                        circle.center[1] + guide_radius_value * math.sin(angle),
                    )
                )

    unique_guides: dict[tuple[int, int], Point] = {}
    quantization = max(guide_epsilon * 0.1, 1e-9)
    for point in raw_guides:
        if not segment_is_clear(point, point):
            continue
        key = (
            int(round(point[0] / quantization)),
            int(round(point[1] / quantization)),
        )
        unique_guides.setdefault(key, point)
    guide_points = tuple(unique_guides.values())
    if not guide_points:
        return

    guide_bins: dict[tuple[int, int], list[int]] = collections.defaultdict(list)
    for guide_id, point in enumerate(guide_points):
        guide_bins[
            (
                int(math.floor(point[0] / guide_radius)),
                int(math.floor(point[1] / guide_radius)),
            )
        ].append(guide_id)
    guide_edges: list[list[tuple[int, float]]] = [
        [] for _ in guide_points
    ]
    for guide_id, point in enumerate(guide_points):
        bin_x = int(math.floor(point[0] / guide_radius))
        bin_y = int(math.floor(point[1] / guide_radius))
        candidates: list[tuple[float, int]] = []
        for near_y in range(bin_y - 1, bin_y + 2):
            for near_x in range(bin_x - 1, bin_x + 2):
                for other_id in guide_bins.get((near_x, near_y), ()):
                    if other_id <= guide_id:
                        continue
                    cost = math.dist(point, guide_points[other_id])
                    if cost <= guide_radius:
                        candidates.append((cost, other_id))
        connected = 0
        for cost, other_id in sorted(candidates):
            if not segment_is_clear(point, guide_points[other_id]):
                continue
            guide_edges[guide_id].append((other_id, cost))
            guide_edges[other_id].append((guide_id, cost))
            connected += 1
            if connected >= 8:
                break

    guide_grid_edges: list[list[tuple[int, float]]] = [
        [] for _ in guide_points
    ]
    grid_guide_edges: dict[int, list[tuple[int, float]]] = collections.defaultdict(
        list
    )
    cross_radius = cell_size * 4.5
    for guide_id, point in enumerate(guide_points):
        center_x, center_y = model._nearest_cell(point)
        candidates: list[tuple[float, int, Point]] = []
        for y in range(max(0, center_y - 4), min(model.height, center_y + 5)):
            for x in range(max(0, center_x - 4), min(model.width, center_x + 5)):
                grid_id = model._index(x, y)
                if model.blocked[grid_id]:
                    continue
                grid_position = grid_point(x, y)
                cost = math.dist(point, grid_position)
                if cost <= cross_radius:
                    candidates.append((cost, grid_id, grid_position))
        # A guide only needs a small number of nearby anchors to join the
        # raster. Testing all 81 cells against every wall made complex maps
        # spend most of a countdown on redundant collision checks.
        connected = 0
        for cost, grid_id, grid_position in sorted(candidates):
            if not segment_is_clear(point, grid_position):
                continue
            guide_grid_edges[guide_id].append((grid_id, cost))
            grid_guide_edges[grid_id].append((guide_id, cost))
            connected += 1
            if connected >= 8:
                break

    grid_count = model.width * model.height
    guide_distances = [math.inf] * len(guide_points)
    merged_queue: list[tuple[float, int]] = []
    for guide_id, point in enumerate(guide_points):
        candidates = [
            model.distances[grid_id] + cost
            for grid_id, cost in guide_grid_edges[guide_id]
            if math.isfinite(model.distances[grid_id])
        ]
        if any(
            math.dist(point, circle.center) <= circle.radius
            for circle in geometry.win_circles
        ) or any(_point_in_polygon(point, polygon) for polygon in geometry.win_polygons):
            candidates.append(0.0)
        if candidates:
            guide_distances[guide_id] = min(candidates)
            heapq.heappush(
                merged_queue, (guide_distances[guide_id], grid_count + guide_id)
            )

    while merged_queue:
        distance, current = heapq.heappop(merged_queue)
        if current >= grid_count:
            guide_id = current - grid_count
            if distance != guide_distances[guide_id]:
                continue
            for other_id, cost in guide_edges[guide_id]:
                candidate = distance + cost
                if candidate + 1e-9 < guide_distances[other_id]:
                    guide_distances[other_id] = candidate
                    heapq.heappush(
                        merged_queue, (candidate, grid_count + other_id)
                    )
            for grid_id, cost in guide_grid_edges[guide_id]:
                candidate = distance + cost
                if candidate + 1e-9 < model.distances[grid_id]:
                    model.distances[grid_id] = candidate
                    heapq.heappush(merged_queue, (candidate, grid_id))
            continue

        if distance != model.distances[current]:
            continue
        current_x = current % model.width
        current_y = current // model.width
        for move_x, move_y, move_cost in moves:
            next_x = current_x - move_x
            next_y = current_y - move_y
            if not (0 <= next_x < model.width and 0 <= next_y < model.height):
                continue
            next_id = model._index(next_x, next_y)
            if model.blocked[next_id] or not clear_grid_edge(
                current_x, current_y, next_x, next_y
            ):
                continue
            candidate = distance + move_cost * cell_size
            if candidate + 1e-9 < model.distances[next_id]:
                model.distances[next_id] = candidate
                heapq.heappush(merged_queue, (candidate, next_id))
        for guide_id, cost in grid_guide_edges.get(current, ()):
            candidate = distance + cost
            if candidate + 1e-9 < guide_distances[guide_id]:
                guide_distances[guide_id] = candidate
                heapq.heappush(
                    merged_queue, (candidate, grid_count + guide_id)
                )

    model.guide_points = guide_points
    model.guide_distances = tuple(guide_distances)


@dataclasses.dataclass(frozen=True)
class AccelerationCapability:
    base_speed: float = 0.0
    decay_below: float = 0.0
    decay_above: float = 0.0
    external_acceleration: float = 0.0
    maximum_speed: float = 0.0

    @classmethod
    def from_settings(cls, settings: Mapping[str, str]) -> AccelerationCapability:
        def value(name: str, default: float) -> float:
            try:
                parsed = float(settings.get(name, str(default)))
            except (TypeError, ValueError):
                return default
            return parsed if math.isfinite(parsed) else default

        speed_multiplier = max(0.0, value("REAL_CYCLE_SPEED_FACTOR", 1.0))
        base_speed = max(0.0, value("CYCLE_SPEED", 10.0) * speed_multiplier)
        cycle_acceleration = max(0.0, value("CYCLE_ACCEL", 10.0))
        self_acceleration = max(0.0, value("CYCLE_ACCEL_SELF", 1.0))
        other_acceleration = max(
            0.0,
            value("CYCLE_ACCEL_TEAM", 1.0),
            value("CYCLE_ACCEL_ENEMY", 1.0),
            value("CYCLE_ACCEL_RIM", 0.0),
        )
        single_acceleration = max(self_acceleration, other_acceleration)
        slingshot_acceleration = (
            single_acceleration + self_acceleration
        ) * max(0.0, value("CYCLE_ACCEL_SLINGSHOT", 1.0))
        tunnel_acceleration = other_acceleration * max(
            0.0, value("CYCLE_ACCEL_TUNNEL", 1.0)
        )
        maximum_multiplier = max(
            single_acceleration,
            slingshot_acceleration,
            tunnel_acceleration,
        )
        acceleration_offset = max(
            1e-6, value("CYCLE_ACCEL_OFFSET", 2.0)
        )
        wall_near = max(0.0, value("CYCLE_WALL_NEAR", 6.0))
        wall_factor = (
            1.0 / acceleration_offset
            - 1.0 / (acceleration_offset + wall_near)
            if wall_near > 0
            else 0.0
        )
        external_acceleration = (
            speed_multiplier
            * cycle_acceleration
            * maximum_multiplier
            * wall_factor
        )
        brake = value("CYCLE_BRAKE", 30.0)
        if brake < 0:
            external_acceleration += -brake * speed_multiplier
        maximum_speed_factor = max(0.0, value("CYCLE_SPEED_MAX", 0.0))
        return cls(
            base_speed=base_speed,
            decay_below=max(0.0, value("CYCLE_SPEED_DECAY_BELOW", 5.0)),
            decay_above=max(0.0, value("CYCLE_SPEED_DECAY_ABOVE", 0.1)),
            external_acceleration=max(0.0, external_acceleration),
            maximum_speed=(
                base_speed * maximum_speed_factor
                if maximum_speed_factor > 0
                else 0.0
            ),
        )


def _acceleration_phase(
    initial_speed: float,
    seconds: float,
    *,
    base_speed: float,
    decay: float,
    external_acceleration: float,
    maximum_speed: float,
) -> tuple[float, float]:
    seconds = max(0.0, seconds)
    speed = max(0.0, initial_speed)
    if seconds <= 0:
        return 0.0, speed
    if maximum_speed > 0 and speed >= maximum_speed:
        return maximum_speed * seconds, maximum_speed
    if decay <= 1e-9:
        if external_acceleration <= 1e-9:
            return speed * seconds, speed
        cap_time = math.inf
        if maximum_speed > speed:
            cap_time = (maximum_speed - speed) / external_acceleration
        accelerating_time = min(seconds, cap_time)
        distance = (
            speed * accelerating_time
            + 0.5 * external_acceleration * accelerating_time**2
        )
        final_speed = speed + external_acceleration * accelerating_time
        if accelerating_time < seconds:
            distance += maximum_speed * (seconds - accelerating_time)
            final_speed = maximum_speed
        return distance, final_speed

    target_speed = base_speed + external_acceleration / decay
    cap_time = math.inf
    if (
        maximum_speed > speed
        and target_speed > maximum_speed
        and target_speed > speed
    ):
        cap_time = math.log(
            (target_speed - speed) / (target_speed - maximum_speed)
        ) / decay
    accelerating_time = min(seconds, cap_time)
    decay_amount = math.exp(-decay * accelerating_time)
    final_speed = target_speed + (speed - target_speed) * decay_amount
    distance = (
        target_speed * accelerating_time
        + (speed - target_speed) * (1.0 - decay_amount) / decay
    )
    if accelerating_time < seconds:
        distance += maximum_speed * (seconds - accelerating_time)
        final_speed = maximum_speed
    return max(0.0, distance), max(0.0, final_speed)


def accelerated_travel_distance(
    initial_speed: float,
    seconds: float,
    capability: AccelerationCapability | None,
) -> float:
    if capability is None:
        return max(0.0, initial_speed) * max(0.0, seconds)
    remaining = max(0.0, seconds)
    speed = max(0.0, initial_speed)
    distance = 0.0
    base_speed = max(0.0, capability.base_speed)
    maximum_speed = max(0.0, capability.maximum_speed)
    if speed < base_speed:
        crossing_time = math.inf
        if maximum_speed > 0 and maximum_speed <= base_speed:
            crossing_time = math.inf
        elif capability.external_acceleration > 1e-9:
            if capability.decay_below > 1e-9:
                target = (
                    base_speed
                    + capability.external_acceleration / capability.decay_below
                )
                crossing_time = math.log(
                    (target - speed) / (target - base_speed)
                ) / capability.decay_below
            else:
                crossing_time = (
                    base_speed - speed
                ) / capability.external_acceleration
        below_time = min(remaining, crossing_time)
        below_distance, speed = _acceleration_phase(
            speed,
            below_time,
            base_speed=base_speed,
            decay=capability.decay_below,
            external_acceleration=capability.external_acceleration,
            maximum_speed=maximum_speed,
        )
        distance += below_distance
        remaining -= below_time
        if remaining <= 1e-9:
            return distance
        speed = max(speed, base_speed)
    above_distance, _ = _acceleration_phase(
        speed,
        remaining,
        base_speed=base_speed,
        decay=capability.decay_above,
        external_acceleration=capability.external_acceleration,
        maximum_speed=maximum_speed,
    )
    return distance + above_distance


def accelerated_travel_seconds(
    distance: float,
    initial_speed: float,
    capability: AccelerationCapability | None,
) -> float:
    distance = max(0.0, distance)
    if distance <= 0:
        return 0.0
    high = 1.0
    while (
        high < 86_400.0
        and accelerated_travel_distance(initial_speed, high, capability) < distance
    ):
        high *= 2.0
    if accelerated_travel_distance(initial_speed, high, capability) < distance:
        return math.inf
    low = 0.0
    for _ in range(48):
        middle = (low + high) / 2.0
        if accelerated_travel_distance(initial_speed, middle, capability) >= distance:
            high = middle
        else:
            low = middle
    return high


@dataclasses.dataclass(frozen=True)
class ProgressAssessment:
    reason: str
    ground_speed: float
    route_speed: float
    required_speed: float
    projected_seconds: float
    can_finish: bool
    making_progress: bool


@dataclasses.dataclass(frozen=True)
class WrongWayProgressUpdate:
    heading_away: bool
    heading_toward: bool
    charged_seconds: float
    total_wrong_way_seconds: float
    remaining_allowance_seconds: float
    warning_due: bool
    exhausted: bool


@dataclasses.dataclass(frozen=True)
class StationaryProgressUpdate:
    charged_seconds: float
    stationary_seconds: float
    warning_due: bool
    exhausted: bool


@dataclasses.dataclass
class PlayerProgressState:
    samples: collections.deque[TimedProgressSample] = dataclasses.field(
        default_factory=collections.deque
    )
    warned_at: float | None = None
    violation_started_at: float | None = None
    last_reason: str = ""
    killed: bool = False
    travel_distance: float = 0.0
    pending_positions: collections.deque[tuple[float, float, float]] = (
        dataclasses.field(default_factory=collections.deque)
    )
    last_route_sample: tuple[float, float] | None = None
    wrong_way_seconds: float = 0.0
    wrong_way_episode_seconds: float = 0.0
    wrong_way_episode_warned: bool = False
    last_position_sample: tuple[float, float, float] | None = None
    stationary_seconds: float = 0.0
    stationary_warned: bool = False
    last_turn_direction: str = ""
    consecutive_same_turns: int = 0

    def clear_violation(self) -> None:
        self.warned_at = None
        self.violation_started_at = None
        self.last_reason = ""

    def clear_wrong_way_episode(self) -> None:
        self.wrong_way_episode_seconds = 0.0
        self.wrong_way_episode_warned = False

    def clear_route_baseline(self) -> None:
        self.last_route_sample = None
        self.clear_wrong_way_episode()

    def observe_position(
        self,
        now: float,
        position: Point,
        *,
        limit_seconds: float = 5.0,
        warning_delay_seconds: float = 1.0,
        position_epsilon: float = 0.01,
        maximum_sample_gap_seconds: float = 2.0,
    ) -> StationaryProgressUpdate:
        """Track one continuous period without meaningful cycle movement."""
        limit = max(0.1, float(limit_seconds))
        warning_delay = max(0.0, float(warning_delay_seconds))
        epsilon = max(0.0, float(position_epsilon))
        maximum_gap = max(0.1, float(maximum_sample_gap_seconds))
        previous = self.last_position_sample
        self.last_position_sample = (float(now), position[0], position[1])
        charged = 0.0
        warning_due = False
        if previous is not None:
            interval = max(0.0, float(now) - previous[0])
            movement = math.dist((previous[1], previous[2]), position)
            if movement <= epsilon:
                charged = min(interval, maximum_gap)
                self.stationary_seconds += charged
                if (
                    not self.stationary_warned
                    and self.stationary_seconds >= warning_delay
                ):
                    self.stationary_warned = True
                    warning_due = True
            else:
                self.stationary_seconds = 0.0
                self.stationary_warned = False
        return StationaryProgressUpdate(
            charged_seconds=charged,
            stationary_seconds=self.stationary_seconds,
            warning_due=warning_due,
            exhausted=self.stationary_seconds >= limit,
        )

    def observe_turn(self, direction: str, limit: int = 15) -> tuple[int, bool]:
        """Count consecutive successful turns in the same direction."""
        if direction not in {"L", "R"}:
            return self.consecutive_same_turns, False
        if direction == self.last_turn_direction:
            self.consecutive_same_turns += 1
        else:
            self.last_turn_direction = direction
            self.consecutive_same_turns = 1
        maximum = max(1, int(limit))
        return self.consecutive_same_turns, self.consecutive_same_turns > maximum

    def observe_route_distance(
        self,
        now: float,
        route_distance: float,
        *,
        allowance_seconds: float = 5.0,
        warning_delay_seconds: float = 1.0,
        direction_slack_distance: float = 0.5,
        maximum_sample_gap_seconds: float = 2.0,
    ) -> WrongWayProgressUpdate:
        """Charge cumulative time only while route distance is increasing."""
        allowance = max(0.1, float(allowance_seconds))
        warning_delay = max(0.0, float(warning_delay_seconds))
        direction_slack = max(0.0, float(direction_slack_distance))
        maximum_gap = max(0.1, float(maximum_sample_gap_seconds))
        previous = self.last_route_sample
        self.last_route_sample = (float(now), float(route_distance))
        heading_away = False
        heading_toward = False
        charged = 0.0
        warning_due = False
        if previous is not None:
            interval = max(0.0, float(now) - previous[0])
            distance_change = float(route_distance) - previous[1]
            heading_away = distance_change > direction_slack
            heading_toward = distance_change < -direction_slack
            if heading_away and interval > 0:
                charged = min(interval, maximum_gap)
                self.wrong_way_seconds += charged
                self.wrong_way_episode_seconds += charged
                if (
                    not self.wrong_way_episode_warned
                    and self.wrong_way_episode_seconds >= warning_delay
                ):
                    self.wrong_way_episode_warned = True
                    warning_due = True
            elif heading_toward:
                # Forward progress pauses the cumulative allowance; it never
                # refunds wrong-way time already used.
                self.clear_wrong_way_episode()
        remaining = max(0.0, allowance - self.wrong_way_seconds)
        return WrongWayProgressUpdate(
            heading_away=heading_away,
            heading_toward=heading_toward,
            charged_seconds=charged,
            total_wrong_way_seconds=self.wrong_way_seconds,
            remaining_allowance_seconds=remaining,
            warning_due=warning_due,
            exhausted=self.wrong_way_seconds >= allowance,
        )


def assess_progress(
    samples: Sequence[TimedProgressSample],
    *,
    remaining_seconds: float,
    route_slack_distance: float = 2.0,
    acceleration_capability: AccelerationCapability | None = None,
) -> ProgressAssessment | None:
    if len(samples) < 2:
        return None
    duration = samples[-1][0] - samples[0][0]
    if duration <= 0:
        return None
    recent_start = 0
    for index, sample in enumerate(samples[:-1]):
        if samples[-1][0] - sample[0] <= min(3.0, duration):
            recent_start = index
            break
    recent_duration = samples[-1][0] - samples[recent_start][0]
    ground_speed = (
        max(0.0, samples[-1][4] - samples[recent_start][4]) / recent_duration
        if recent_duration > 1e-9
        else 0.0
    )

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
    slack_distance = max(0.0, route_slack_distance)
    remaining_distance = max(0.0, samples[-1][3] - slack_distance)
    available_seconds = max(0.0, remaining_seconds)
    required_speed = (
        remaining_distance / available_seconds
        if available_seconds > 1e-9
        else math.inf
    )
    projected_seconds = accelerated_travel_seconds(
        remaining_distance,
        ground_speed,
        acceleration_capability,
    )

    # Reachability is intentionally optimistic: it uses the racer's recent
    # measured speed and maximum configured acceleration capability over the
    # wall-aware remaining route. Direction is evaluated independently below,
    # so a fast circle cannot pass merely by moving fast.
    can_finish = projected_seconds <= available_seconds
    making_progress = (
        net_progress > slack_distance
        and route_speed > slack_distance / duration
    )

    if can_finish and making_progress:
        return None
    if not can_finish and not making_progress:
        reason = (
            "your projected pace cannot reach the winzone before time expires "
            "and you are not making consistent progress toward it"
        )
    elif not can_finish:
        reason = "your projected pace cannot reach the winzone before time expires"
    else:
        reason = "you are not making consistent progress toward the winzone"
    return ProgressAssessment(
        reason=reason,
        ground_speed=ground_speed,
        route_speed=route_speed,
        required_speed=required_speed,
        projected_seconds=projected_seconds,
        can_finish=can_finish,
        making_progress=making_progress,
    )
