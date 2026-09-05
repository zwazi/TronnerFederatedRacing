#!/usr/bin/env python3
"""Recover replay start states from authoritative cycle replay ladder logs."""

from __future__ import annotations

import argparse
import collections
import dataclasses
import gzip
import re
import sqlite3
from io import TextIOWrapper
from pathlib import Path
from typing import BinaryIO, Iterable, TextIO


ACTION_CODES = {"L": 0, "R": 1, "B0": 2, "B1": 3}
VERSION_SUFFIX_RE = re.compile(r"-[^/-]+\.aamap\.xml$", re.IGNORECASE)
REQUIRED_COLUMNS = {
    "id",
    "spawn_game_time",
    "release_offset_us",
    "start_x",
    "start_y",
    "start_xdir",
    "start_ydir",
    "start_speed",
    "initial_turns",
    "input_data",
    "recorded_at",
    "ended_at",
    "outcome",
}


@dataclasses.dataclass
class LogCapture:
    token: str
    resource_key: str
    spawn_game_time: float
    start: tuple[float, float, float, float, float, int]
    release_offset_us: int | None = None
    events: list[tuple[int, int]] = dataclasses.field(default_factory=list)
    seen_events: set[tuple[int, int]] = dataclasses.field(default_factory=set)
    braking: bool = False
    duration_us: int = 0
    input_data: bytes = b""


@dataclasses.dataclass(frozen=True)
class Repair:
    run_id: int
    start: tuple[float, float, float, float, float, int]
    outcome: int


@dataclasses.dataclass(frozen=True)
class RepairPlan:
    parsed_captures: int
    database_runs: int
    matched_runs: int
    unchanged_runs: int
    ambiguous_runs: int
    unmatched_runs: int
    repairs: tuple[Repair, ...]

    @property
    def finished_repairs(self) -> int:
        return sum(repair.outcome == 1 for repair in self.repairs)


def _round_microseconds(seconds: float) -> int:
    return round(seconds * 1_000_000)


def _normalized_resource_key(resource_key: str) -> str:
    """Match a logical map across metadata-only published version migrations."""
    return VERSION_SUFFIX_RE.sub(".aamap.xml", resource_key).casefold()


def _encode_unsigned_varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def encode_replay_inputs(events: Iterable[tuple[int, int]]) -> bytes:
    encoded = bytearray()
    previous_offset = 0
    for offset_us, action in events:
        delta = int(offset_us) - previous_offset
        previous_offset = int(offset_us)
        zigzag = (delta << 1) if delta >= 0 else ((-delta << 1) - 1)
        encoded.extend(_encode_unsigned_varint((zigzag << 2) | action))
    return bytes(encoded)


def _open_ladderlog(path: Path) -> TextIO:
    raw: BinaryIO = path.open("rb")
    if raw.read(2) == b"\x1f\x8b":
        raw.seek(0)
        return gzip.open(raw, mode="rt", encoding="utf-8", errors="replace")
    raw.seek(0)
    return TextIOWrapper(raw, encoding="utf-8", errors="replace")


def parse_ladderlog(path: Path) -> list[LogCapture]:
    active: dict[str, LogCapture] = {}
    completed: list[LogCapture] = []
    current_resource_key = ""
    with _open_ladderlog(path) as lines:
        for line in lines:
            parts = line.split()
            if not parts:
                continue
            event = parts[0]
            if event == "CURRENT_MAP" and len(parts) >= 4:
                current_resource_key = parts[3]
                continue
            if event == "SHUTDOWN":
                active.clear()
                continue
            if event == "CYCLE_REPLAY_BEGIN" and len(parts) >= 10:
                try:
                    game_time = float(parts[3])
                    x, y, xdir, ydir, speed = map(float, parts[4:9])
                    turns = int(parts[9])
                except ValueError:
                    continue
                token = parts[1]
                active[token] = LogCapture(
                    token=token,
                    resource_key=current_resource_key,
                    spawn_game_time=game_time,
                    start=(x, y, xdir, ydir, max(0.0, speed), max(0, turns)),
                )
                continue
            if event == "CYCLE_REPLAY_STATE" and len(parts) >= 10:
                capture = active.get(parts[1])
                if capture is None or parts[2] != "release":
                    continue
                try:
                    game_time = float(parts[3])
                    x, y, xdir, ydir, speed = map(float, parts[4:9])
                    turns = int(parts[9])
                except ValueError:
                    continue
                capture.start = (
                    x,
                    y,
                    xdir,
                    ydir,
                    max(0.0, speed),
                    max(0, turns),
                )
                capture.release_offset_us = _round_microseconds(
                    game_time - capture.spawn_game_time
                )
                continue
            if event == "CYCLE_REPLAY_INPUT" and len(parts) >= 4:
                capture = active.get(parts[1])
                if capture is None:
                    continue
                try:
                    game_time = float(parts[2])
                except ValueError:
                    continue
                action_name = parts[3]
                action = ACTION_CODES.get(action_name)
                if action is None:
                    continue
                if action_name.startswith("B"):
                    braking = action_name == "B1"
                    if braking == capture.braking:
                        continue
                    capture.braking = braking
                replay_event = (
                    _round_microseconds(game_time - capture.spawn_game_time),
                    action,
                )
                if replay_event in capture.seen_events:
                    continue
                capture.seen_events.add(replay_event)
                capture.events.append(replay_event)
                continue
            if event == "CYCLE_REPLAY_END" and len(parts) >= 4:
                capture = active.pop(parts[1], None)
                if capture is None:
                    continue
                try:
                    end_game_time = float(parts[3])
                except ValueError:
                    end_game_time = capture.spawn_game_time
                capture.duration_us = _round_microseconds(
                    max(0.0, end_game_time - capture.spawn_game_time)
                )
                capture.input_data = encode_replay_inputs(capture.events)
                completed.append(capture)
    return completed


def _capture_key(capture: LogCapture) -> tuple[str, int, int | None, bytes]:
    return (
        _normalized_resource_key(capture.resource_key),
        _round_microseconds(capture.spawn_game_time),
        capture.release_offset_us,
        capture.input_data,
    )


def build_repair_plan(
    connection: sqlite3.Connection,
    captures: list[LogCapture],
) -> RepairPlan:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(replay_runs)")
    }
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise RuntimeError(
            "replay_runs is missing columns: " + ", ".join(sorted(missing))
        )

    candidates: dict[
        tuple[str, int, int | None, bytes], list[LogCapture]
    ] = collections.defaultdict(list)
    for capture in captures:
        candidates[_capture_key(capture)].append(capture)

    repairs: list[Repair] = []
    matched_runs = 0
    unchanged_runs = 0
    ambiguous_runs = 0
    unmatched_runs = 0
    rows = connection.execute(
        "SELECT replay_runs.id, replay_maps.resource_key, "
        "replay_runs.spawn_game_time, replay_runs.release_offset_us, "
        "replay_runs.input_data, replay_runs.recorded_at, replay_runs.ended_at, "
        "replay_runs.start_x, replay_runs.start_y, replay_runs.start_xdir, "
        "replay_runs.start_ydir, replay_runs.start_speed, "
        "replay_runs.initial_turns, replay_runs.outcome "
        "FROM replay_runs JOIN replay_maps ON replay_maps.id=replay_runs.map_ref"
    ).fetchall()
    for row in rows:
        (
            run_id,
            resource_key,
            spawn_game_time,
            release_offset_us,
            input_data,
            recorded_at,
            ended_at,
            *stored_values,
        ) = row
        outcome = int(stored_values.pop())
        key = (
            _normalized_resource_key(str(resource_key)),
            _round_microseconds(float(spawn_game_time)),
            int(release_offset_us) if release_offset_us is not None else None,
            bytes(input_data),
        )
        duration_us = _round_microseconds(float(ended_at) - float(recorded_at))
        duration_matches = [
            capture
            for capture in candidates.get(key, ())
            if abs(capture.duration_us - duration_us) <= 2
        ]
        if not duration_matches:
            unmatched_runs += 1
            continue
        possible_starts = {capture.start for capture in duration_matches}
        if len(possible_starts) != 1:
            ambiguous_runs += 1
            continue
        matched_runs += 1
        recovered_start = next(iter(possible_starts))
        if tuple(stored_values) == recovered_start:
            unchanged_runs += 1
            continue
        repairs.append(Repair(int(run_id), recovered_start, outcome))

    return RepairPlan(
        parsed_captures=len(captures),
        database_runs=len(rows),
        matched_runs=matched_runs,
        unchanged_runs=unchanged_runs,
        ambiguous_runs=ambiguous_runs,
        unmatched_runs=unmatched_runs,
        repairs=tuple(repairs),
    )


def apply_repairs(connection: sqlite3.Connection, plan: RepairPlan) -> None:
    with connection:
        connection.executemany(
            "UPDATE replay_runs SET start_x=?, start_y=?, start_xdir=?, "
            "start_ydir=?, start_speed=?, initial_turns=? WHERE id=?",
            ((*repair.start, repair.run_id) for repair in plan.repairs),
        )


def backup_database(connection: sqlite3.Connection, path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"backup already exists: {path}")
    backup = sqlite3.connect(path)
    try:
        connection.backup(backup)
        result = backup.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"backup integrity check failed: {result!r}")
    finally:
        backup.close()
    path.chmod(0o600)


def print_plan(plan: RepairPlan, applying: bool) -> None:
    print(f"parsed_captures={plan.parsed_captures}")
    print(f"database_runs={plan.database_runs}")
    print(f"matched_runs={plan.matched_runs}")
    print(f"unchanged_runs={plan.unchanged_runs}")
    print(f"ambiguous_runs={plan.ambiguous_runs}")
    print(f"unmatched_runs={plan.unmatched_runs}")
    print(f"{'updated' if applying else 'would_update'}={len(plan.repairs)}")
    print(
        f"{'updated_finished' if applying else 'would_update_finished'}="
        f"{plan.finished_repairs}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--ladderlog", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.apply and args.backup is None:
        parser.error("--apply requires --backup")

    captures = parse_ladderlog(args.ladderlog)
    connection = sqlite3.connect(args.database, timeout=30)
    try:
        plan = build_repair_plan(connection, captures)
        if args.apply:
            backup_database(connection, args.backup)
            apply_repairs(connection, plan)
        print_plan(plan, args.apply)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
