#!/usr/bin/env python3
"""Tronner Racing controller for an Armagetron sty+ct+ap dedicated server.

The controller follows ladderlog.txt, writes commands to the server's input
stream, mirrors maps from the configured Git repository over plain HTTP for
legacy clients, and stores race records in SQLite.
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import collections
import contextlib
import dataclasses
import datetime
import functools
import hashlib
import http.server
import json
import logging
import math
import os
import random
import re
import resource
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from firebase_catalog import FirebaseCatalogClient, FirebaseCatalogError
from live_dashboard import FirebaseLiveDashboardPublisher, public_player_id


LOG = logging.getLogger("TronnerRacing")
MAP_SUFFIX = ".aamap.xml"
RESEND_ENDPOINT = "https://api.resend.com/emails"
# Account linking is an optional operator integration. Never default a public
# source build to somebody else's billable endpoint.
DEFAULT_GAME_LINK_ENDPOINT = ""
DEFAULT_GAME_TEXT_ENCODING = "latin-1"
REPLAY_FORMAT_VERSION = 1
REPLAY_ACTION_CODES = {"L": 0, "R": 1, "B0": 2, "B1": 3}
REPLAY_ACTION_NAMES = tuple(REPLAY_ACTION_CODES)
REPLAY_SETTINGS_FORMAT_VERSION = 1
MAX_FEDERATION_CONTROLLER_EVENT_BYTES = 16_384
MAX_FEDERATION_PLAYER_NAME_CHARACTERS = 128
MAX_FEDERATION_CHAT_CHARACTERS = 512
MAX_FEDERATION_RECORDS_PER_BATCH = 20
MAX_FEDERATION_PREFERENCES_PER_BATCH = 100
MAX_FEDERATION_CATALOG_EXCLUSIONS = 256
MAX_FEDERATION_RECORD_KEY_CHARACTERS = 1024
MAX_FEDERATION_RECORD_IDENTITY_CHARACTERS = 256
MAX_FEDERATION_MAP_BYTES = 4 * 1024 * 1024
SERVER_CONSOLE_HISTORY_LINES = 250
SERVER_CONSOLE_INITIAL_LINES = 100
SERVER_CONSOLE_BATCH_SIZE = 25
SERVER_CONSOLE_STREAM_SECONDS = 90.0
SERVER_CONSOLE_MAX_FILE_BYTES = 8 * 1024 * 1024
SERVER_CONSOLE_SENSITIVE_RE = re.compile(
    r"(?i)\b(?:admin[_-]?pass|password|passphrase|secret|api[_-]?key|"
    r"authorization|bearer|private[_-]?key)\b"
)
FEDERATION_LOCAL_COMMANDS = frozenset(
    {
        "/cp",
        "/display_server_tags",
        "/help",
        "/join",
        "/link",
        "/reload_controller",
        "/report",
        "/respawn",
        "/restart",
        "/setspawn",
        "/spec",
        "/spectate",
        "/start",
        "/sui",
    }
)
SERVER_MANAGEMENT_COMMANDS = frozenset(
    {
        "announce",
        "direct_message",
        "kick",
        "ban",
        "silence",
        "voice",
        "kill",
        "force_skip",
        "end_map",
        "queue_map",
        "remove_queued_map",
        "clear_queue",
        "change_map",
        "reload_maps",
        "restart_round",
        "set_engine_option",
        "reload_controller",
        "start_console_stream",
    }
)
SERVER_MANAGEMENT_ENGINE_OPTIONS = {
    "IDLE_KICK_TIME": (0.0, 86_400.0),
    "SPAM_AUTOKICK": (0.0, 10_000.0),
    "SPAM_AUTOKICK_COUNT": (0.0, 1_000.0),
    "MAX_CLIENTS": (1.0, 64.0),
    "MAX_PLAYERS": (1.0, 64.0),
    "VOTING_ALLOWED": (0.0, 1.0),
    "VOTING_KICK_TIME": (0.0, 86_400.0),
    "CYCLE_RUBBER": (0.0, 100.0),
    "CYCLE_SPEED": (0.01, 1_000.0),
    "CYCLE_ACCEL": (0.0, 1_000.0),
    "CYCLE_BRAKE": (0.0, 1.0),
}


def _ascii_compatible_encoding(value: object) -> str | None:
    """Resolve a Python codec that preserves Armagetron's ASCII protocol."""
    candidate = str(value).strip()
    if not candidate:
        return None
    try:
        encoding = codecs.lookup(candidate).name
        probe = "ENCODING".encode(encoding)
        if probe != b"ENCODING" or probe.decode(encoding) != "ENCODING":
            return None
        return encoding
    except (LookupError, UnicodeError):
        return None


def canonical_game_text_encoding(
    value: object,
    fallback: object = DEFAULT_GAME_TEXT_ENCODING,
) -> str:
    """Normalize advertised codec names and reject non-text protocol codecs."""
    fallback_encoding = (
        _ascii_compatible_encoding(fallback)
        or _ascii_compatible_encoding(DEFAULT_GAME_TEXT_ENCODING)
        or "iso8859-1"
    )
    encoding = _ascii_compatible_encoding(value)
    if encoding is None:
        LOG.warning(
            "unsupported Armagetron text encoding %r; using %s",
            value,
            fallback_encoding,
        )
        return fallback_encoding
    return encoding


def detect_game_text_encoding(
    ladderlog: Path,
    fallback: object = DEFAULT_GAME_TEXT_ENCODING,
) -> str:
    """Return the most recently advertised ladderlog ENCODING codec."""
    fallback_encoding = canonical_game_text_encoding(fallback)
    advertised: bytes | None = None
    try:
        with ladderlog.open("rb") as handle:
            for raw_line in handle:
                if raw_line.startswith(b"ENCODING "):
                    fields = raw_line[len(b"ENCODING "):].strip().split()
                    if fields:
                        advertised = fields[0]
    except OSError:
        return fallback_encoding
    if advertised is None:
        return fallback_encoding
    try:
        name = advertised.decode("ascii")
    except UnicodeDecodeError:
        LOG.warning(
            "non-ASCII Armagetron ENCODING declaration; using %s",
            fallback_encoding,
        )
        return fallback_encoding
    return canonical_game_text_encoding(name, fallback_encoding)


def decode_game_text(data: bytes, encoding: str, context: str) -> str:
    """Decode protocol bytes, reporting corruption instead of hiding it."""
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as error:
        LOG.warning(
            "invalid %s data in %s at byte %d; replacing undecodable bytes",
            encoding,
            context,
            error.start,
        )
        return data.decode(encoding, "replace")


def encode_game_text(text: str, encoding: str, context: str) -> bytes:
    """Encode protocol text and safely degrade unsupported Unicode characters."""
    try:
        return text.encode(encoding)
    except UnicodeEncodeError as error:
        LOG.warning(
            "%s contains characters unsupported by %s at character %d; replacing them",
            context,
            encoding,
            error.start,
        )
        return text.encode(encoding, "replace")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def clean_console_text(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def quote_console(value: object) -> str:
    text = clean_console_text(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def quote_console_exact(value: object) -> str:
    """Quote trusted text without trimming intentional edge whitespace."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def quote_console_block(value: object) -> str:
    """Quote a multiline argument using the console parser's ``\\n`` escape."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", "\\n")
    return f'"{text}"'


def readline_console_text(value: object) -> str:
    """Escape text consumed by tString::ReadLine without adding visible quotes."""
    return clean_console_text(value).replace("\\", "\\\\")


def readline_console_block(value: object) -> str:
    """Encode line breaks for one tString::ReadLine console command."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\\", "\\\\").replace("\n", "\\n")


COLOR_CODE_RE = re.compile(r"0x[0-9a-f]{6}")
INPUT_COLOR_CODE_RE = re.compile(r"0[xX][0-9a-fA-F]{6}|0[xX]RESETT")
RESOURCE_TAG_BYTES_RE = re.compile(
    br"<Resource\b[^>]*>", re.IGNORECASE | re.DOTALL
)
XML_ATTRIBUTE_BYTES_RE = re.compile(
    br"([A-Za-z_:][A-Za-z0-9_:.-]*)\s*=\s*([\"'])(.*?)\2",
    re.DOTALL,
)

# Bright, low-contrast racing palette. Armagetron renders dark color controls
# with a distracting white backing box, so every controller-owned color keeps
# at least two channels comfortably above the dark range.
COLOR_RESET = "0xffffff"
COLOR_BORDER = "0x70e6ff"
COLOR_TITLE = "0xffd166"
COLOR_RANK_HEADER = "0xff9de1"
COLOR_TIME_HEADER = "0x91ffb6"
COLOR_TURNS_HEADER = "0xc4b5ff"
COLOR_NAME_HEADER = "0xfff2a8"
COLOR_DATA = "0xe8f7ff"
COLOR_VALUE = "0xfff2a8"
COLOR_COMMAND = "0xc4b5ff"
COLOR_SUCCESS = "0x7dff9b"
COLOR_ERROR = "0xff8c8c"
COLOR_WARNING = "0xffc46b"
COLOR_MUTED = "0xb8c9ff"
COLOR_CURRENT_MAP = "0xff5cff"
COLOR_FEDERATION_TAG = "0x66ccff"
COLOR_PLAYER_ENTERED = "0x7fff7f"
COLOR_PLAYER_LEFT = "0xff7f7f"
CHECKPOINT_CENTER_GAP = " " * 34

INLINE_COMMAND_RE = re.compile(r"(?<!\w)(/[a-z][a-z0-9_-]*)(?!\w)", re.I)
TIP_QUOTED_RE = re.compile(r'"([^"\r\n]*)"')


def normalize_console_colors(value: object) -> str:
    """Return text whose color controls use only canonical lowercase hex."""
    text = clean_console_text(value)

    def canonical(match: re.Match[str]) -> str:
        token = match.group(0)
        if token[2:].casefold() == "resett":
            return "0xffffff"
        return "0x" + token[2:].lower()

    return INPUT_COLOR_CODE_RE.sub(canonical, text)


def brighten_console_colors(value: object, minimum_channel_total: int = 500) -> str:
    """Lift user-selected dark colors while preserving their hue relationship."""
    text = normalize_console_colors(value)
    minimum_channel_total = max(0, min(765, int(minimum_channel_total)))

    def brighten(match: re.Match[str]) -> str:
        token = match.group(0)
        channels = [int(token[index:index + 2], 16) for index in (2, 4, 6)]
        total = sum(channels)
        if total >= minimum_channel_total:
            return token
        blend = (minimum_channel_total - total) / (765 - total)
        lifted = [
            min(255, round(channel + (255 - channel) * blend))
            for channel in channels
        ]
        deficit = minimum_channel_total - sum(lifted)
        for index in sorted(range(3), key=lambda item: lifted[item]):
            increase = min(deficit, 255 - lifted[index])
            lifted[index] += increase
            deficit -= increase
            if deficit <= 0:
                break
        return "0x" + "".join(f"{channel:02x}" for channel in lifted)

    return COLOR_CODE_RE.sub(brighten, text)


def plain_console_text(value: object) -> str:
    return COLOR_CODE_RE.sub("", normalize_console_colors(value))


def _message_base_color(text: str) -> str:
    """Choose a readable semantic color for an ordinary racing message."""
    lowered = text.casefold()
    if any(
        marker in lowered
        for marker in (
            "cannot",
            "disabled",
            "failed",
            "invalid",
            "no active",
            "no current",
            "no map",
            "no record",
            "not available",
            "only an owner",
            "only active",
            "rate limit",
            "unable",
            "usage:",
        )
    ):
        return COLOR_ERROR
    if any(
        marker in lowered
        for marker in (
            "already",
            "countdown",
            "please wait",
            "press brake",
            "required",
        )
    ):
        return COLOR_WARNING
    if any(
        marker in lowered
        for marker in (
            "added:",
            "enabled",
            "extended",
            "finished",
            "reloaded",
            "saved",
        )
    ):
        return COLOR_SUCCESS
    return COLOR_DATA


def _highlight_commands(text: str, base_color: str) -> str:
    return INLINE_COMMAND_RE.sub(
        lambda match: f"{COLOR_COMMAND}{match.group(1)}{base_color}",
        text,
    )


def style_console_message(value: object) -> str:
    """Apply the shared palette to every controller-owned visible message."""
    text = normalize_console_colors(value)
    if not text:
        return ""

    # Structured formatters already select their semantic colors. A base and
    # final reset still prevent user/map colors from leaking into later text.
    if COLOR_CODE_RE.search(text):
        return f"{COLOR_DATA}{text}{COLOR_RESET}"

    command_help = re.fullmatch(r"(/.+?)(\s+-\s+)(.*)", text)
    if command_help:
        command, separator, description = command_help.groups()
        return (
            f"{COLOR_COMMAND}{command}{COLOR_BORDER}{separator}"
            f"{COLOR_DATA}{description}{COLOR_RESET}"
        )

    label = re.match(r"^([^:]{1,48}:)(.*)$", text)
    if label:
        heading, remainder = label.groups()
        value_color = _message_base_color(text)
        return (
            f"{COLOR_BORDER}{heading}{value_color}"
            f"{_highlight_commands(remainder, value_color)}{COLOR_RESET}"
        )

    base_color = _message_base_color(text)
    return (
        f"{base_color}{_highlight_commands(text, base_color)}{COLOR_RESET}"
    )


def style_tip_message(value: object) -> str:
    """Render a tip in white with double-quoted contents highlighted."""
    text = plain_console_text(value)
    highlighted = TIP_QUOTED_RE.sub(
        lambda match: (
            f'"{COLOR_COMMAND}{match.group(1)}{COLOR_RESET}"'
        ),
        text,
    )
    return f"{COLOR_RESET}{highlighted}{COLOR_RESET}"


def style_console_block(lines: Iterable[object]) -> str:
    """Style each line independently, then retain their logical ordering."""
    return "\n".join(style_console_message(line) for line in lines)


def split_admin_reason(value: str) -> tuple[str, str, bool]:
    """Split `[map selector] -- [reason]` without breaking names containing dashes."""
    text = value.strip()
    separator = re.search(r"(?:^|\s)--(?:\s|$)", text)
    if not separator:
        return text, "", False
    return (
        text[: separator.start()].strip(),
        text[separator.end() :].strip(),
        True,
    )


def race_time_decimals(entry: object) -> int:
    """Return map-specific display precision without changing stored times."""
    try:
        decimals = int(getattr(entry, "time_decimals", 3))
    except (TypeError, ValueError):
        return 3
    return max(0, min(8, decimals))


def build_leaderboard_table(
    map_name: str,
    author: str,
    records: Sequence[Record],
    personal_rows: Sequence[
        tuple[str, int | str, str, float | None, int | None]
    ] = (),
    top_limit: int = 3,
    axes: int | None = None,
    rating: float | None = None,
    time_decimals: int = 3,
) -> tuple[list[str], dict[str, list[str]]]:
    """Build one common table and per-player rows that attach below it."""
    time_decimals = max(0, int(time_decimals))
    top_records = list(records[: max(0, int(top_limit))])
    top_rows: list[tuple[str, str, str, str]] = [
        (
            f"{rank}.",
            f"{record.best_seconds:.{time_decimals}f}",
            "--" if record.best_turns is None else str(record.best_turns),
            plain_console_text(record.username),
        )
        for rank, record in enumerate(top_records, 1)
    ]
    if not top_rows:
        top_rows.append(("--", "--", "--", "--"))

    top_keys = {record.identity_key for record in top_records}
    private_data: list[tuple[str, str, str, str, str]] = []
    for identity_key, rank, username, seconds, turns in personal_rows:
        if identity_key in top_keys:
            continue
        rank_text = "--" if rank == "--" else f"{rank}."
        time_text = (
            "--" if seconds is None else f"{seconds:.{time_decimals}f}"
        )
        turns_text = "--" if turns is None else str(turns)
        private_data.append(
            (
                identity_key,
                rank_text,
                time_text,
                turns_text,
                plain_console_text(username),
            )
        )

    all_rows = top_rows + [row[1:] for row in private_data]
    rank_width = max(4, *(len(row[0]) for row in all_rows))
    time_width = max(5, *(len(row[1]) for row in all_rows))
    turns_width = max(5, *(len(row[2]) for row in all_rows))
    name_width = max(12, *(min(32, len(row[3])) for row in all_rows))
    map_text = plain_console_text(f"Map: {map_name} | Author: {author}")

    # Expand the name column when the map heading needs more room.
    minimum_name_width = min(
        32,
        len(map_text) - rank_width - time_width - turns_width - 11,
    )
    name_width = max(name_width, minimum_name_width)

    def fitted(value: str, width: int) -> str:
        if len(value) <= width:
            return value
        return value[: max(0, width - 1)] + "~"

    def row(
        rank: str,
        seconds: str,
        turns: str,
        username: str,
        colors: tuple[str, str, str, str],
    ) -> str:
        rank_color, time_color, turns_color, name_color = colors
        return (
            f"{COLOR_BORDER}| {rank_color}"
            f"{fitted(rank, rank_width).center(rank_width)} {COLOR_BORDER}| "
            f"{time_color}{fitted(seconds, time_width).center(time_width)} "
            f"{COLOR_BORDER}| {turns_color}"
            f"{fitted(turns, turns_width).center(turns_width)} {COLOR_BORDER}| "
            f"{name_color}{fitted(username, name_width).center(name_width)} "
            f"{COLOR_BORDER}|{COLOR_RESET}"
        )

    column_border = (
        f"+-{'-' * rank_width}-+-{'-' * time_width}-+-{'-' * turns_width}-+"
        f"-{'-' * name_width}-+"
    )
    outer_border = "+" + "-" * (len(column_border) - 2) + "+"
    map_row = (
        f"{COLOR_BORDER}|{COLOR_TITLE}"
        f"{fitted(map_text, len(outer_border) - 2).center(len(outer_border) - 2)}"
        f"{COLOR_BORDER}|{COLOR_RESET}"
    )
    colored_outer_border = f"{COLOR_BORDER}{outer_border}{COLOR_RESET}"
    colored_column_border = f"{COLOR_BORDER}{column_border}{COLOR_RESET}"
    axes_value = "--" if axes is None else str(axes)
    rating_value = "--/5" if rating is None else f"{rating:.2f}/5"
    status_left_width = rank_width + time_width + 3
    status_right_width = turns_width + name_width + 3
    status_border = (
        f"+-{'-' * status_left_width}-+-{'-' * status_right_width}-+"
    )
    colored_status_border = f"{COLOR_BORDER}{status_border}{COLOR_RESET}"

    def centered_status_cell(
        label: str,
        value: str,
        width: int,
        label_color: str,
    ) -> str:
        visible = f"{label}: {value}"
        remaining = max(0, width - len(visible))
        left = remaining // 2
        right = remaining - left
        return (
            f"{' ' * left}{label_color}{label}: {COLOR_VALUE}{value}"
            f"{' ' * right}"
        )

    status_row = (
        f"{COLOR_BORDER}| "
        f"{centered_status_cell('Axes', axes_value, status_left_width, COLOR_RANK_HEADER)}"
        f" {COLOR_BORDER}| "
        f"{centered_status_cell('Rating', rating_value, status_right_width, COLOR_TIME_HEADER)} "
        f"{COLOR_BORDER}|{COLOR_RESET}"
    )
    header_colors = (
        COLOR_RANK_HEADER,
        COLOR_TIME_HEADER,
        COLOR_TURNS_HEADER,
        COLOR_NAME_HEADER,
    )
    data_colors = (COLOR_DATA, COLOR_DATA, COLOR_DATA, COLOR_DATA)
    common = [
        colored_outer_border,
        map_row,
        colored_column_border,
        row("Rank", "Time", "Turns", "Name", header_colors),
        colored_column_border,
        *(row(*values, data_colors) for values in top_rows),
        colored_column_border,
        status_row,
        colored_status_border,
    ]
    private = {
        identity_key: [
            row(rank, seconds, turns, username, data_colors),
            colored_column_border,
        ]
        for identity_key, rank, seconds, turns, username in private_data
    }
    return common, private


def format_finish_message(
    colored_username: str,
    seconds: float,
    finish_rank: int,
    best_seconds: float,
    best_rank: int,
    previous_best: float | None,
    turns: int | None,
    best_turns: int | None,
    previous_best_turns: int | None,
    no_cp_seconds: float | None = None,
    no_cp_rank: int | None = None,
    no_cp_turns: int | None = None,
    improved: bool = False,
    previous_best_rank: int | None = None,
    time_decimals: int = 3,
) -> str:
    time_decimals = max(0, int(time_decimals))
    colored_username = brighten_console_colors(colored_username)
    turns_text = "--" if turns is None else str(turns)
    finish_text = (
        f"{colored_username}{COLOR_RESET} {COLOR_BORDER}- "
        f"{COLOR_TIME_HEADER}Finish: {COLOR_VALUE}"
        f"{seconds:.{time_decimals}f}"
        f"{COLOR_MUTED}, {COLOR_TURNS_HEADER}Turns: {COLOR_VALUE}{turns_text}"
        f"{COLOR_MUTED}, {COLOR_RANK_HEADER}Rank: {COLOR_VALUE}{finish_rank}"
        f"{COLOR_RESET}"
    )
    if previous_best is None and no_cp_seconds is None:
        return finish_text

    reference = previous_best if previous_best is not None else best_seconds
    split = round(seconds - reference, time_decimals)
    if split < 0:
        color = COLOR_SUCCESS
        split_text = f"{split:.{time_decimals}f}"
    elif split > 0:
        color = COLOR_ERROR
        split_text = f"+{split:.{time_decimals}f}"
    else:
        color = COLOR_MUTED
        split_text = f"{0:.{time_decimals}f}"

    turn_reference = (
        best_turns if previous_best is None else previous_best_turns
    )
    if turns is None or turn_reference is None:
        turn_color = COLOR_MUTED
        turn_split_text = "--"
    else:
        turn_split = turns - turn_reference
        if turn_split < 0:
            turn_color = COLOR_SUCCESS
            turn_split_text = str(turn_split)
        elif turn_split > 0:
            turn_color = COLOR_ERROR
            turn_split_text = f"+{turn_split}"
        else:
            turn_color = COLOR_MUTED
            turn_split_text = "0"

    show_previous_best = improved and previous_best is not None
    reference_label = "Previous best" if show_previous_best else "Best"
    displayed_best = previous_best if show_previous_best else best_seconds
    displayed_turns = previous_best_turns if show_previous_best else best_turns
    displayed_rank = (
        previous_best_rank
        if show_previous_best and previous_best_rank is not None
        else best_rank
    )
    best_turns_text = "--" if displayed_turns is None else str(displayed_turns)
    message = (
        f"{finish_text} {COLOR_BORDER}| {COLOR_TIME_HEADER}{reference_label}: "
        f"{COLOR_VALUE}{displayed_best:.{time_decimals}f}{COLOR_MUTED}, "
        f"{COLOR_TURNS_HEADER}Turns: {COLOR_VALUE}{best_turns_text}"
        f"{COLOR_MUTED}, {COLOR_RANK_HEADER}Rank: {COLOR_VALUE}{displayed_rank} "
        f"{COLOR_BORDER}| {COLOR_NAME_HEADER}Split: {color}{split_text}"
        f"{COLOR_MUTED}, {turn_color}{turn_split_text}{COLOR_RESET}"
    )
    if no_cp_seconds is None:
        return message

    no_cp_turns_text = "--" if no_cp_turns is None else str(no_cp_turns)
    no_cp_rank_text = "--" if no_cp_rank is None else str(no_cp_rank)
    no_cp_split = round(no_cp_seconds - best_seconds, time_decimals)
    if no_cp_split < 0:
        no_cp_split_color = COLOR_SUCCESS
        no_cp_split_text = f"{no_cp_split:.{time_decimals}f}"
    elif no_cp_split > 0:
        no_cp_split_color = COLOR_ERROR
        no_cp_split_text = f"+{no_cp_split:.{time_decimals}f}"
    else:
        no_cp_split_color = COLOR_MUTED
        no_cp_split_text = f"{0:.{time_decimals}f}"
    if no_cp_turns is None or best_turns is None:
        no_cp_turn_color = COLOR_MUTED
        no_cp_turn_split_text = "--"
    else:
        no_cp_turn_split = no_cp_turns - best_turns
        if no_cp_turn_split < 0:
            no_cp_turn_color = COLOR_SUCCESS
            no_cp_turn_split_text = str(no_cp_turn_split)
        elif no_cp_turn_split > 0:
            no_cp_turn_color = COLOR_ERROR
            no_cp_turn_split_text = f"+{no_cp_turn_split}"
        else:
            no_cp_turn_color = COLOR_MUTED
            no_cp_turn_split_text = "0"
    return (
        f"{message} {COLOR_BORDER}| {COLOR_TIME_HEADER}No-CP: "
        f"{COLOR_VALUE}{no_cp_seconds:.{time_decimals}f}{COLOR_MUTED}, "
        f"{COLOR_RANK_HEADER}Rank: {COLOR_VALUE}{no_cp_rank_text}"
        f"{COLOR_MUTED}, {COLOR_TURNS_HEADER}Turns: "
        f"{COLOR_VALUE}{no_cp_turns_text} {COLOR_BORDER}| "
        f"{COLOR_NAME_HEADER}Split: {no_cp_split_color}{no_cp_split_text}"
        f"{COLOR_MUTED}, {no_cp_turn_color}{no_cp_turn_split_text}"
        f"{COLOR_RESET}"
    )


def bump_resource_version(version: str) -> str:
    match = re.match(r"^(.*?)(\d+)$", version)
    if not match:
        return version + ".1"
    prefix, digits = match.groups()
    bumped = str(int(digits) + 1)
    if len(digits) > 1 and digits.startswith("0"):
        bumped = bumped.zfill(len(digits))
    return prefix + bumped


def rewrite_map_resource_version(data: bytes, version: str) -> bytes:
    """Change only the Resource version attribute, preserving all other bytes."""
    resource = RESOURCE_TAG_BYTES_RE.search(data)
    if resource is None:
        raise ValueError("map has no Resource tag")
    tag = resource.group(0)
    version_attribute = next(
        (
            match
            for match in XML_ATTRIBUTE_BYTES_RE.finditer(tag)
            if match.group(1).lower() == b"version"
        ),
        None,
    )
    if version_attribute is None:
        raise ValueError("map Resource tag has no version attribute")
    updated_tag = (
        tag[:version_attribute.start(3)]
        + version.encode("utf-8")
        + tag[version_attribute.end(3):]
    )
    return data[:resource.start()] + updated_tag + data[resource.end():]


def install_immutable_file(source: Path, destination: Path) -> None:
    """Install a file once, refusing to change bytes at an existing path."""
    data = source.read_bytes()
    if destination.exists():
        if destination.read_bytes() != data:
            raise RuntimeError(
                f"immutable resource conflict at {destination}"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def format_size_factor(value: float) -> str:
    if abs(value) < 0.0000005:
        value = 0.0
    return f"{value:.6f}".rstrip("0").rstrip(".")


def padded_center_command(value: object, padding: int = 10) -> str:
    # ReadLine strips ordinary leading whitespace. Each escaped space survives
    # parsing as one actual leading space; trailing spaces already survive.
    left = "\\ " * padding
    right = " " * padding
    text = readline_console_text(style_console_message(value))
    return f"CENTER_MESSAGE {left}{text}{right}"


def normalized_map_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in value if ch.isalnum())


def parse_intercepted_command(payload: str) -> tuple[str, str, int, str] | None:
    """Return command, player log name, access level, and argument tail."""
    parts = payload.split(maxsplit=4)
    if len(parts) < 4:
        return None
    try:
        access_level = int(parts[3])
    except ValueError:
        return None
    return (
        parts[0].casefold(),
        parts[1],
        access_level,
        parts[4].strip() if len(parts) > 4 else "",
    )


def extend_votes_required(active_players: int) -> int:
    return 1 if active_players <= 1 else math.ceil(0.60 * active_players)


def skip_votes_required(active_players: int) -> int:
    return math.floor(0.60 * max(1, active_players)) + 1


def final_countdown_seconds(records: Sequence[Record]) -> float:
    return records[0].best_seconds * 1.5 if records else 90.0


def map_play_seconds(
    records: Sequence[Record],
    maximum_seconds: float = 300.0,
    racer_time_multiplier: float = 1.25,
    target_finishes: float = 5.0,
    minimum_seconds: float = 120.0,
) -> float:
    """Return the adaptive map window, bounded by its minimum and maximum."""
    minimum = max(0.0, float(minimum_seconds))
    maximum = max(minimum, float(maximum_seconds))
    if not records:
        return maximum
    best = float(records[0].best_seconds)
    multiplier = max(0.0, float(racer_time_multiplier))
    finishes = max(0.0, float(target_finishes))
    calculated = best * multiplier * finishes
    if not math.isfinite(calculated) or calculated <= 0:
        return maximum
    return max(minimum, min(maximum, calculated))


def map_open_play_seconds(
    records: Sequence[Record],
    maximum_seconds: float = 300.0,
    racer_time_multiplier: float = 1.25,
    target_finishes: float = 5.0,
    minimum_seconds: float = 120.0,
) -> float:
    """Return the full respawn-enabled window before the final countdown."""
    return map_play_seconds(
        records,
        maximum_seconds,
        racer_time_multiplier,
        target_finishes,
        minimum_seconds,
    )


def parse_winzone_finish(payload: str) -> tuple[str, float, int | None] | None:
    """Parse the sty+ct+ap WINZONE_PLAYER_ENTER format actually emitted.

    The optional zone name may be empty; indexing from the right keeps the
    player and game-time fields stable in both forms.
    """
    parts = payload.split()
    if len(parts) < 6:
        return None
    try:
        finish_time = float(parts[-1])
    except ValueError:
        return None
    if parts[-2].startswith("turns="):
        try:
            return parts[-7], finish_time, int(parts[-2][len("turns="):])
        except (ValueError, IndexError):
            return None
    try:
        return parts[-6], finish_time, None
    except IndexError:
        return None


@dataclasses.dataclass(frozen=True)
class CheckpointEntry:
    player_name: str
    checkpoint_id: int
    game_time: float
    x: float | None = None
    y: float | None = None
    xdir: float | None = None
    ydir: float | None = None
    speed: float | None = None
    turns: int | None = None

    @property
    def has_respawn_state(self) -> bool:
        return None not in (
            self.x,
            self.y,
            self.xdir,
            self.ydir,
            self.speed,
            self.turns,
        )


def parse_checkpoint_entry(payload: str) -> CheckpointEntry | None:
    """Parse legacy or checkpoint-respawn CHECKPOINT_PLAYER_ENTER events."""
    parts = payload.split()
    if len(parts) not in {3, 9}:
        return None
    try:
        checkpoint_id = int(parts[1])
        if len(parts) == 3:
            game_time = float(parts[2])
            state: tuple[float, float, float, float, float, int] | None = None
        else:
            state = (
                float(parts[2]),
                float(parts[3]),
                float(parts[4]),
                float(parts[5]),
                float(parts[6]),
                int(parts[7]),
            )
            game_time = float(parts[8])
    except ValueError:
        return None
    if checkpoint_id <= 0 or not math.isfinite(game_time):
        return None
    if state is None:
        return CheckpointEntry(parts[0], checkpoint_id, game_time)
    x, y, xdir, ydir, speed, turns = state
    if (
        not all(math.isfinite(value) for value in (x, y, xdir, ydir, speed))
        or speed < 0
        or turns < 0
        or turns > 65535
        or (xdir == 0 and ydir == 0)
    ):
        return None
    return CheckpointEntry(
        parts[0], checkpoint_id, game_time, x, y, xdir, ydir, speed, turns
    )


def safe_resource_component(value: str) -> bool:
    if not value or value in {".", ".."}:
        return False
    return not any(ch in value for ch in "/\\();\r\n\t") and not any(
        ch.isspace() for ch in value
    )


def load_json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_helpful_messages(path: Path) -> list[str]:
    """Load one console message per nonempty, non-comment line."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [
        clean_console_text(line)
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]


def load_custom_helpful_messages(store: "StateStore") -> list[str]:
    """Return valid admin-created tips in their stable ID order."""
    state = store.get_json("custom_helpful_messages", {})
    tips = state.get("tips", []) if isinstance(state, dict) else []
    if not isinstance(tips, list):
        return []
    valid: list[tuple[int, str]] = []
    for item in tips:
        if not isinstance(item, dict):
            continue
        try:
            tip_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        message = clean_console_text(item.get("message", ""))
        if tip_id > 0 and message:
            valid.append((tip_id, message))
    return [message for _, message in sorted(valid)]


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def send_resend_report(
    api_key: str,
    recipient: str,
    sender: str,
    subject: str,
    body: str,
    endpoint: str = RESEND_ENDPOINT,
    timeout_seconds: float = 10.0,
) -> None:
    """Send one complete report without exposing its API key in logs or argv."""
    request_body = json.dumps(
        {
            "from": sender,
            "to": [recipient],
            "subject": subject,
            "text": body,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=request_body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "TronnerRacing/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = response.getcode()
            response_body = response.read(16384)
    except urllib.error.HTTPError as error:
        status = error.code
        response_body = error.read(16384)
    if not 200 <= status < 300:
        raise RuntimeError(f"report service returned HTTP {status}")
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("report service returned an invalid response") from error
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError("report service rejected the submission")


class GameLinkServiceError(RuntimeError):
    """A safe error returned by the website's one-time link endpoint."""

    def __init__(self, code: str, public_message: str):
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


def redeem_game_account_link(
    endpoint: str,
    secret: str,
    code: str,
    game_username: str,
    server_id: str,
    timeout_seconds: float = 10.0,
) -> dict[str, object]:
    """Redeem a short-lived website code for a server-authenticated global id."""
    parsed_endpoint = urllib.parse.urlsplit(endpoint)
    if parsed_endpoint.scheme != "https" or not parsed_endpoint.hostname:
        raise RuntimeError("game link endpoint must be HTTPS")
    request_body = json.dumps(
        {
            "code": code,
            "gameUsername": game_username,
            "serverId": server_id,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=request_body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "User-Agent": "TronnerRacing/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = response.getcode()
            response_body = response.read(16_384)
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            response_body = error.read(16_384)
        finally:
            error.close()
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("game link service returned an invalid response") from error
    if not 200 <= status < 300:
        details = result.get("error", {}) if isinstance(result, dict) else {}
        error_code = str(details.get("code", "link-failed"))[:80]
        public_message = str(
            details.get("message", "Unable to link that account right now.")
        ).strip()[:240]
        raise GameLinkServiceError(
            error_code,
            public_message or "Unable to link that account right now.",
        )
    if not isinstance(result, dict) or result.get("linked") is not True:
        raise RuntimeError("game link service rejected the claim")
    return result


USER_COMMAND_HELP = (
    ("/q [map]", "Queue a map after the current map."),
    ("/q remove [map]", "Remove the first matching map from the queue."),
    ("/q clear", "Clear every map from the queue."),
    ("/rate [1-5]", "Rate the current map."),
    ("/rate undo", "Undo your latest rating change on the current map."),
    ("/rate revoke", "Remove your rating from the current map."),
    ("/extend", "Vote to add five minutes to the current map."),
    ("/skip", "Vote to advance to the next map."),
    ("/nextmap", "Show the next queued or rotated map."),
    ("/rotation", "Privately show the alphabetical map rotation."),
    ("/exclusion_list", "Privately show maps excluded from rotation."),
    ("/leaderboard", "Privately show the current map's top 10 times."),
    (
        "/setspawn [#]",
        "Always use a spawn number; omit # for latest or use 0 to clear it.",
    ),
    (
        "/start [brake|immediate|countdown|respawn]",
        "Choose how your cycle begins moving after each respawn.",
    ),
    (
        "/display_server_tags",
        "Toggle server tags on other players' names (off by default).",
    ),
    (
        "/link [6-digit code]",
        "Link your authenticated in-game name to your tronner.io account.",
    ),
    (
        "/cp",
        "Respawn from your last checkpoint; a quick second /cp resets speed.",
    ),
    ("/restart", "Clear checkpoint progress and restart from the map spawn."),
    ("/respawn", "Kill your current cycle and respawn at your last checkpoint."),
    ("/sui", "Same as /respawn: kill your cycle and enable respawning."),
    ("/join", "Enable respawning without killing your current cycle."),
    ("/spec or /spectate", "Disable scripted respawning."),
    ("/report [message]", "Privately send a report to the server owner."),
    ("/help", "Show the commands available to you."),
)

ADMIN_COMMAND_HELP = (
    ("records_admin_access_level", "/forceskip", "Advance without a vote."),
    ("map_admin_access_level", "/end", "Start the end-of-map timer."),
    (
        "records_admin_access_level",
        "/resetalltimes",
        "Delete every time on the current map.",
    ),
    (
        "records_admin_access_level",
        "/reset [user] [map]",
        "Delete one user's time; map defaults to current.",
    ),
    (
        "map_admin_access_level",
        "/exclude [map] -- [reason]",
        "Hold a map out of rotation; map and reason are optional.",
    ),
    (
        "map_admin_access_level",
        "/review [map] -- [reason]",
        "Send a map and optional reason to Vectron; also supports list/remove.",
    ),
    (
        "map_admin_access_level",
        "/remove_exclusion [map]",
        "Return an excluded map to the pool.",
    ),
    ("map_admin_access_level", "/reloadmaps", "Reload maps from the repository."),
    ("size_admin_access_level", "/size [+x|-x]", "Change this map's size factor."),
)


def build_help_lines(entries: Sequence[tuple[str, str]]) -> list[str]:
    """Format command help as two left-aligned, consistently spaced columns."""
    visible_entries = [
        (plain_console_text(command), plain_console_text(description))
        for command, description in entries
    ]
    if not visible_entries:
        return []
    command_width = max(len(command) for command, _ in visible_entries)
    return [
        f"{command.ljust(command_width)} - {description}"
        for command, description in visible_entries
    ]


def build_compact_columns(
    items: Sequence[str],
    column_count: int = 4,
    gap: str = "  ",
) -> list[str]:
    """Pack sorted items top-to-bottom into compact, aligned columns."""
    raw_items = [str(item) for item in items]
    visible_items = [plain_console_text(item) for item in raw_items]
    if not visible_items or column_count <= 0:
        return []

    active_columns = min(column_count, len(visible_items))
    base_size, extra = divmod(len(visible_items), active_columns)
    sizes = [base_size + (index < extra) for index in range(active_columns)]
    columns: list[list[tuple[str, str]]] = []
    offset = 0
    for size in sizes:
        columns.append(
            list(
                zip(
                    raw_items[offset:offset + size],
                    visible_items[offset:offset + size],
                )
            )
        )
        offset += size
    widths = [max(len(visible) for _, visible in column) for column in columns]

    lines = []
    for row_index in range(max(sizes)):
        last_column = max(
            index
            for index, column in enumerate(columns)
            if row_index < len(column)
        )
        cells = []
        for column_index in range(last_column + 1):
            column = columns[column_index]
            if row_index < len(column):
                value, visible = column[row_index]
            else:
                value, visible = "", ""
            if column_index < last_column:
                value += " " * (widths[column_index] - len(visible))
            cells.append(value)
        lines.append(gap.join(cells))
    return lines


def build_rotation_columns(
    items: Sequence[tuple[str, str, str, bool]],
    column_count: int = 2,
    field_gap: str = "  ",
    column_gap: str = "   |   ",
) -> list[str]:
    """Build map blocks containing aligned name, author, and version fields."""
    raw_items = [
        (str(name), str(author), str(version), bool(is_current))
        for name, author, version, is_current in items
    ]
    if not raw_items or column_count <= 0:
        return []

    active_columns = min(column_count, len(raw_items))
    base_size, extra = divmod(len(raw_items), active_columns)
    sizes = [base_size + (index < extra) for index in range(active_columns)]
    columns: list[list[tuple[str, str, str, bool]]] = []
    offset = 0
    for size in sizes:
        columns.append(raw_items[offset:offset + size])
        offset += size

    headings = ("Map name", "Author", "Version")
    widths = []
    for column in columns:
        widths.append(
            tuple(
                max(
                    len(headings[field_index]),
                    *(
                        len(plain_console_text(item[field_index]))
                        for item in column
                    ),
                )
                for field_index in range(3)
            )
        )

    def format_block(
        values: tuple[str, str, str],
        block_widths: tuple[int, int, int],
    ) -> str:
        fields = []
        for field_index, value in enumerate(values):
            visible_width = len(plain_console_text(value))
            padding = block_widths[field_index] - visible_width
            fields.append(value + (" " * padding))
        return field_gap.join(fields)

    lines = [
        column_gap.join(
            format_block(headings, block_widths)
            for block_widths in widths
        )
    ]
    for row_index in range(max(sizes)):
        last_column = max(
            index
            for index, column in enumerate(columns)
            if row_index < len(column)
        )
        blocks = []
        for column_index in range(last_column + 1):
            column = columns[column_index]
            block_widths = widths[column_index]
            if row_index < len(column):
                name, author, version, is_current = column[row_index]
                block = format_block((name, author, version), block_widths)
                if is_current:
                    block = f"{COLOR_CURRENT_MAP}{block}{COLOR_RESET}"
            else:
                block = " " * (
                    sum(block_widths) + (len(field_gap) * 2)
                )
            blocks.append(block)
        lines.append(column_gap.join(blocks).rstrip())
    return lines


@dataclasses.dataclass(frozen=True)
class SpawnPoint:
    x: float
    y: float
    xdir: float
    ydir: float


@dataclasses.dataclass(frozen=True)
class MapEntry:
    key: str
    name: str
    author: str
    version: str
    category: str
    source_path: str
    local_path: Path
    spawns: tuple[SpawnPoint, ...]
    axes: int | None = None
    map_id: str = ""
    revision_id: str = ""
    storage_path: str = ""
    record_key: str = ""
    rating_key_override: str = ""
    checkpoint_ids: tuple[int, ...] = ()
    checkpoint_mode: str = ""
    time_decimals: int = 3

    @property
    def label(self) -> str:
        return f"{self.name} by {self.author}"

    @property
    def rating_key(self) -> str:
        """Stable logical identity shared by resource/size revisions."""
        if self.rating_key_override:
            return self.rating_key_override
        parts = [self.author, *self.category.split("/"), self.name]
        return "/".join(part for part in parts if part).casefold()

    @property
    def records_key(self) -> str:
        """Revision identity used by finish records and leaderboards."""
        return self.record_key or self.key


def map_records_key(entry: object) -> str:
    """Return a record identity while supporting lightweight test doubles."""
    return str(getattr(entry, "records_key", getattr(entry, "key")))


def map_spawn_preferences_key(entry: object) -> str:
    """Return a stable map identity that survives published revisions."""
    map_id = str(getattr(entry, "map_id", "")).strip()
    if map_id:
        return f"map-id:{map_id}"
    rating_key = str(getattr(entry, "rating_key", "")).strip().casefold()
    if rating_key:
        return f"logical:{rating_key}"
    return f"resource:{getattr(entry, 'key')}"


def _encode_unsigned_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint value must be nonnegative")
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def encode_replay_inputs(events: Iterable[tuple[int, int]]) -> bytes:
    """Pack signed microsecond deltas and two-bit actions into varints."""
    encoded = bytearray()
    previous_offset = 0
    for offset_us, action in events:
        if action < 0 or action > 3:
            raise ValueError("replay action must fit in two bits")
        delta = int(offset_us) - previous_offset
        previous_offset = int(offset_us)
        zigzag = (delta << 1) if delta >= 0 else ((-delta << 1) - 1)
        encoded.extend(_encode_unsigned_varint((zigzag << 2) | action))
    return bytes(encoded)


def decode_replay_inputs(data: bytes) -> list[tuple[int, int]]:
    """Decode a version-one replay input stream for validation/playback."""
    events: list[tuple[int, int]] = []
    value = 0
    shift = 0
    offset = 0
    for byte in data:
        value |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
            if shift > 63:
                raise ValueError("replay varint is too large")
            continue
        action = value & 3
        zigzag = value >> 2
        delta = -(zigzag // 2) - 1 if zigzag & 1 else zigzag // 2
        offset += delta
        events.append((offset, action))
        value = 0
        shift = 0
    if shift:
        raise ValueError("truncated replay varint")
    return events


def encode_replay_settings(items: Iterable[tuple[bytes, bytes]]) -> bytes:
    """Encode a deterministic, lossless settings snapshot before compression."""
    values = list(items)
    encoded = bytearray(b"TRS\x01")
    encoded.extend(_encode_unsigned_varint(len(values)))
    for name, value in values:
        encoded.extend(_encode_unsigned_varint(len(name)))
        encoded.extend(name)
        encoded.extend(_encode_unsigned_varint(len(value)))
        encoded.extend(value)
    return bytes(encoded)


def _decode_unsigned_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            raise ValueError("settings varint is too large")
    raise ValueError("truncated settings varint")


def decode_replay_settings(data: bytes) -> list[tuple[bytes, bytes]]:
    """Decode the uncompressed version-one settings snapshot format."""
    if not data.startswith(b"TRS\x01"):
        raise ValueError("unsupported replay settings format")
    offset = 4
    count, offset = _decode_unsigned_varint(data, offset)
    items: list[tuple[bytes, bytes]] = []
    for _ in range(count):
        name_length, offset = _decode_unsigned_varint(data, offset)
        name_end = offset + name_length
        if name_end > len(data):
            raise ValueError("truncated replay setting name")
        name = data[offset:name_end]
        offset = name_end
        value_length, offset = _decode_unsigned_varint(data, offset)
        value_end = offset + value_length
        if value_end > len(data):
            raise ValueError("truncated replay setting value")
        items.append((name, data[offset:value_end]))
        offset = value_end
    if offset != len(data):
        raise ValueError("trailing replay settings data")
    return items


def publish_repository_map_status(
    repository: object,
    key: str,
    status: str,
    reason: str,
) -> None:
    """Publish status when the repository backend supports mutations."""
    setter = getattr(repository, "set_map_status", None)
    if callable(setter):
        setter(key, status, reason)


@dataclasses.dataclass(frozen=True)
class CheckpointSnapshot:
    checkpoint_id: int
    x: float
    y: float
    xdir: float
    ydir: float
    speed: float
    turns: int
    event_game: float
    attempt_started_game: float
    checkpoints_collected: frozenset[int]
    no_cp_elapsed: float


@dataclasses.dataclass
class Player:
    log_name: str
    display_name: str
    auth_name: str | None = None
    colored_name: str | None = None
    color_code: str | None = None
    owner_id: int | None = None
    connected: bool = True
    active: bool = True
    forced_racing: bool = False
    alive: bool = False
    spawn_cursor: int = 0
    last_spawn_index: int | None = None
    generation: int = 0
    pending_respawn: bool = False
    respawn_created_game: float | None = None
    attempt_started_game: float | None = None
    attempt_number: int = 0
    respawn_enabled: bool = True
    is_ai: bool = False
    federation_server_id: str | None = None
    last_activity_monotonic: float | None = None
    afk: bool = False
    last_activity_position: tuple[float, float] | None = None
    activity_cycle_alive: bool = False
    activity_snapshot_seen: bool = False
    suspended_votes: dict[str, int] = dataclasses.field(default_factory=dict)
    start_mode: str = "immediate"
    display_server_tags: bool = False
    pending_start_mode: str = "immediate"
    manual_restart_pending: bool = False
    checkpoints_collected: set[int] = dataclasses.field(default_factory=set)
    checkpoint_notice_monotonic: float | None = None
    checkpoint_snapshot: CheckpointSnapshot | None = None
    checkpoint_respawn_requested: bool = False
    checkpoint_respawn_speed: float | None = None
    checkpoint_respawn_used: bool = False
    pending_respawn_kind: str = ""
    no_cp_elapsed: float = 0.0
    no_cp_segment_started_game: float | None = None
    last_checkpoint_respawn_monotonic: float | None = None
    last_checkpoint_game: float | None = None

    @property
    def target(self) -> str:
        return self.log_name

    @property
    def record_name(self) -> str:
        return self.auth_name or self.display_name or self.log_name

    @property
    def identity_key(self) -> str:
        if self.auth_name:
            return "auth:" + self.auth_name.casefold()
        if self.federation_server_id:
            return (
                "guest:"
                + self.federation_server_id.casefold()
                + ":"
                + self.record_name.casefold()
            )
        return "guest:" + self.record_name.casefold()

    @property
    def colored_display_name(self) -> str:
        if self.colored_name:
            return brighten_console_colors(self.colored_name)
        color = brighten_console_colors(self.color_code or COLOR_RESET)
        return f"{color}{self.display_name or self.log_name}"


@dataclasses.dataclass
class ReplayCapture:
    token: str
    player_log_name: str
    identity_key: str
    username: str
    authenticated: bool
    map_identifier: str
    revision_identifier: str
    resource_key: str
    started_at: float
    spawn_game_time: float
    x: float
    y: float
    xdir: float
    ydir: float
    speed: float
    initial_turns: int
    size_factor: float | None
    start_mode: str
    checkpoint_spawn: bool
    settings_identifier: str | None = None
    settings_transitions: list[tuple[int, str]] = dataclasses.field(default_factory=list)
    release_offset_us: int | None = None
    events: list[tuple[int, int]] = dataclasses.field(default_factory=list)
    seen_events: set[tuple[int, int]] = dataclasses.field(default_factory=set)
    braking: bool = False
    outcome: str = "death"
    death_reason: str = ""
    finish_seconds: float | None = None
    finish_turns: int | None = None
    personal_best: bool = False

    def update_identity(self, player: Player) -> None:
        self.identity_key = player.identity_key
        self.username = player.record_name
        self.authenticated = bool(player.auth_name)

    def update_state(
        self,
        game_time: float,
        x: float,
        y: float,
        xdir: float,
        ydir: float,
        speed: float,
        turns: int,
        released: bool = False,
    ) -> None:
        if not all(math.isfinite(value) for value in (game_time, x, y, xdir, ydir, speed)):
            return
        self.x = x
        self.y = y
        self.xdir = xdir
        self.ydir = ydir
        self.speed = max(0.0, speed)
        self.initial_turns = max(0, turns)
        if released:
            self.release_offset_us = round(
                (game_time - self.spawn_game_time) * 1_000_000
            )

    def add_input(self, game_time: float, action_name: str) -> bool:
        action = REPLAY_ACTION_CODES.get(action_name)
        if action is None or not math.isfinite(game_time):
            return False
        if action_name.startswith("B"):
            braking = action_name == "B1"
            if braking == self.braking:
                return False
            self.braking = braking
        offset_us = round((game_time - self.spawn_game_time) * 1_000_000)
        event = (offset_us, action)
        if event in self.seen_events:
            return False
        self.seen_events.add(event)
        self.events.append(event)
        return True

    def add_settings_transition(self, game_time: float, identifier: str) -> bool:
        if not identifier or not math.isfinite(game_time):
            return False
        current = (
            self.settings_transitions[-1][1]
            if self.settings_transitions
            else self.settings_identifier
        )
        if identifier == current:
            return False
        offset_us = round((game_time - self.spawn_game_time) * 1_000_000)
        if offset_us <= 0 and not self.settings_transitions:
            self.settings_identifier = identifier
            return True
        self.settings_transitions.append((max(0, offset_us), identifier))
        return True


@dataclasses.dataclass
class ReplaySettingsAssembly:
    format_version: int
    expected_count: int
    items: list[tuple[bytes, bytes]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class Record:
    identity_key: str
    username: str
    best_seconds: float
    authenticated: bool
    best_turns: int | None = None
    achieved_at: float | None = None


@dataclasses.dataclass(frozen=True)
class StoredIdentity:
    identity_key: str
    username: str
    authenticated: bool


@dataclasses.dataclass(frozen=True)
class UserMergeResult:
    records_moved: int
    finishes_moved: int
    overlapping_records: int
    replay_runs_moved: int = 0


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.owner_thread_id = threading.get_ident()
        self.thread_connections = threading.local()
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                map_key TEXT NOT NULL,
                identity_key TEXT NOT NULL,
                username TEXT NOT NULL,
                authenticated INTEGER NOT NULL,
                best_seconds REAL NOT NULL,
                best_turns INTEGER,
                achieved_at REAL NOT NULL,
                federated INTEGER NOT NULL DEFAULT 0,
                replay_available INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (map_key, identity_key)
            );
            CREATE INDEX IF NOT EXISTS records_by_map_time
                ON records(map_key, best_seconds, achieved_at);
            CREATE TABLE IF NOT EXISTS finishes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                map_key TEXT NOT NULL,
                identity_key TEXT NOT NULL,
                username TEXT NOT NULL,
                authenticated INTEGER NOT NULL,
                seconds REAL NOT NULL,
                turns INTEGER,
                finished_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS federation_record_outbox (
                map_key TEXT NOT NULL,
                identity_key TEXT NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                best_seconds REAL NOT NULL,
                best_turns INTEGER,
                achieved_at REAL NOT NULL,
                has_replay INTEGER NOT NULL DEFAULT 0,
                queued_at REAL NOT NULL,
                PRIMARY KEY (map_key, identity_key)
            );
            CREATE INDEX IF NOT EXISTS federation_record_outbox_queue
                ON federation_record_outbox(queued_at, event_id);
            CREATE TABLE IF NOT EXISTS ratings (
                map_key TEXT NOT NULL,
                identity_key TEXT NOT NULL,
                username TEXT NOT NULL,
                authenticated INTEGER NOT NULL,
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                previous_rating INTEGER CHECK(
                    previous_rating IS NULL OR previous_rating BETWEEN 1 AND 5
                ),
                undo_available INTEGER NOT NULL DEFAULT 1,
                rated_at REAL NOT NULL,
                PRIMARY KEY (map_key, identity_key)
            );
            CREATE INDEX IF NOT EXISTS ratings_by_map
                ON ratings(map_key);
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS replay_maps (
                id INTEGER PRIMARY KEY,
                map_identifier TEXT NOT NULL,
                revision_identifier TEXT NOT NULL,
                resource_key TEXT NOT NULL,
                UNIQUE(map_identifier, revision_identifier, resource_key)
            );
            CREATE TABLE IF NOT EXISTS replay_players (
                id INTEGER PRIMARY KEY,
                identity_key TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                authenticated INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS replay_settings (
                id INTEGER PRIMARY KEY,
                server_identifier TEXT NOT NULL UNIQUE,
                fingerprint_sha256 TEXT NOT NULL UNIQUE,
                format_version INTEGER NOT NULL,
                setting_count INTEGER NOT NULL,
                compression INTEGER NOT NULL,
                setting_data BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS replay_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                map_ref INTEGER NOT NULL REFERENCES replay_maps(id),
                player_ref INTEGER NOT NULL REFERENCES replay_players(id),
                recorded_at REAL NOT NULL,
                ended_at REAL NOT NULL,
                spawn_game_time REAL NOT NULL,
                release_offset_us INTEGER,
                start_x REAL NOT NULL,
                start_y REAL NOT NULL,
                start_xdir REAL NOT NULL,
                start_ydir REAL NOT NULL,
                start_speed REAL NOT NULL,
                initial_turns INTEGER NOT NULL,
                size_factor REAL,
                start_mode INTEGER NOT NULL,
                checkpoint_spawn INTEGER NOT NULL,
                settings_ref INTEGER REFERENCES replay_settings(id),
                outcome INTEGER NOT NULL,
                death_reason TEXT NOT NULL,
                finish_seconds REAL,
                finish_turns INTEGER,
                personal_best INTEGER NOT NULL,
                event_count INTEGER NOT NULL,
                format_version INTEGER NOT NULL,
                input_data BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS replay_runs_by_player
                ON replay_runs(player_ref, recorded_at DESC);
            CREATE INDEX IF NOT EXISTS replay_runs_by_map
                ON replay_runs(map_ref, recorded_at DESC);
            CREATE INDEX IF NOT EXISTS replay_runs_personal_bests
                ON replay_runs(player_ref, map_ref, personal_best)
                WHERE personal_best = 1;
            CREATE TABLE IF NOT EXISTS replay_setting_transitions (
                run_ref INTEGER NOT NULL REFERENCES replay_runs(id) ON DELETE CASCADE,
                offset_us INTEGER NOT NULL,
                settings_ref INTEGER NOT NULL REFERENCES replay_settings(id),
                PRIMARY KEY(run_ref, offset_us, settings_ref)
            );
            """
        )
        record_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(records)")
        }
        if "best_turns" not in record_columns:
            self.connection.execute("ALTER TABLE records ADD COLUMN best_turns INTEGER")
        if "federated" not in record_columns:
            self.connection.execute(
                "ALTER TABLE records ADD COLUMN federated INTEGER NOT NULL DEFAULT 0"
            )
        if "replay_available" not in record_columns:
            self.connection.execute(
                "ALTER TABLE records ADD COLUMN replay_available INTEGER NOT NULL DEFAULT 0"
            )
        outbox_columns = {
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(federation_record_outbox)"
            )
        }
        if "has_replay" not in outbox_columns:
            self.connection.execute(
                "ALTER TABLE federation_record_outbox ADD COLUMN "
                "has_replay INTEGER NOT NULL DEFAULT 0"
            )
        finish_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(finishes)")
        }
        if "turns" not in finish_columns:
            self.connection.execute("ALTER TABLE finishes ADD COLUMN turns INTEGER")
        # Older databases may contain personal-best rows from before complete
        # finish history was introduced. Backfill each missing best once while
        # leaving every already-recorded finish untouched.
        self.connection.execute(
            "INSERT INTO finishes(map_key, identity_key, username, authenticated, "
            "seconds, turns, finished_at) "
            "SELECT records.map_key, records.identity_key, records.username, "
            "records.authenticated, records.best_seconds, records.best_turns, "
            "records.achieved_at FROM records WHERE records.federated=0 "
            "AND NOT EXISTS ("
            "SELECT 1 FROM finishes WHERE finishes.map_key=records.map_key "
            "AND finishes.identity_key=records.identity_key "
            "AND finishes.seconds=records.best_seconds "
            "AND (finishes.turns=records.best_turns OR "
            "(finishes.turns IS NULL AND records.best_turns IS NULL)))"
        )
        replay_run_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(replay_runs)")
        }
        if "settings_ref" not in replay_run_columns:
            self.connection.execute(
                "ALTER TABLE replay_runs ADD COLUMN settings_ref INTEGER "
                "REFERENCES replay_settings(id)"
            )
        self.connection.commit()

    def current_connection(self) -> sqlite3.Connection:
        """Use one WAL connection per worker without sharing SQLite objects."""
        if threading.get_ident() == self.owner_thread_id:
            return self.connection
        connection = getattr(self.thread_connections, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=30.0)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            self.thread_connections.connection = connection
        return connection

    def close(self) -> None:
        self.connection.close()

    def get_json(self, key: str, default):
        row = self.current_connection().execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def set_json(self, key: str, value) -> None:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        connection = self.current_connection()
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encoded),
        )
        connection.commit()

    def add_replay_settings(
        self,
        server_identifier: str,
        format_version: int,
        items: Iterable[tuple[bytes, bytes]],
    ) -> int:
        """Store one deduplicated settings state with a collision check."""
        values = list(items)
        uncompressed = encode_replay_settings(values)
        fingerprint = hashlib.sha256(uncompressed).hexdigest()
        existing = self.connection.execute(
            "SELECT id, fingerprint_sha256 FROM replay_settings "
            "WHERE server_identifier=?",
            (server_identifier,),
        ).fetchone()
        if existing:
            if existing[1] != fingerprint:
                raise ValueError(
                    f"replay settings identifier collision: {server_identifier}"
                )
            return int(existing[0])
        blob = zlib.compress(uncompressed, level=9)
        cursor = self.connection.execute(
            "INSERT INTO replay_settings(server_identifier, fingerprint_sha256, "
            "format_version, setting_count, compression, setting_data) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                server_identifier,
                fingerprint,
                format_version,
                len(values),
                1,
                sqlite3.Binary(blob),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def replay_settings_ref(self, server_identifier: str | None) -> int | None:
        if not server_identifier:
            return None
        row = self.connection.execute(
            "SELECT id FROM replay_settings WHERE server_identifier=?",
            (server_identifier,),
        ).fetchone()
        return int(row[0]) if row else None

    def add_replay(self, capture: ReplayCapture, ended_at: float) -> int:
        """Persist one compact, physics-free cycle input stream."""
        self.connection.execute(
            "INSERT INTO replay_maps(map_identifier, revision_identifier, resource_key) "
            "VALUES(?, ?, ?) ON CONFLICT(map_identifier, revision_identifier, resource_key) "
            "DO NOTHING",
            (
                capture.map_identifier,
                capture.revision_identifier,
                capture.resource_key,
            ),
        )
        map_ref = self.connection.execute(
            "SELECT id FROM replay_maps WHERE map_identifier=? "
            "AND revision_identifier=? AND resource_key=?",
            (
                capture.map_identifier,
                capture.revision_identifier,
                capture.resource_key,
            ),
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO replay_players(identity_key, username, authenticated) "
            "VALUES(?, ?, ?) ON CONFLICT(identity_key) DO UPDATE SET "
            "username=excluded.username, authenticated=excluded.authenticated",
            (
                capture.identity_key,
                capture.username,
                int(capture.authenticated),
            ),
        )
        player_ref = self.connection.execute(
            "SELECT id FROM replay_players WHERE identity_key=?",
            (capture.identity_key,),
        ).fetchone()[0]
        start_modes = {
            "brake": 0,
            "immediate": 1,
            "countdown": 2,
            "respawn": 3,
        }
        outcomes = {
            "death": 0,
            "finish": 1,
            "replaced": 2,
            "round_end": 3,
            "controller_stop": 4,
            "invalid_finish": 5,
        }
        blob = encode_replay_inputs(capture.events)
        settings_ref = self.replay_settings_ref(capture.settings_identifier)
        cursor = self.connection.execute(
            "INSERT INTO replay_runs("
            "map_ref, player_ref, recorded_at, ended_at, spawn_game_time, "
            "release_offset_us, start_x, start_y, start_xdir, start_ydir, "
            "start_speed, initial_turns, size_factor, start_mode, checkpoint_spawn, settings_ref, "
            "outcome, death_reason, finish_seconds, finish_turns, personal_best, "
            "event_count, format_version, input_data"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                map_ref,
                player_ref,
                capture.started_at,
                ended_at,
                capture.spawn_game_time,
                capture.release_offset_us,
                capture.x,
                capture.y,
                capture.xdir,
                capture.ydir,
                capture.speed,
                capture.initial_turns,
                capture.size_factor,
                start_modes.get(capture.start_mode, 0),
                int(capture.checkpoint_spawn),
                settings_ref,
                outcomes.get(capture.outcome, 0),
                capture.death_reason,
                capture.finish_seconds,
                capture.finish_turns,
                int(capture.personal_best),
                len(capture.events),
                REPLAY_FORMAT_VERSION,
                sqlite3.Binary(blob),
            ),
        )
        run_ref = int(cursor.lastrowid)
        for offset_us, identifier in capture.settings_transitions:
            transition_ref = self.replay_settings_ref(identifier)
            if transition_ref is None:
                LOG.warning(
                    "replay %s references unknown settings %s",
                    capture.token,
                    identifier,
                )
                continue
            self.connection.execute(
                "INSERT OR IGNORE INTO replay_setting_transitions"
                "(run_ref, offset_us, settings_ref) VALUES(?, ?, ?)",
                (run_ref, offset_us, transition_ref),
            )
        self.connection.commit()
        return run_ref

    def add_finish(
        self, map_key: str, player: Player, seconds: float, turns: int | None = None
    ) -> tuple[Record, bool, float | None, int | None]:
        now = time.time()
        authenticated = bool(player.auth_name)
        self.connection.execute(
            "INSERT INTO finishes(map_key, identity_key, username, authenticated, seconds, turns, finished_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                map_key,
                player.identity_key,
                player.record_name,
                int(authenticated),
                seconds,
                turns,
                now,
            ),
        )
        old = self.connection.execute(
            "SELECT best_seconds, best_turns, achieved_at FROM records WHERE map_key=? AND identity_key=?",
            (map_key, player.identity_key),
        ).fetchone()
        previous_best = float(old[0]) if old is not None else None
        previous_best_turns = (
            int(old[1]) if old is not None and old[1] is not None else None
        )
        previous_achieved_at = (
            float(old[2]) if old is not None and old[2] is not None else None
        )
        improved = (
            previous_best is None
            or seconds < previous_best
            or (
                seconds == previous_best
                and turns is not None
                and (previous_best_turns is None or turns < previous_best_turns)
            )
        )
        if improved:
            self.connection.execute(
                "INSERT INTO records(map_key, identity_key, username, authenticated, best_seconds, best_turns, achieved_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(map_key, identity_key) DO UPDATE SET "
                "username=excluded.username, authenticated=excluded.authenticated, "
                "best_seconds=excluded.best_seconds, best_turns=excluded.best_turns, "
                "achieved_at=excluded.achieved_at, federated=0",
                (
                    map_key,
                    player.identity_key,
                    player.record_name,
                    int(authenticated),
                    seconds,
                    turns,
                    now,
                ),
            )
        else:
            self.connection.execute(
                "UPDATE records SET username=?, authenticated=? WHERE map_key=? AND identity_key=?",
                (player.record_name, int(authenticated), map_key, player.identity_key),
            )
        self.connection.commit()
        best = seconds if improved else float(previous_best)
        best_turns = turns if improved else previous_best_turns
        return (
            Record(
                player.identity_key,
                player.record_name,
                best,
                authenticated,
                best_turns,
                now if improved else previous_achieved_at,
            ),
            improved,
            previous_best,
            previous_best_turns,
        )

    def records(self, map_key: str) -> list[Record]:
        rows = self.connection.execute(
            "SELECT identity_key, username, best_seconds, authenticated, best_turns, achieved_at "
            "FROM records WHERE map_key=? ORDER BY best_seconds ASC, "
            "best_turns IS NULL ASC, best_turns ASC, achieved_at ASC",
            (map_key,),
        ).fetchall()
        return [
            Record(
                row[0],
                row[1],
                float(row[2]),
                bool(row[3]),
                int(row[4]) if row[4] is not None else None,
                float(row[5]) if row[5] is not None else None,
            )
            for row in rows
        ]

    def dashboard_record_rows(self) -> list[dict[str, object]]:
        """Return one local SQLite snapshot for precomputed public rankings."""
        connection = self.current_connection()
        rows = connection.execute(
            "SELECT map_key, identity_key, username, authenticated, best_seconds, "
            "best_turns, achieved_at, replay_available FROM records ORDER BY map_key, best_seconds, "
            "best_turns IS NULL ASC, best_turns ASC, achieved_at ASC"
        ).fetchall()
        replay_keys = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT DISTINCT replay_maps.resource_key, "
                "replay_players.identity_key FROM replay_runs "
                "JOIN replay_players ON replay_players.id=replay_runs.player_ref "
                "JOIN replay_maps ON replay_maps.id=replay_runs.map_ref WHERE "
                "replay_runs.outcome=1 AND replay_runs.finish_seconds IS NOT NULL"
            ).fetchall()
        }
        return [
            {
                "mapKey": str(row[0]),
                "identityKey": str(row[1]),
                "username": str(row[2]),
                "authenticated": bool(row[3]),
                "bestSeconds": float(row[4]),
                "bestTurns": int(row[5]) if row[5] is not None else None,
                "achievedAt": float(row[6]),
                "hasReplay": bool(row[7]) or (str(row[0]), str(row[1])) in replay_keys,
            }
            for row in rows
        ]

    def dashboard_replay_player_ids(self, map_key: str) -> set[str]:
        """Return public player ids with a viewable finished run on one map."""
        connection = self.current_connection()
        rows = connection.execute(
            "SELECT DISTINCT replay_players.identity_key FROM replay_runs "
            "JOIN replay_players ON replay_players.id=replay_runs.player_ref "
            "JOIN replay_maps ON replay_maps.id=replay_runs.map_ref WHERE "
            "replay_maps.resource_key=? AND replay_runs.outcome=1 AND "
            "replay_runs.finish_seconds IS NOT NULL",
            (map_key,),
        ).fetchall()
        identities = {str(row[0]) for row in rows}
        identities.update(
            str(row[0]) for row in connection.execute(
                "SELECT identity_key FROM records WHERE map_key=? "
                "AND replay_available=1",
                (map_key,),
            ).fetchall()
        )
        return {public_player_id(identity) for identity in identities}

    def mark_replay_available(self, map_key: str, identity_key: str) -> bool:
        """Remember that some viewable run exists for this racer and map."""
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE records SET replay_available=1 WHERE map_key=? AND "
                "identity_key=? AND authenticated=1 AND replay_available=0",
                (map_key, identity_key),
            )
        return int(cursor.rowcount) > 0

    def dashboard_finished_replays_after(
        self,
        run_id: int,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        """Return a bounded queue of finished replay captures for publishing."""
        rows = self.current_connection().execute(
            "SELECT replay_runs.id, replay_players.identity_key, "
            "replay_players.username, replay_players.authenticated, "
            "replay_maps.map_identifier, replay_maps.revision_identifier, "
            "replay_maps.resource_key, replay_runs.recorded_at, "
            "replay_runs.ended_at, replay_runs.finish_seconds, "
            "replay_runs.finish_turns, replay_runs.personal_best, "
            "replay_runs.event_count, replay_runs.settings_ref "
            "FROM replay_runs JOIN replay_players ON "
            "replay_players.id=replay_runs.player_ref JOIN replay_maps ON "
            "replay_maps.id=replay_runs.map_ref WHERE replay_runs.id>? "
            "AND replay_runs.outcome=1 AND replay_runs.finish_seconds IS NOT NULL "
            "ORDER BY replay_runs.id LIMIT ?",
            (max(0, int(run_id)), max(1, min(int(limit), 500))),
        ).fetchall()
        return [
            {
                "runId": int(row[0]),
                "identityKey": str(row[1]),
                "playerId": public_player_id(str(row[1])),
                "username": str(row[2]),
                "authenticated": bool(row[3]),
                "mapId": str(row[4]),
                "revisionId": str(row[5]),
                "mapKey": str(row[6]),
                "recordedAt": float(row[7]),
                "endedAt": float(row[8]),
                "seconds": float(row[9]),
                "turns": int(row[10]) if row[10] is not None else None,
                "personalBest": bool(row[11]),
                "eventCount": int(row[12]),
                "settingsRef": int(row[13]) if row[13] is not None else None,
            }
            for row in rows
        ]

    def dashboard_player_map_history(
        self,
        identity_key: str,
        map_key: str,
        limit: int = 2500,
    ) -> list[dict[str, object]]:
        rows = self.current_connection().execute(
            "SELECT replay_runs.id, replay_runs.recorded_at, "
            "replay_runs.ended_at, replay_runs.finish_seconds, "
            "replay_runs.finish_turns, replay_runs.personal_best, "
            "replay_runs.event_count, replay_runs.settings_ref, "
            "replay_maps.map_identifier, replay_maps.revision_identifier "
            "FROM replay_runs JOIN replay_players ON "
            "replay_players.id=replay_runs.player_ref JOIN replay_maps ON "
            "replay_maps.id=replay_runs.map_ref WHERE "
            "replay_players.identity_key=? AND replay_maps.resource_key=? "
            "AND replay_runs.outcome=1 AND replay_runs.finish_seconds IS NOT NULL "
            "ORDER BY replay_runs.recorded_at DESC, replay_runs.id DESC LIMIT ?",
            (identity_key, map_key, max(1, min(int(limit), 2500))),
        ).fetchall()
        return [
            {
                "runId": int(row[0]),
                "recordedAt": int(float(row[1]) * 1000),
                "endedAt": int(float(row[2]) * 1000),
                "seconds": round(float(row[3]), 6),
                "turns": int(row[4]) if row[4] is not None else None,
                "personalBest": bool(row[5]),
                "eventCount": int(row[6]),
                "settingsRef": int(row[7]) if row[7] is not None else None,
                "mapId": str(row[8]),
                "revisionId": str(row[9]),
            }
            for row in rows
        ]

    def dashboard_replay_payload(self, run_id: int) -> dict[str, object] | None:
        connection = self.current_connection()
        row = connection.execute(
            "SELECT replay_runs.id, replay_players.identity_key, "
            "replay_players.username, replay_players.authenticated, "
            "replay_maps.map_identifier, replay_maps.revision_identifier, "
            "replay_maps.resource_key, replay_runs.recorded_at, "
            "replay_runs.ended_at, replay_runs.spawn_game_time, "
            "replay_runs.release_offset_us, replay_runs.start_x, "
            "replay_runs.start_y, replay_runs.start_xdir, "
            "replay_runs.start_ydir, replay_runs.start_speed, "
            "replay_runs.initial_turns, replay_runs.size_factor, "
            "replay_runs.start_mode, replay_runs.checkpoint_spawn, "
            "replay_runs.settings_ref, replay_runs.finish_seconds, "
            "replay_runs.finish_turns, replay_runs.personal_best, "
            "replay_runs.format_version, replay_runs.input_data "
            "FROM replay_runs JOIN replay_players ON "
            "replay_players.id=replay_runs.player_ref JOIN replay_maps ON "
            "replay_maps.id=replay_runs.map_ref WHERE replay_runs.id=? "
            "AND replay_runs.outcome=1 AND replay_runs.finish_seconds IS NOT NULL",
            (int(run_id),),
        ).fetchone()
        if row is None:
            return None
        events = decode_replay_inputs(bytes(row[25]))
        transitions = connection.execute(
            "SELECT replay_setting_transitions.offset_us, replay_settings.fingerprint_sha256 "
            "FROM replay_setting_transitions JOIN replay_settings ON "
            "replay_settings.id=replay_setting_transitions.settings_ref "
            "WHERE replay_setting_transitions.run_ref=? ORDER BY "
            "replay_setting_transitions.offset_us",
            (int(run_id),),
        ).fetchall()
        settings_fingerprint = ""
        if row[20] is not None:
            settings_row = connection.execute(
                "SELECT fingerprint_sha256 FROM replay_settings WHERE id=?",
                (int(row[20]),),
            ).fetchone()
            settings_fingerprint = str(settings_row[0]) if settings_row else ""
        return {
            "schemaVersion": 1,
            "formatVersion": int(row[24]),
            "runId": int(row[0]),
            "playerId": public_player_id(str(row[1])),
            "name": str(row[2])[:128],
            "authenticated": bool(row[3]),
            "mapId": str(row[4]),
            "revisionId": str(row[5]),
            "mapKey": str(row[6]),
            "recordedAt": int(float(row[7]) * 1000),
            "endedAt": int(float(row[8]) * 1000),
            "spawnGameTime": round(float(row[9]), 6),
            "releaseOffsetUs": int(row[10]) if row[10] is not None else None,
            "start": {
                "x": round(float(row[11]), 9),
                "y": round(float(row[12]), 9),
                "xdir": round(float(row[13]), 12),
                "ydir": round(float(row[14]), 12),
                "speed": round(float(row[15]), 9),
                "turns": int(row[16]),
                "sizeFactor": float(row[17]) if row[17] is not None else None,
                "mode": int(row[18]),
                "checkpoint": bool(row[19]),
            },
            "settingsFingerprint": settings_fingerprint,
            "settingsTransitions": [
                [int(offset), str(fingerprint)]
                for offset, fingerprint in transitions
            ],
            "seconds": round(float(row[21]), 6),
            "turns": int(row[22]) if row[22] is not None else None,
            "personalBest": bool(row[23]),
            "events": [[int(offset), int(action)] for offset, action in events],
        }

    def dashboard_replay_settings(self, settings_ref: int) -> dict[str, object] | None:
        row = self.current_connection().execute(
            "SELECT fingerprint_sha256, format_version, setting_count, "
            "compression, setting_data FROM replay_settings WHERE id=?",
            (int(settings_ref),),
        ).fetchone()
        if row is None:
            return None
        raw = zlib.decompress(bytes(row[4])) if int(row[3]) == 1 else bytes(row[4])
        items = decode_replay_settings(raw)
        return {
            "schemaVersion": 1,
            "formatVersion": int(row[1]),
            "fingerprint": str(row[0]),
            "settingCount": int(row[2]),
            "settings": [
                [name.decode("utf-8", "replace"), value.decode("utf-8", "replace")]
                for name, value in items
            ],
        }

    def dashboard_replay_settings_by_fingerprint(
        self,
        fingerprint: str,
    ) -> dict[str, object] | None:
        row = self.current_connection().execute(
            "SELECT id FROM replay_settings WHERE fingerprint_sha256=?",
            (str(fingerprint),),
        ).fetchone()
        return self.dashboard_replay_settings(int(row[0])) if row else None

    @staticmethod
    def _federation_record_event_id(
        local_server_id: str,
        map_key: str,
        identity_key: str,
        username: str,
        best_seconds: float,
        best_turns: int | None,
        achieved_at: float,
        has_replay: bool,
    ) -> str:
        material = json.dumps(
            [
                local_server_id,
                map_key,
                identity_key,
                username,
                best_seconds,
                best_turns,
                achieved_at,
                has_replay,
            ],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _queue_federation_record_row(
        self,
        local_server_id: str,
        row: Sequence[object],
        queued_at: float,
    ) -> None:
        map_key = str(row[0])
        identity_key = str(row[1])
        username = str(row[2])
        best_seconds = float(row[3])
        best_turns = int(row[4]) if row[4] is not None else None
        achieved_at = float(row[5])
        has_replay = bool(row[6])
        event_id = self._federation_record_event_id(
            local_server_id,
            map_key,
            identity_key,
            username,
            best_seconds,
            best_turns,
            achieved_at,
            has_replay,
        )
        self.connection.execute(
            "INSERT INTO federation_record_outbox("
            "map_key, identity_key, event_id, username, best_seconds, "
            "best_turns, achieved_at, has_replay, queued_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(map_key, identity_key) DO UPDATE SET "
            "event_id=excluded.event_id, username=excluded.username, "
            "best_seconds=excluded.best_seconds, best_turns=excluded.best_turns, "
            "achieved_at=excluded.achieved_at, has_replay=excluded.has_replay, "
            "queued_at=excluded.queued_at",
            (
                map_key,
                identity_key,
                event_id,
                username,
                best_seconds,
                best_turns,
                achieved_at,
                int(has_replay),
                queued_at,
            ),
        )

    def seed_federation_record_outbox(self, local_server_id: str) -> int:
        """Queue the current authenticated PB snapshot for convergence."""
        rows = self.connection.execute(
            "SELECT records.map_key, records.identity_key, records.username, "
            "records.best_seconds, records.best_turns, records.achieved_at, "
            "records.replay_available OR EXISTS(SELECT 1 FROM replay_runs "
            "JOIN replay_players ON replay_players.id=replay_runs.player_ref "
            "JOIN replay_maps ON replay_maps.id=replay_runs.map_ref WHERE "
            "replay_players.identity_key=records.identity_key AND "
            "replay_maps.resource_key=records.map_key AND replay_runs.outcome=1 "
            "AND replay_runs.finish_seconds IS NOT NULL) FROM records WHERE "
            "records.authenticated=1"
        ).fetchall()
        queued_at = time.time()
        with self.connection:
            for row in rows:
                self._queue_federation_record_row(local_server_id, row, queued_at)
        return len(rows)

    def queue_federation_record(
        self,
        local_server_id: str,
        map_key: str,
        identity_key: str,
    ) -> bool:
        """Durably queue one authenticated local PB for the peer."""
        row = self.connection.execute(
            "SELECT records.map_key, records.identity_key, records.username, "
            "records.best_seconds, records.best_turns, records.achieved_at, "
            "records.replay_available OR EXISTS(SELECT 1 FROM replay_runs "
            "JOIN replay_players ON replay_players.id=replay_runs.player_ref "
            "JOIN replay_maps ON replay_maps.id=replay_runs.map_ref WHERE "
            "replay_players.identity_key=records.identity_key AND "
            "replay_maps.resource_key=records.map_key AND replay_runs.outcome=1 "
            "AND replay_runs.finish_seconds IS NOT NULL) FROM records WHERE "
            "records.map_key=? AND records.identity_key=? "
            "AND records.authenticated=1",
            (map_key, identity_key),
        ).fetchone()
        if row is None:
            return False
        with self.connection:
            self._queue_federation_record_row(local_server_id, row, time.time())
        return True

    def pending_federation_records(self, limit: int) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT event_id, map_key, identity_key, username, best_seconds, "
            "best_turns, achieved_at, has_replay FROM federation_record_outbox "
            "ORDER BY queued_at, event_id LIMIT ?",
            (max(1, min(int(limit), MAX_FEDERATION_RECORDS_PER_BATCH)),),
        ).fetchall()
        return [
            {
                "event_id": str(row[0]),
                "map_key": str(row[1]),
                "identity_key": str(row[2]),
                "username": str(row[3]),
                "best_seconds": float(row[4]),
                "best_turns": int(row[5]) if row[5] is not None else None,
                "achieved_at": float(row[6]),
                "has_replay": bool(row[7]),
            }
            for row in rows
        ]

    def federation_record_snapshot(
        self,
        local_server_id: str,
        limit: int,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        """Return a stable authenticated PB page for a newly joined peer."""
        rows = self.connection.execute(
            "SELECT records.map_key, records.identity_key, records.username, "
            "records.best_seconds, records.best_turns, records.achieved_at, "
            "records.replay_available OR EXISTS(SELECT 1 FROM replay_runs "
            "JOIN replay_players ON replay_players.id=replay_runs.player_ref "
            "JOIN replay_maps ON replay_maps.id=replay_runs.map_ref WHERE "
            "replay_players.identity_key=records.identity_key AND "
            "replay_maps.resource_key=records.map_key AND replay_runs.outcome=1 "
            "AND replay_runs.finish_seconds IS NOT NULL) FROM records WHERE "
            "records.authenticated=1 ORDER BY records.map_key, records.identity_key "
            "LIMIT ? OFFSET ?",
            (
                max(1, min(int(limit), MAX_FEDERATION_RECORDS_PER_BATCH)),
                max(0, int(offset)),
            ),
        ).fetchall()
        records = []
        for row in rows:
            map_key = str(row[0])
            identity_key = str(row[1])
            username = str(row[2])
            best_seconds = float(row[3])
            best_turns = int(row[4]) if row[4] is not None else None
            achieved_at = float(row[5])
            has_replay = bool(row[6])
            records.append({
                "event_id": self._federation_record_event_id(
                    local_server_id,
                    map_key,
                    identity_key,
                    username,
                    best_seconds,
                    best_turns,
                    achieved_at,
                    has_replay,
                ),
                "map_key": map_key,
                "identity_key": identity_key,
                "username": username,
                "best_seconds": best_seconds,
                "best_turns": best_turns,
                "achieved_at": achieved_at,
                "has_replay": has_replay,
            })
        return records

    def acknowledge_federation_records(self, event_ids: Sequence[str]) -> int:
        values = list(dict.fromkeys(event_ids))
        if not values:
            return 0
        placeholders = ",".join("?" for _ in values)
        with self.connection:
            cursor = self.connection.execute(
                f"DELETE FROM federation_record_outbox "
                f"WHERE event_id IN ({placeholders})",
                values,
            )
        return max(0, int(cursor.rowcount))

    def apply_federated_record(
        self,
        *,
        map_key: str,
        identity_key: str,
        username: str,
        best_seconds: float,
        best_turns: int | None,
        achieved_at: float,
        has_replay: bool = False,
    ) -> bool:
        """Merge one authenticated peer PB without duplicating finish history."""
        existing = self.connection.execute(
            "SELECT best_seconds, best_turns, username, authenticated, achieved_at, "
            "replay_available "
            "FROM records WHERE map_key=? AND identity_key=?",
            (map_key, identity_key),
        ).fetchone()
        incoming_rank = (
            best_seconds,
            math.inf if best_turns is None else best_turns,
        )
        existing_rank = (
            (
                float(existing[0]),
                math.inf if existing[1] is None else int(existing[1]),
            )
            if existing is not None
            else None
        )
        if existing_rank is None or incoming_rank < existing_rank:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO records(map_key, identity_key, username, "
                    "authenticated, best_seconds, best_turns, achieved_at, "
                    "federated, replay_available) VALUES(?, ?, ?, 1, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(map_key, identity_key) DO UPDATE SET "
                    "username=excluded.username, authenticated=1, "
                    "best_seconds=excluded.best_seconds, "
                    "best_turns=excluded.best_turns, "
                    "achieved_at=excluded.achieved_at, federated=1, "
                    "replay_available=MAX(records.replay_available, "
                    "excluded.replay_available)",
                    (
                        map_key,
                        identity_key,
                        username,
                        best_seconds,
                        best_turns,
                        achieved_at,
                        int(has_replay),
                    ),
                )
            return True
        if incoming_rank == existing_rank:
            earliest = min(float(existing[4]), achieved_at)
            changed = (
                str(existing[2]) != username
                or not bool(existing[3])
                or float(existing[4]) != earliest
                or (has_replay and not bool(existing[5]))
            )
            if changed:
                with self.connection:
                    self.connection.execute(
                        "UPDATE records SET username=?, authenticated=1, "
                        "achieved_at=?, replay_available=MAX(replay_available, ?) "
                        "WHERE map_key=? AND identity_key=?",
                        (username, earliest, int(has_replay), map_key, identity_key),
                    )
            return changed
        return False

    def rating_average(self, map_key: str) -> float | None:
        row = self.connection.execute(
            "SELECT AVG(rating) FROM ratings WHERE map_key=?", (map_key,)
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def rating_for(self, map_key: str, identity_key: str) -> int | None:
        row = self.connection.execute(
            "SELECT rating FROM ratings WHERE map_key=? AND identity_key=?",
            (map_key, identity_key),
        ).fetchone()
        return int(row[0]) if row else None

    def set_rating(
        self, map_key: str, player: Player, rating: int
    ) -> tuple[int | None, bool]:
        if not 1 <= rating <= 5:
            raise ValueError("rating must be between 1 and 5")
        previous = self.rating_for(map_key, player.identity_key)
        now = time.time()
        if previous == rating:
            with self.connection:
                self.connection.execute(
                    "UPDATE ratings SET username=?, authenticated=?, rated_at=? "
                    "WHERE map_key=? AND identity_key=?",
                    (
                        player.record_name,
                        int(bool(player.auth_name)),
                        now,
                        map_key,
                        player.identity_key,
                    ),
                )
            return previous, False
        with self.connection:
            self.connection.execute(
                "INSERT INTO ratings("
                "map_key, identity_key, username, authenticated, rating, "
                "previous_rating, undo_available, rated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(map_key, identity_key) DO UPDATE SET "
                "username=excluded.username, "
                "authenticated=excluded.authenticated, "
                "rating=excluded.rating, "
                "previous_rating=excluded.previous_rating, "
                "undo_available=1, "
                "rated_at=excluded.rated_at",
                (
                    map_key,
                    player.identity_key,
                    player.record_name,
                    int(bool(player.auth_name)),
                    rating,
                    previous,
                    now,
                ),
            )
        return previous, True

    def undo_rating(
        self, map_key: str, identity_key: str
    ) -> tuple[int, int | None] | None:
        row = self.connection.execute(
            "SELECT rating, previous_rating, undo_available FROM ratings "
            "WHERE map_key=? AND identity_key=?",
            (map_key, identity_key),
        ).fetchone()
        if not row or not bool(row[2]):
            return None
        current = int(row[0])
        previous = int(row[1]) if row[1] is not None else None
        with self.connection:
            if previous is None:
                self.connection.execute(
                    "DELETE FROM ratings WHERE map_key=? AND identity_key=?",
                    (map_key, identity_key),
                )
            else:
                self.connection.execute(
                    "UPDATE ratings SET rating=?, previous_rating=NULL, "
                    "undo_available=0, rated_at=? "
                    "WHERE map_key=? AND identity_key=?",
                    (previous, time.time(), map_key, identity_key),
                )
        return current, previous

    def revoke_rating(self, map_key: str, identity_key: str) -> int | None:
        current = self.rating_for(map_key, identity_key)
        if current is None:
            return None
        with self.connection:
            self.connection.execute(
                "DELETE FROM ratings WHERE map_key=? AND identity_key=?",
                (map_key, identity_key),
            )
        return current

    def reset_map(self, map_key: str) -> tuple[int, int]:
        record_count = self.connection.execute(
            "SELECT COUNT(*) FROM records WHERE map_key=?", (map_key,)
        ).fetchone()[0]
        finish_count = self.connection.execute(
            "SELECT COUNT(*) FROM finishes WHERE map_key=?", (map_key,)
        ).fetchone()[0]
        with self.connection:
            self.connection.execute("DELETE FROM records WHERE map_key=?", (map_key,))
            self.connection.execute("DELETE FROM finishes WHERE map_key=?", (map_key,))
        return int(record_count), int(finish_count)

    def reset_user(self, map_key: str, username: str) -> tuple[list[str], int, int]:
        query = plain_console_text(username).strip().casefold()
        if not query:
            return [], 0, 0
        rows = self.connection.execute(
            "SELECT identity_key, username FROM records WHERE map_key=? "
            "UNION SELECT identity_key, username FROM finishes WHERE map_key=?",
            (map_key, map_key),
        ).fetchall()
        identities: set[str] = set()
        names: set[str] = set()
        for identity_key, stored_name in rows:
            identity_fold = str(identity_key).casefold()
            identity_name = identity_fold.split(":", 1)[-1]
            if query in {
                identity_fold,
                identity_name,
                plain_console_text(stored_name).casefold(),
            }:
                identities.add(str(identity_key))
                names.add(str(stored_name))
        if not identities:
            return [], 0, 0
        placeholders = ",".join("?" for _ in identities)
        parameters = [map_key, *sorted(identities)]
        record_count = self.connection.execute(
            f"SELECT COUNT(*) FROM records WHERE map_key=? "
            f"AND identity_key IN ({placeholders})",
            parameters,
        ).fetchone()[0]
        finish_count = self.connection.execute(
            f"SELECT COUNT(*) FROM finishes WHERE map_key=? "
            f"AND identity_key IN ({placeholders})",
            parameters,
        ).fetchone()[0]
        with self.connection:
            self.connection.execute(
                f"DELETE FROM records WHERE map_key=? "
                f"AND identity_key IN ({placeholders})",
                parameters,
            )
            self.connection.execute(
                f"DELETE FROM finishes WHERE map_key=? "
                f"AND identity_key IN ({placeholders})",
                parameters,
            )
        return sorted(names, key=str.casefold), int(record_count), int(finish_count)

    def matching_user_identities(self, username: str) -> list[StoredIdentity]:
        """Find saved-time identities without silently merging ambiguous users."""
        query = plain_console_text(username).strip().casefold()
        if not query:
            return []
        rows = self.connection.execute(
            "SELECT identity_key, username, authenticated, saved_at FROM ("
            "SELECT identity_key, username, authenticated, achieved_at AS saved_at "
            "FROM records UNION ALL "
            "SELECT identity_key, username, authenticated, finished_at AS saved_at "
            "FROM finishes UNION ALL "
            "SELECT replay_players.identity_key, replay_players.username, "
            "replay_players.authenticated, MAX(replay_runs.recorded_at) AS saved_at "
            "FROM replay_players JOIN replay_runs "
            "ON replay_runs.player_ref=replay_players.id "
            "GROUP BY replay_players.id) ORDER BY saved_at DESC"
        ).fetchall()
        identities: dict[str, StoredIdentity] = {}
        direct_matches: set[str] = set()
        name_matches: set[str] = set()
        explicit_identity = query.startswith(("auth:", "guest:"))
        for identity_key, stored_name, authenticated, _ in rows:
            identity_key = str(identity_key)
            identity_fold = identity_key.casefold()
            identity = identities.setdefault(
                identity_fold,
                StoredIdentity(identity_key, str(stored_name), bool(authenticated)),
            )
            if query == identity_fold or (
                not explicit_identity and query == identity_fold.split(":", 1)[-1]
            ):
                direct_matches.add(identity.identity_key.casefold())
            if (
                not explicit_identity
                and query == plain_console_text(stored_name).strip().casefold()
            ):
                name_matches.add(identity.identity_key.casefold())
        selected = direct_matches if direct_matches else name_matches
        return sorted(
            (identities[key] for key in selected),
            key=lambda identity: identity.identity_key.casefold(),
        )

    @staticmethod
    def identity_for_player(player: Player) -> StoredIdentity:
        return StoredIdentity(
            player.identity_key,
            player.record_name,
            bool(player.auth_name),
        )

    @staticmethod
    def explicit_user_identity(username: str) -> StoredIdentity | None:
        explicit = plain_console_text(username).strip()
        explicit_fold = explicit.casefold()
        if explicit_fold.startswith("auth:"):
            return StoredIdentity(
                explicit_fold,
                explicit.split(":", 1)[1],
                True,
            )
        if explicit_fold.startswith("guest:"):
            return StoredIdentity(
                explicit_fold,
                explicit.split(":", 1)[1],
                False,
            )
        return None

    def merge_users(
        self,
        source_identity_key: str,
        destination: StoredIdentity,
    ) -> UserMergeResult:
        """Atomically move all times, finishes, and replays to ``destination``."""
        if source_identity_key.casefold() == destination.identity_key.casefold():
            raise ValueError("source and destination identities are the same")
        source_records = self.connection.execute(
            "SELECT map_key, best_seconds, best_turns, achieved_at FROM records "
            "WHERE identity_key=?",
            (source_identity_key,),
        ).fetchall()
        finish_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM finishes WHERE identity_key=?",
                (source_identity_key,),
            ).fetchone()[0]
        )
        source_replay_player = self.connection.execute(
            "SELECT id FROM replay_players WHERE identity_key=?",
            (source_identity_key,),
        ).fetchone()
        replay_count = 0
        if source_replay_player is not None:
            replay_count = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM replay_runs WHERE player_ref=?",
                    (source_replay_player[0],),
                ).fetchone()[0]
            )
        overlapping_records = 0
        with self.connection:
            for map_key, seconds, turns, achieved_at in source_records:
                existing = self.connection.execute(
                    "SELECT best_seconds, best_turns FROM records "
                    "WHERE map_key=? AND identity_key=?",
                    (map_key, destination.identity_key),
                ).fetchone()
                source_rank = (
                    float(seconds),
                    math.inf if turns is None else int(turns),
                )
                if existing is None:
                    self.connection.execute(
                        "INSERT INTO records("
                        "map_key, identity_key, username, authenticated, "
                        "best_seconds, best_turns, achieved_at) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (
                            map_key,
                            destination.identity_key,
                            destination.username,
                            int(destination.authenticated),
                            seconds,
                            turns,
                            achieved_at,
                        ),
                    )
                else:
                    overlapping_records += 1
                    destination_rank = (
                        float(existing[0]),
                        math.inf if existing[1] is None else int(existing[1]),
                    )
                    if source_rank < destination_rank:
                        self.connection.execute(
                            "UPDATE records SET username=?, authenticated=?, "
                            "best_seconds=?, best_turns=?, achieved_at=? "
                            "WHERE map_key=? AND identity_key=?",
                            (
                                destination.username,
                                int(destination.authenticated),
                                seconds,
                                turns,
                                achieved_at,
                                map_key,
                                destination.identity_key,
                            ),
                        )
                    else:
                        self.connection.execute(
                            "UPDATE records SET username=?, authenticated=? "
                            "WHERE map_key=? AND identity_key=?",
                            (
                                destination.username,
                                int(destination.authenticated),
                                map_key,
                                destination.identity_key,
                            ),
                        )
            self.connection.execute(
                "UPDATE finishes SET identity_key=?, username=?, authenticated=? "
                "WHERE identity_key=?",
                (
                    destination.identity_key,
                    destination.username,
                    int(destination.authenticated),
                    source_identity_key,
                ),
            )
            self.connection.execute(
                "DELETE FROM records WHERE identity_key=?",
                (source_identity_key,),
            )
            if source_replay_player is not None:
                self.connection.execute(
                    "INSERT INTO replay_players(identity_key, username, authenticated) "
                    "VALUES(?, ?, ?) ON CONFLICT(identity_key) DO UPDATE SET "
                    "username=excluded.username, authenticated=excluded.authenticated",
                    (
                        destination.identity_key,
                        destination.username,
                        int(destination.authenticated),
                    ),
                )
                destination_replay_player = self.connection.execute(
                    "SELECT id FROM replay_players WHERE identity_key=?",
                    (destination.identity_key,),
                ).fetchone()[0]
                self.connection.execute(
                    "UPDATE replay_runs SET player_ref=? WHERE player_ref=?",
                    (destination_replay_player, source_replay_player[0]),
                )
                self.connection.execute(
                    "DELETE FROM replay_players WHERE id=?",
                    (source_replay_player[0],),
                )
        return UserMergeResult(
            records_moved=len(source_records),
            finishes_moved=finish_count,
            overlapping_records=overlapping_records,
            replay_runs_moved=replay_count,
        )


@dataclasses.dataclass(frozen=True)
class HotCommandDefinition:
    command: str
    handler: object
    access_setting: str
    access_denied: str
    help_command: str
    help_description: str


class HotCommandRegistry:
    """Atomically reload standalone admin command modules when files change."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._commands: dict[str, HotCommandDefinition] = {}
        self._last_attempted_fingerprint: tuple[tuple[str, str], ...] | None = None
        self.last_error: str | None = None

    def _snapshot(
        self,
    ) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[Path, bytes], ...]]:
        if not self.directory.is_dir():
            return (), ()
        files = tuple(
            (path, path.read_bytes())
            for path in sorted(self.directory.glob("*.py"))
            if not path.name.startswith("_")
        )
        fingerprint = tuple(
            (path.name, hashlib.sha256(source).hexdigest())
            for path, source in files
        )
        return fingerprint, files

    def reload_if_changed(self) -> bool:
        try:
            fingerprint, files = self._snapshot()
        except OSError as exc:
            self.last_error = str(exc)
            LOG.exception("reading hot command modules failed")
            return False
        if fingerprint == self._last_attempted_fingerprint:
            return False
        self._last_attempted_fingerprint = fingerprint
        candidate: dict[str, HotCommandDefinition] = {}
        try:
            for path, source in files:
                namespace = {
                    "__builtins__": __builtins__,
                    "__file__": str(path),
                    "__name__": f"_tronner_hot_command_{path.stem}",
                }
                exec(compile(source, str(path), "exec"), namespace)
                declarations = namespace.get("COMMANDS")
                if not isinstance(declarations, dict):
                    raise TypeError(f"{path.name} must define a COMMANDS dictionary")
                for raw_command, metadata in declarations.items():
                    command = str(raw_command).strip().casefold()
                    if not command.startswith("/") or any(
                        character.isspace() for character in command
                    ):
                        raise ValueError(
                            f"invalid command name {raw_command!r} in {path.name}"
                        )
                    if command in candidate:
                        raise ValueError(f"duplicate hot command: {command}")
                    if not isinstance(metadata, dict):
                        raise TypeError(f"metadata for {command} must be a dictionary")
                    handler = metadata.get("handler")
                    if not callable(handler):
                        raise TypeError(f"handler for {command} is not callable")
                    candidate[command] = HotCommandDefinition(
                        command=command,
                        handler=handler,
                        access_setting=str(metadata["access_setting"]),
                        access_denied=str(metadata["access_denied"]),
                        help_command=str(metadata["help_command"]),
                        help_description=str(metadata["help_description"]),
                    )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            LOG.exception(
                "hot command reload failed; retaining %d last-known-good command(s)",
                len(self._commands),
            )
            return False
        self._commands = candidate
        self.last_error = None
        LOG.info(
            "loaded %d hot admin command(s) from %s",
            len(candidate),
            self.directory,
        )
        return True

    async def dispatch(
        self,
        controller,
        command: str,
        player: Player,
        access_level: int,
        arguments: str,
    ) -> bool:
        self.reload_if_changed()
        definition = self._commands.get(command.casefold())
        if definition is None:
            return False
        maximum_access = int(controller.config.get(definition.access_setting, 1))
        if access_level > maximum_access:
            await controller.private(player, definition.access_denied)
            return True
        await definition.handler(controller, player, access_level, arguments)
        return True

    def help_entries(
        self, config: dict, access_level: int
    ) -> list[tuple[str, str]]:
        return [
            (definition.help_command, definition.help_description)
            for definition in sorted(
                self._commands.values(), key=lambda item: item.command
            )
            if access_level <= int(config.get(definition.access_setting, 1))
        ]


class CommandSink:
    def __init__(self, path: Path, encoding: str = "utf-8"):
        self.path = path
        self.encoding = canonical_game_text_encoding(encoding, "utf-8")
        self.lock = asyncio.Lock()

    def set_encoding(self, encoding: str) -> None:
        self.encoding = canonical_game_text_encoding(encoding, self.encoding)

    async def send(self, *commands: str) -> None:
        lines = []
        for command in commands:
            # Preserve intentional trailing whitespace in CENTER_MESSAGE while
            # still guaranteeing one physical console command per item.
            command = str(command).replace("\r", " ").replace("\n", " ")
            if command.strip():
                lines.append(command)
        if not lines:
            return
        payload = encode_game_text(
            "\n".join(lines) + "\n",
            self.encoding,
            "Armagetron console command",
        )
        async with self.lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)


class MapRepository:
    def __init__(self, config: dict):
        self.source = str(config.get("repository_source", "git")).strip().casefold()
        if self.source not in {"git", "firebase"}:
            raise ValueError("repository_source must be git or firebase")
        self.git_url = config["repository_git_url"]
        self.branch = config.get("repository_branch", "main")
        self.git_checkout = Path(config["repository_checkout"])
        self.firebase_root = Path(
            config.get(
                "firebase_catalog_dir",
                "/var/lib/tronner-racing/firebase-catalog",
            )
        )
        self.checkout = (
            self.firebase_root / "current"
            if self.source == "firebase"
            else self.git_checkout
        )
        self.firebase = (
            FirebaseCatalogClient(config) if self.source == "firebase" else None
        )
        self.firebase_require_ready = bool(
            config.get("firebase_catalog_require_ready", True)
        )
        self.firebase_publish_wait_seconds = max(
            5.0,
            float(config.get("firebase_catalog_publish_wait_seconds", 60)),
        )
        self.firebase_maps_by_key: dict[str, dict] = {}
        self.firebase_inactive_keys: set[str] = set()
        self.firebase_generation = ""
        self.firebase_catalog_version = 0
        self.public_dir = Path(config["public_dir"])
        self.cache_dir = Path(config["resource_cache_dir"])
        self.dtd_source_dir = Path(config["dtd_source_dir"])
        self.override_dir = Path(
            config.get("map_override_dir", "/var/lib/tronner-racing/map-overrides")
        )
        self.revision_dir = Path(
            config.get(
                "map_revision_dir",
                "/var/lib/tronner-racing/map-revisions",
            )
        )
        self.excluded_keys: set[str] = set()
        self.catalog: dict[str, MapEntry] = {}
        self.source_to_key: dict[str, str] = {}
        self.issues: list[str] = []

    def sync(
        self,
        restore_worktree: bool = False,
        *,
        catalog_state: dict | None = None,
        force_firestore: bool = False,
    ) -> dict | None:
        if self.firebase is not None:
            manifest = self.firebase.sync_snapshot(
                self.firebase_root,
                require_ready=self.firebase_require_ready,
                catalog_state=catalog_state,
                force_firestore=force_firestore,
            )
            self._load_firebase_manifest()
            self.scan()
            return manifest
        self.checkout.parent.mkdir(parents=True, exist_ok=True)
        if (self.checkout / ".git").is_dir():
            if restore_worktree:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.checkout),
                        "restore",
                        "--source=HEAD",
                        "--worktree",
                        "--",
                        ".",
                    ],
                    check=True,
                    timeout=30,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            subprocess.run(
                ["git", "-C", str(self.checkout), "pull", "--ff-only", "origin", self.branch],
                check=True,
                timeout=90,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        else:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", self.branch, self.git_url, str(self.checkout)],
                check=True,
                timeout=120,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        self.scan()
        return None

    def _load_firebase_manifest(self) -> None:
        if self.firebase is None:
            self.firebase_maps_by_key = {}
            self.firebase_inactive_keys = set()
            return
        manifest_path = self.checkout / ".catalog.json"
        try:
            manifest = json.loads(manifest_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FirebaseCatalogError(
                f"unable to read Firebase catalog manifest: {exc}"
            ) from exc
        maps = manifest.get("maps", [])
        if not isinstance(maps, list) or not maps:
            raise FirebaseCatalogError("Firebase catalog manifest contains no maps")
        self.firebase_maps_by_key = {
            str(item["resourcePath"]): item
            for item in maps
            if isinstance(item, dict) and item.get("resourcePath")
        }
        self.firebase_inactive_keys = {
            key
            for key, item in self.firebase_maps_by_key.items()
            if item.get("status") != "active"
        }
        self.firebase_generation = str(manifest.get("generation") or "")
        self.firebase_catalog_version = int(manifest.get("catalogVersion") or 0)

    @staticmethod
    def _direction(node: ET.Element, inherited: tuple[float, float] | None = None) -> tuple[float, float]:
        if "angle" in node.attrib:
            angle = math.radians(float(node.attrib["angle"]))
            length = float(node.attrib.get("length", "1"))
            return math.cos(angle) * length, math.sin(angle) * length
        xdir = float(node.attrib.get("xdir", "0"))
        ydir = float(node.attrib.get("ydir", "0"))
        if xdir == 0 and ydir == 0 and inherited is not None:
            return inherited
        return xdir, ydir

    def _parse_map(self, path: Path, source_path: str | None = None) -> MapEntry:
        document = ET.parse(path)
        root = document.getroot()
        resource = root if local_name(root.tag) == "Resource" else next(
            node for node in root.iter() if local_name(node.tag) == "Resource"
        )
        author = resource.attrib["author"].strip()
        name = resource.attrib["name"].strip()
        version = resource.attrib["version"].strip()
        category = resource.attrib.get("category", "").strip("/")
        category_parts = [part for part in category.split("/") if part]
        for component in [author, name, version, *category_parts]:
            if not safe_resource_component(component):
                raise ValueError(f"unsafe resource component {component!r}")
        key = "/".join([author, *category_parts, f"{name}-{version}{MAP_SUFFIX}"])
        spawns: list[SpawnPoint] = []
        axes: int | None = None
        checkpoint_ids: set[int] = set()
        checkpoint_requirement: int | None = None
        time_decimals = 3
        for node in root.iter():
            if local_name(node.tag) == "Axes" and "number" in node.attrib:
                axes = int(node.attrib["number"])
                if axes < 1:
                    raise ValueError("map axes must be positive")
                break
        for node in root.iter():
            if (
                local_name(node.tag) == "Setting"
                and node.attrib.get("name", "").casefold()
                == "race_checkpoint_require_hit"
            ):
                try:
                    checkpoint_requirement = int(node.attrib.get("value", ""))
                except ValueError:
                    checkpoint_requirement = None
            if (
                local_name(node.tag) == "Setting"
                and node.attrib.get("name", "").casefold()
                == "race_time_decimals"
            ):
                try:
                    configured_decimals = int(node.attrib.get("value", ""))
                except ValueError as exc:
                    raise ValueError("RACE_TIME_DECIMALS must be an integer") from exc
                if not 0 <= configured_decimals <= 8:
                    raise ValueError("RACE_TIME_DECIMALS must be between 0 and 8")
                time_decimals = configured_decimals
            if (
                local_name(node.tag) == "Zone"
                and node.attrib.get("effect", "").casefold() == "checkpoint"
            ):
                checkpoint = next(
                    (
                        child
                        for child in node.iter()
                        if local_name(child.tag) == "Checkpoint"
                    ),
                    None,
                )
                if checkpoint is None:
                    raise ValueError("checkpoint zone has no Checkpoint element")
                try:
                    checkpoint_id = int(checkpoint.attrib["id"])
                except (KeyError, ValueError) as exc:
                    raise ValueError("checkpoint ID must be a positive integer") from exc
                if checkpoint_id <= 0:
                    raise ValueError("checkpoint ID must be a positive integer")
                checkpoint_ids.add(checkpoint_id)

        def add_spawn(node: ET.Element, inherited: tuple[float, float] | None = None) -> None:
            xdir, ydir = self._direction(node, inherited)
            spawns.append(
                SpawnPoint(float(node.attrib["x"]), float(node.attrib["y"]), xdir, ydir)
            )
            for child in node:
                if local_name(child.tag) == "Spawn":
                    add_spawn(child, (xdir, ydir))

        child_spawns = {
            id(child)
            for parent in root.iter()
            if local_name(parent.tag) == "Spawn"
            for child in parent
            if local_name(child.tag) == "Spawn"
        }
        for node in root.iter():
            if local_name(node.tag) == "Spawn" and id(node) not in child_spawns:
                add_spawn(node)
        if not spawns:
            raise ValueError("map has no spawn points")
        if source_path is None:
            source_path = path.relative_to(self.checkout).as_posix()
        firebase_metadata = self.firebase_maps_by_key.get(key, {})
        checkpoint_mode = ""
        if checkpoint_ids:
            checkpoint_mode = "unordered" if checkpoint_requirement == 1 else "ordered"
        return MapEntry(
            key=key,
            name=name,
            author=author,
            version=version,
            category=category,
            source_path=source_path,
            local_path=path,
            spawns=tuple(spawns),
            axes=axes,
            map_id=str(firebase_metadata.get("mapId", "")),
            revision_id=str(firebase_metadata.get("activeRevisionId", "")),
            storage_path=str(firebase_metadata.get("storagePath", "")),
            record_key=str(firebase_metadata.get("recordKey", "")),
            rating_key_override=str(firebase_metadata.get("ratingKey", "")),
            checkpoint_ids=tuple(sorted(checkpoint_ids)),
            checkpoint_mode=checkpoint_mode,
            time_decimals=time_decimals,
        )

    def scan(self) -> None:
        if self.firebase is not None:
            self._load_firebase_manifest()
        raw_catalog: dict[str, MapEntry] = {}
        source_to_raw_key: dict[str, str] = {}
        issues: list[str] = []
        roots = (
            ((self.checkout, False),)
            if self.firebase is not None
            else ((self.checkout, False), (self.override_dir, True))
        )
        for root, is_override in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob(f"*{MAP_SUFFIX}")):
                if ".git" in path.parts:
                    continue
                rel = path.relative_to(root).as_posix()
                try:
                    entry = self._parse_map(path, source_path=rel)
                    if (
                        entry.key in self.excluded_keys
                        or entry.key in self.firebase_inactive_keys
                    ):
                        continue
                    if entry.key in raw_catalog and not is_override:
                        issues.append(
                            f"duplicate canonical resource {entry.key}: {entry.source_path}"
                        )
                        continue
                    raw_catalog[entry.key] = entry
                    source_to_raw_key[entry.source_path] = entry.key
                except Exception as exc:
                    issues.append(f"{rel}: {exc}")
        if not raw_catalog:
            raise RuntimeError("repository contains no usable racing maps")

        reserved_keys = set(raw_catalog)
        catalog: dict[str, MapEntry] = {}
        resolved_keys: dict[str, str] = {}
        for raw_key in sorted(raw_catalog):
            raw_entry = raw_catalog[raw_key]
            entry = self._resolve_immutable_entry(
                raw_entry,
                reserved_keys,
                issues,
            )
            if entry.key in catalog:
                raise RuntimeError(
                    f"resolved map key collision at {entry.key}"
                )
            catalog[entry.key] = entry
            resolved_keys[raw_key] = entry.key
            reserved_keys.add(entry.key)

        source_to_key = {
            source: resolved_keys[raw_key]
            for source, raw_key in source_to_raw_key.items()
            if raw_key in resolved_keys
        }
        self.catalog = catalog
        self.source_to_key = source_to_key
        self.issues = issues
        self._build_public_mirror()
        LOG.info(
            "loaded %d maps from repository (%d issue(s))",
            len(catalog),
            len(issues),
        )
        for issue in issues[:20]:
            LOG.warning("map repository issue: %s", issue)

    def _stored_paths(self, key: str) -> tuple[Path, ...]:
        return tuple(
            root / key
            for root in (self.public_dir, self.cache_dir, self.revision_dir)
        )

    def _entry_conflicts_with_stored_bytes(self, entry: MapEntry) -> bool:
        source_bytes = entry.local_path.read_bytes()
        return any(
            path.is_file() and path.read_bytes() != source_bytes
            for path in self._stored_paths(entry.key)
        )

    def _matching_revision(
        self,
        entry: MapEntry,
        reserved_keys: set[str],
    ) -> MapEntry | None:
        parent = self.revision_dir.joinpath(*entry.key.split("/")[:-1])
        if not parent.is_dir():
            return None
        source_bytes = entry.local_path.read_bytes()
        for path in sorted(parent.iterdir()):
            if not path.is_file() or not path.name.endswith(MAP_SUFFIX):
                continue
            try:
                candidate = self._parse_map(path, source_path=entry.source_path)
            except Exception:
                continue
            if candidate.key in reserved_keys:
                continue
            if (
                candidate.author.casefold() != entry.author.casefold()
                or candidate.category.casefold() != entry.category.casefold()
                or candidate.name.casefold() != entry.name.casefold()
            ):
                continue
            if (
                rewrite_map_resource_version(source_bytes, candidate.version)
                == path.read_bytes()
            ):
                return candidate
        return None

    def _key_exists(self, key: str, reserved_keys: set[str]) -> bool:
        if key in reserved_keys:
            return True
        return any(
            (root / key).exists()
            for root in (
                self.public_dir,
                self.cache_dir,
                self.override_dir,
                self.revision_dir,
            )
        )

    def _resolve_immutable_entry(
        self,
        entry: MapEntry,
        reserved_keys: set[str],
        issues: list[str],
    ) -> MapEntry:
        if not self._entry_conflicts_with_stored_bytes(entry):
            return entry

        if self.firebase is not None:
            raise FirebaseCatalogError(
                f"Firebase reused immutable resource path {entry.key} with different bytes"
            )

        existing = self._matching_revision(entry, reserved_keys)
        if existing is not None:
            issues.append(
                f"same-version content change for {entry.key}; "
                f"reusing immutable revision {existing.key}"
            )
            return existing

        version = bump_resource_version(entry.version)
        category_parts = [part for part in entry.category.split("/") if part]
        while True:
            key = "/".join(
                [
                    entry.author,
                    *category_parts,
                    f"{entry.name}-{version}{MAP_SUFFIX}",
                ]
            )
            if not self._key_exists(key, reserved_keys):
                break
            version = bump_resource_version(version)

        destination = self.revision_dir / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = rewrite_map_resource_version(entry.local_path.read_bytes(), version)
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.tmp"
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, destination)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

        resolved = self._parse_map(destination, source_path=entry.source_path)
        issues.append(
            f"same-version content change for {entry.key}; "
            f"published repository bytes as {resolved.key}"
        )
        return resolved

    def create_size_revision(self, entry: MapEntry, size_factor: float) -> MapEntry:
        """Create a persistent versioned override with a map-local SIZE_FACTOR."""
        document = ET.parse(entry.local_path)
        root = document.getroot()
        resource = root if local_name(root.tag) == "Resource" else next(
            node for node in root.iter() if local_name(node.tag) == "Resource"
        )
        map_node = next(node for node in resource.iter() if local_name(node.tag) == "Map")

        version = bump_resource_version(entry.version)
        while True:
            key = "/".join(
                [
                    entry.author,
                    *([part for part in entry.category.split("/") if part]),
                    f"{entry.name}-{version}{MAP_SUFFIX}",
                ]
            )
            destination = self.override_dir / key
            if key not in self.catalog and not destination.exists():
                break
            version = bump_resource_version(version)

        resource.set("version", version)
        settings = next(
            (child for child in map_node if local_name(child.tag) == "Settings"),
            None,
        )
        namespace = map_node.tag.split("}", 1)[0] + "}" if "}" in map_node.tag else ""
        if settings is None:
            settings = ET.Element(namespace + "Settings")
            settings.text = "\n"
            settings.tail = map_node.text or "\n"
            map_node.insert(0, settings)
        size_settings = [
            child
            for child in settings
            if local_name(child.tag) == "Setting"
            and child.attrib.get("name", "").casefold() == "size_factor"
        ]
        if not size_settings:
            setting = ET.Element(namespace + "Setting")
            setting.set("name", "SIZE_FACTOR")
            setting.tail = settings.text or "\n"
            settings.append(setting)
            size_settings.append(setting)
        for setting in size_settings:
            setting.set("value", format_size_factor(size_factor))

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        raw = entry.local_path.read_bytes()
        encoding_match = re.search(br"<\?xml[^>]*encoding=[\"']([^\"']+)", raw[:512])
        encoding = encoding_match.group(1).decode("ascii") if encoding_match else "utf-8"
        doctype_match = re.search(br"<!DOCTYPE[^>]+>", raw[:4096], re.IGNORECASE)
        document.write(temporary, encoding=encoding, xml_declaration=True)
        if doctype_match:
            serialized = temporary.read_bytes()
            declaration_end = serialized.find(b"?>")
            insert_at = declaration_end + 2 if declaration_end >= 0 else 0
            serialized = (
                serialized[:insert_at]
                + b"\n"
                + doctype_match.group(0)
                + serialized[insert_at:]
            )
            temporary.write_bytes(serialized)
        os.replace(temporary, destination)
        revision = self._parse_map(destination, source_path=key)
        if self.firebase is not None:
            if not entry.map_id or not entry.revision_id:
                raise FirebaseCatalogError(
                    "active map is missing Firebase map/revision identity"
                )
            assert self.firebase is not None
            published = self.firebase.publish_size_revision(
                map_id=entry.map_id,
                expected_revision_id=entry.revision_id,
                data=destination.read_bytes(),
                identity={
                    "authorName": revision.author,
                    "category": revision.category,
                    "mapName": revision.name,
                    "mapVersion": revision.version,
                },
                size_factor=size_factor,
            )
            # The catalog manifest builder runs asynchronously after the
            # Firestore commit. Loading the full maps collection immediately
            # used hundreds of document reads and let the leader advance before
            # a follower could possibly obtain the same revision. Poll only
            # the single invalidation document, then consume the first compact
            # manifest that contains the published immutable resource.
            deadline = time.monotonic() + self.firebase_publish_wait_seconds
            seen_signatures: set[tuple[int, str, str]] = set()
            while True:
                state = self.firebase.get_catalog_state()
                signature = (
                    int(state.get("catalogVersion") or 0),
                    str(state.get("generation") or ""),
                    str(state.get("serverManifestSha256") or ""),
                )
                if all(signature) and signature not in seen_signatures:
                    seen_signatures.add(signature)
                    if (
                        signature[0] != self.firebase_catalog_version
                        or signature[1] != self.firebase_generation
                    ):
                        self.sync(catalog_state=state)
                        selected = self.catalog.get(revision.key)
                        if (
                            selected is not None
                            and selected.revision_id == published["revisionId"]
                        ):
                            return selected
                if time.monotonic() >= deadline:
                    raise FirebaseCatalogError(
                        f"published size revision {revision.key} did not reach "
                        "the server catalog before the timeout"
                    )
                time.sleep(1.0)
        return revision

    def set_map_status(self, key: str, status: str, reason: str) -> None:
        """Publish active/inactive status when Firebase is authoritative."""
        if self.firebase is None:
            return
        metadata = self.firebase_maps_by_key.get(key)
        if not metadata or not metadata.get("mapId"):
            raise FirebaseCatalogError(f"map {key} has no Firebase catalog identity")
        self.firebase.set_map_status(str(metadata["mapId"]), status, reason)
        self.sync(force_firestore=True)

    def list_map_reviews(self) -> list[dict]:
        if self.firebase is None:
            return []
        return self.firebase.list_map_reviews()

    def submit_map_review(self, key: str, reason: str) -> dict:
        if self.firebase is None:
            raise FirebaseCatalogError("map review requires the Firebase catalog")
        metadata = self.firebase_maps_by_key.get(key)
        if not metadata or not metadata.get("mapId"):
            raise FirebaseCatalogError(f"map {key} has no Firebase catalog identity")
        review = self.firebase.submit_map_review(str(metadata["mapId"]), reason)
        self.sync(force_firestore=True)
        return review

    def cancel_map_review(self, review_id: str, reason: str) -> dict:
        if self.firebase is None:
            raise FirebaseCatalogError("map review requires the Firebase catalog")
        review = self.firebase.cancel_map_review(review_id, reason)
        self.sync(force_firestore=True)
        return review

    @staticmethod
    def map_size_factor(entry: MapEntry) -> float | None:
        document = ET.parse(entry.local_path)
        result = None
        for node in document.getroot().iter():
            if (
                local_name(node.tag) == "Setting"
                and node.attrib.get("name", "").casefold() == "size_factor"
            ):
                result = float(node.attrib["value"])
        return result

    def _build_public_mirror(self) -> None:
        self.public_dir.mkdir(parents=True, exist_ok=True)
        for entry in self.catalog.values():
            destination = self.public_dir / entry.key
            install_immutable_file(entry.local_path, destination)
        if self.dtd_source_dir.is_dir():
            for dtd in self.dtd_source_dir.rglob("*.dtd"):
                destination = self.public_dir / dtd.name
                if not destination.exists():
                    shutil.copy2(dtd, destination)
        # sty.dtd is commonly supplied by the resource repository rather than
        # installed with the game data. Preserve any copy the game has already
        # cached and expose it from the mirror root for clients.
        for dtd in self.cache_dir.glob("*.dtd"):
            destination = self.public_dir / dtd.name
            if not destination.exists() or destination.stat().st_mtime_ns != dtd.stat().st_mtime_ns:
                shutil.copy2(dtd, destination)

    def cache_for_server(self, entry: MapEntry) -> None:
        destination = self.cache_dir / entry.key
        install_immutable_file(entry.local_path, destination)
        for dtd in self.public_dir.glob("*.dtd"):
            cached_dtd = self.cache_dir / dtd.name
            if not cached_dtd.exists():
                shutil.copy2(dtd, cached_dtd)

    def install_federated_resource(
        self,
        key: str,
        data: bytes,
        expected_sha256: str,
    ) -> MapEntry:
        """Validate and immutably install one leader-selected map resource."""

        if (
            not isinstance(data, bytes)
            or not data
            or len(data) > MAX_FEDERATION_MAP_BYTES
        ):
            raise ValueError("invalid federated map size")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("invalid federated map digest")
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError("federated map digest mismatch")

        temporary_root = self.revision_dir / ".federation-incoming"
        temporary_root.mkdir(parents=True, exist_ok=True)
        temporary = temporary_root / f"{actual_sha256}.{os.getpid()}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            entry = self._parse_map(temporary, source_path=key)
            if entry.key != key:
                raise ValueError(
                    f"federated map identity mismatch: expected {key}, got {entry.key}"
                )
            install_immutable_file(temporary, self.public_dir / key)
            install_immutable_file(temporary, self.cache_dir / key)
            for dtd in self.public_dir.glob("*.dtd"):
                cached_dtd = self.cache_dir / dtd.name
                if not cached_dtd.exists():
                    shutil.copy2(dtd, cached_dtd)
            return self._parse_external(self.cache_dir / key, key)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def fetch_federated_resource(
        self,
        base_url: str,
        key: str,
        expected_sha256: str,
        timeout_seconds: float = 10.0,
    ) -> MapEntry:
        """Fetch one authenticated leader selection over the private overlay."""

        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("invalid federation leader resource URL")
        quoted_key = "/".join(
            urllib.parse.quote(component, safe="-._~")
            for component in key.split("/")
        )
        url = urllib.parse.urljoin(base_url.rstrip("/") + "/", quoted_key)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/xml, text/xml, */*"},
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            data = response.read(MAX_FEDERATION_MAP_BYTES + 1)
        return self.install_federated_resource(key, data, expected_sha256)

    def find_by_spec(self, spec: str) -> MapEntry | None:
        key = spec.split("(", 1)[0]
        if key in self.catalog:
            return self.catalog[key]
        cached = self.cache_dir / key
        if cached.is_file():
            try:
                return self._parse_external(cached, key)
            except Exception as exc:
                LOG.warning("unable to parse active external map %s: %s", key, exc)
        published = self.public_dir / key
        if published.is_file():
            try:
                return self._parse_external(published, key)
            except Exception as exc:
                LOG.warning("unable to parse published map %s: %s", key, exc)
        source_key = self.source_to_key.get(key)
        if source_key:
            return self.catalog[source_key]
        return None

    def _parse_external(self, path: Path, key: str) -> MapEntry:
        entry = self._parse_map(path, source_path=key)
        return dataclasses.replace(entry, key=key, source_path=key, local_path=path)

    def display_name(self, entry: MapEntry) -> str:
        """Return a deterministic selector for maps that share a name."""
        siblings = sorted(
            (
                candidate
                for candidate in self.catalog.values()
                if candidate.name.casefold() == entry.name.casefold()
            ),
            key=lambda candidate: (
                candidate.author.casefold(),
                candidate.version.casefold(),
                candidate.key.casefold(),
            ),
        )
        if len(siblings) < 2:
            return entry.name
        for number, candidate in enumerate(siblings, 1):
            if candidate.key == entry.key:
                return f"{entry.name} {number}"
        return entry.name

    def search(self, query: str) -> list[MapEntry]:
        query_fold = query.strip().casefold()
        normalized = normalized_map_name(query)
        exact: list[MapEntry] = []
        partial: list[MapEntry] = []
        for entry in self.catalog.values():
            names = {
                entry.name.casefold(),
                self.display_name(entry).casefold(),
                entry.key.casefold(),
                Path(entry.key).name[: -len(MAP_SUFFIX)].casefold(),
            }
            normalized_names = {normalized_map_name(item) for item in names}
            if query_fold in names or (normalized and normalized in normalized_names):
                exact.append(entry)
            elif query_fold and any(query_fold in item for item in names):
                partial.append(entry)
            elif normalized and any(normalized in item for item in normalized_names):
                partial.append(entry)
        return sorted(
            exact or partial,
            key=lambda item: (
                self.display_name(item).casefold(),
                item.author.casefold(),
                item.key.casefold(),
            ),
        )


class QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        LOG.debug("map mirror: " + fmt, *args)

    def list_directory(self, path):
        self.send_error(404, "Directory listing disabled")
        return None

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=300")
        super().end_headers()


class MirrorServer:
    def __init__(self, root: Path, bind: str, port: int):
        handler = functools.partial(QuietStaticHandler, directory=str(root))
        self.server = http.server.ThreadingHTTPServer((bind, port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="map-mirror", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class FederationControllerProtocol(asyncio.DatagramProtocol):
    """Deliver sidecar-authenticated control events to the controller."""

    def __init__(self, controller: "TronnerRacing"):
        self.controller = controller

    def datagram_received(self, data: bytes, address) -> None:
        if not data or len(data) > MAX_FEDERATION_CONTROLLER_EVENT_BYTES:
            LOG.warning("dropping invalid local federation event size")
            return
        task = asyncio.create_task(
            self.controller.handle_federation_datagram(data),
            name="federation-controller-event",
        )
        self.controller._federation_event_tasks.add(task)
        task.add_done_callback(self.controller._federation_event_tasks.discard)

    def error_received(self, exc: Exception) -> None:
        LOG.warning("local federation socket error: %s", exc)


class TronnerRacing:
    def __init__(self, config: dict):
        self.config = config
        self.started_at_epoch = time.time()
        self.hot_commands = HotCommandRegistry(
            Path(
                config.get(
                    "hot_commands_dir",
                    Path(__file__).resolve().with_name("hot_commands"),
                )
            )
        )
        configured_encoding = config.get("game_text_encoding", "auto")
        fallback_encoding = config.get(
            "game_text_encoding_fallback",
            DEFAULT_GAME_TEXT_ENCODING,
        )
        self.game_text_encoding_auto = (
            str(configured_encoding).strip().casefold() == "auto"
        )
        if self.game_text_encoding_auto:
            self.game_text_encoding = detect_game_text_encoding(
                Path(config.get("ladderlog", "")),
                fallback_encoding,
            )
        else:
            self.game_text_encoding = canonical_game_text_encoding(
                configured_encoding,
                fallback_encoding,
            )
        self.sink = CommandSink(
            Path(config["console_input"]),
            self.game_text_encoding,
        )
        self.repository = MapRepository(config)
        self.store = StateStore(Path(config["database"]))
        saved_start_preferences = self.store.get_json("start_preferences", {})
        self.start_preferences: dict[str, str] = {
            str(identity_key): str(mode).casefold()
            for identity_key, mode in (
                saved_start_preferences.items()
                if isinstance(saved_start_preferences, dict)
                else ()
            )
            if str(mode).casefold()
            in {"brake", "immediate", "countdown", "respawn"}
        }
        saved_server_tag_preferences = self.store.get_json(
            "display_server_tag_preferences", {}
        )
        self.display_server_tag_preferences: dict[str, bool] = {
            str(identity_key): enabled
            for identity_key, enabled in (
                saved_server_tag_preferences.items()
                if isinstance(saved_server_tag_preferences, dict)
                else ()
            )
            if isinstance(enabled, bool)
        }
        self.spawn_preferences_path = Path(
            config.get(
                "spawn_preferences_file",
                "/var/lib/tronner-racing/spawn_preferences.json",
            )
        )
        loaded_preferences = load_json_object(self.spawn_preferences_path).get(
            "preferences", {}
        )
        self.spawn_preferences: dict[str, dict[str, int]] = (
            loaded_preferences if isinstance(loaded_preferences, dict) else {}
        )
        saved_preference_versions = self.store.get_json(
            "federation_preference_versions", {}
        )
        self.federation_preference_versions: dict[str, list[object]] = (
            saved_preference_versions
            if isinstance(saved_preference_versions, dict)
            else {}
        )
        self.federation_preference_pending: dict[str, dict[str, object]] = {}
        self._federation_preference_snapshot_cache: dict[
            str, tuple[float, list[dict[str, object]]]
        ] = {}
        self.command_windows: dict[int, collections.deque[float]] = {}
        self.command_warning_times: dict[int, float] = {}
        saved_helpful_cycle = self.store.get_json("helpful_message_cycle", {})
        self.helpful_message_cycle: dict = (
            saved_helpful_cycle if isinstance(saved_helpful_cycle, dict) else {}
        )
        saved_helpful_round_token = self.store.get_json(
            "helpful_message_round_token", None
        )
        self.helpful_message_round_token: str | None = (
            str(saved_helpful_round_token) if saved_helpful_round_token else None
        )
        self.helpful_message_round_generation = 0
        self.helpful_message_announced = False
        self._helpful_message_task: asyncio.Task | None = None
        self._server_options_last: str | None = None
        self.report_last_sent: dict[str, float] = {}
        saved_report_epochs = self.store.get_json("report_success_epochs", [])
        self.report_success_epochs: collections.deque[float] = collections.deque()
        if isinstance(saved_report_epochs, list):
            for value in saved_report_epochs:
                with contextlib.suppress(TypeError, ValueError):
                    self.report_success_epochs.append(float(value))
        self.excluded_map_keys: set[str] = set(
            self.store.get_json("excluded_map_keys", [])
        )
        loaded_exclusion_reasons = self.store.get_json(
            "excluded_map_reasons", {}
        )
        self.excluded_map_reasons: dict[str, str] = (
            {
                str(key): str(reason)
                for key, reason in loaded_exclusion_reasons.items()
                if str(key) in self.excluded_map_keys and str(reason).strip()
            }
            if isinstance(loaded_exclusion_reasons, dict)
            else {}
        )
        self.repository.excluded_keys = self.excluded_map_keys
        self.catalog_state_signature: tuple[int, str, str] | None = None
        self.catalog_ack_signature: tuple[int, str, str] | None = None
        self.next_activity_probe_monotonic = 0.0
        self.players: dict[str, Player] = {}
        self.aliases: dict[str, Player] = {}
        self.rotation: collections.deque[str] = collections.deque(
            self.store.get_json("rotation", [])
        )
        self.queue: collections.deque[str] = collections.deque(
            self.store.get_json("queue", [])
        )
        self.cycle_played: set[str] = set(self.store.get_json("cycle_played", []))
        # Upgrade state written by versions that only persisted the remaining
        # rotation.  The active repository map has necessarily been consumed.
        if not self.cycle_played:
            saved_current = self.store.get_json("current_key", None)
            if saved_current:
                self.cycle_played.add(saved_current)
        self.current: MapEntry | None = None
        self.current_spec: str | None = None
        self.current_size_factor: float | None = None
        self.restoring_saved_map = False
        self.deadline_epoch: float | None = self.store.get_json("deadline_epoch", None)
        self.round_started_epoch: float | None = self.store.get_json(
            "round_started_epoch", None
        )
        self.extend_votes: set[str] = set()
        self.skip_votes: set[str] = set()
        self.extend_vote_generation = 0
        self.skip_vote_generation = 0
        self.round_active = False
        self.round_started_map_key: str | None = self.store.get_json(
            "round_started_map_key", None
        )
        self.transitioning = bool(self.store.get_json("transitioning", False))
        self.transition_target_key: str | None = self.store.get_json(
            "transition_target_key", None
        )
        if self.transitioning and not self.transition_target_key:
            self.transition_target_key = self.store.get_json("current_key", None)
        self.transition_map_confirmed = False
        self.transition_observed_key: str | None = None
        self.transition_started_epoch: float | None = None
        self.transition_round_started_pending = False
        self.final_countdown_active = bool(
            self.store.get_json("final_countdown_active", False)
        )
        self.final_countdown_end_epoch: float | None = self.store.get_json(
            "final_countdown_end_epoch", None
        )
        self.final_countdown_map_key: str | None = self.store.get_json(
            "final_countdown_map_key", None
        )
        self.final_countdown_announcement: str | None = None
        self.finalists: set[int] = set()
        self.finishes_in_progress: set[int] = set()
        reload_state = self.store.get_json("controller_reload", {})
        self.controller_reload_state: dict = (
            reload_state if isinstance(reload_state, dict) else {}
        )
        self.respawns_paused = bool(self.controller_reload_state.get("pending"))
        self.controller_reload_draining = False
        self._controller_reload_task: asyncio.Task | None = None
        self.last_game_time: float | None = None
        self.last_game_monotonic: float | None = None
        self.respawn_tasks: dict[int, asyncio.Task] = {}
        self.freeze_tasks: dict[int, asyncio.Task] = {}
        self.center_clear_tasks: dict[int, asyncio.Task] = {}
        self.replay_captures: dict[str, ReplayCapture] = {}
        self.active_replay_tokens: dict[int, str] = {}
        self.replay_settings_assemblies: dict[str, ReplaySettingsAssembly] = {}
        self.active_replay_settings_identifier: str | None = None
        # online_players.txt is rewritten in place by the game server.  A read
        # can therefore briefly omit a player who is actually connected.  Do
        # not let one incomplete snapshot override authoritative ladderlog
        # lifecycle events.
        self.online_snapshot_misses: dict[int, int] = {}
        self.last_time_left_minute: int | None = None
        self.map_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.mirror: MirrorServer | None = None
        self._display_task: asyncio.Task | None = None
        self._transition_watchdog_task: asyncio.Task | None = None
        federation = config.get("federation", {})
        if not isinstance(federation, dict):
            raise ValueError("federation configuration must be an object")
        self.federation_role = str(federation.get("role", "off")).casefold()
        if self.federation_role not in {"off", "leader", "follower"}:
            raise ValueError("federation role must be off, leader, or follower")
        self.federation_local_server_id = clean_console_text(
            federation.get("local_server_id", "")
        )
        self.federation_remote_server_id = clean_console_text(
            federation.get(
                "remote_server_id",
                federation.get("leader_server_id", ""),
            )
        )
        self.federation_remote_region = clean_console_text(
            federation.get(
                "remote_region_label",
                federation.get("leader_region_label", "REMOTE"),
            )
        )[:16] or "REMOTE"
        configured_remote_servers = federation.get("remote_servers")
        if configured_remote_servers is None:
            configured_remote_servers = (
                {self.federation_remote_server_id: self.federation_remote_region}
                if self.federation_remote_server_id
                else {}
            )
        if not isinstance(configured_remote_servers, dict):
            raise ValueError("federation remote_servers must be an object")
        self.federation_remote_regions: dict[str, str] = {}
        for raw_server_id, raw_region in configured_remote_servers.items():
            server_id = clean_console_text(raw_server_id)
            region = clean_console_text(raw_region)[:16]
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", server_id)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}", region)
                or server_id == self.federation_local_server_id
            ):
                raise ValueError("invalid federation remote server")
            self.federation_remote_regions[server_id] = region
        self.federation_leader_server_id = clean_console_text(
            federation.get(
                "leader_server_id",
                (
                    self.federation_local_server_id
                    if self.federation_role == "leader"
                    else self.federation_remote_server_id
                ),
            )
        )
        if self.federation_role != "off":
            if not self.federation_remote_regions:
                raise ValueError("federation requires at least one remote server")
            if self.federation_role == "leader":
                if self.federation_leader_server_id != self.federation_local_server_id:
                    raise ValueError("federation leader identity mismatch")
            elif self.federation_leader_server_id not in self.federation_remote_regions:
                raise ValueError("federation follower must include its leader")
        socket_value = str(federation.get("controller_import_socket", "")).strip()
        self.federation_import_socket = Path(socket_value) if socket_value else None
        publish_socket_value = str(
            federation.get("controller_publish_socket", "")
        ).strip()
        self.federation_publish_socket = (
            Path(publish_socket_value) if publish_socket_value else None
        )
        try:
            self.federation_map_prepare_lead_seconds = float(
                federation.get("map_prepare_lead_seconds", 3.0)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid federation map prepare lead time") from exc
        if not 1.0 <= self.federation_map_prepare_lead_seconds <= 30.0:
            raise ValueError("federation map prepare lead time must be 1..30 seconds")
        if self.federation_role == "follower":
            if not self.federation_remote_server_id:
                raise ValueError("federation follower requires leader_server_id")
            if self.federation_import_socket is None:
                raise ValueError(
                    "federation follower requires controller_import_socket"
                )
        if self.federation_role == "leader" and self.federation_publish_socket is None:
            raise ValueError(
                "federation leader requires controller_publish_socket"
            )
        if (
            self.federation_import_socket is not None
            and not self.federation_remote_server_id
        ):
            raise ValueError(
                "federation controller import requires remote_server_id"
            )
        if self.federation_role != "off" and not self.federation_local_server_id:
            raise ValueError("federation requires local_server_id")
        self._seed_federation_preference_versions()
        self.federation_sync_chat = bool(federation.get("sync_chat", True))
        self.federation_sync_presence = bool(
            federation.get("sync_presence", True)
        )
        self.federation_sync_maps = bool(federation.get("sync_maps", True))
        self.federation_round_sync_enabled = bool(
            federation.get("round_sync_enabled", True)
        )
        self.federation_round_sync_release_lead_seconds = max(
            0.1,
            min(
                2.0,
                float(federation.get("round_sync_release_lead_seconds", 0.5)),
            ),
        )
        self.federation_round_sync_timeout_seconds = max(
            5.0,
            min(
                60.0,
                float(federation.get("round_sync_timeout_seconds", 30.0)),
            ),
        )
        self.federation_local_round_ready_key = ""
        self.federation_remote_round_ready_key = ""
        self.federation_local_round_ready_at = 0.0
        self.federation_remote_round_ready_at = 0.0
        self.federation_round_last_release_at = 0.0
        self.federation_round_release_key = ""
        self.federation_round_release_at = 0.0
        self.federation_remote_players: dict[str, dict[str, object]] = {}
        self.federation_remote_round_ready: dict[str, tuple[str, float]] = {}
        self.federation_remote_maps: dict[str, str] = {}
        self.federation_remote_rounds: dict[str, tuple[bool, str, str]] = {}
        self.federation_remote_map_key = ""
        self.federation_remote_round_active = False
        self.federation_remote_round_map_key = ""
        self.federation_remote_round_started_at = ""
        self.federation_remote_round_adopted_key = ""
        self.federation_command_players: dict[str, Player] = {}
        self.federation_finalists: set[str] = set()
        self.federation_snapshot_received = False
        self.federation_snapshots_received: set[str] = set()
        self.federation_last_received_monotonic = 0.0
        self.federation_last_sent_ns = 0
        self.federation_last_state_sent_ns: dict[str, int] = {}
        self.federation_peer_last_received_monotonic: dict[str, float] = {}
        self.federation_last_boot_id = ""
        self._federation_transport: asyncio.DatagramTransport | None = None
        self._federation_publish_transport: socket.socket | None = None
        self.federation_prepared_map_key: str | None = None
        self.federation_prepared_map_activate_ns = 0
        self.federation_prepared_map_sha256 = ""
        self.federation_map_prepare_lock = asyncio.Lock()
        self.federation_leader_current_map_key = ""
        self.federation_leader_next_map_key = ""
        self._federation_server_state_last_publish_monotonic = 0.0
        self.federation_leader_resource_base_url = str(
            federation.get("leader_resource_base_url", "")
        ).strip()
        self.federation_resource_timeout_seconds = max(
            1.0,
            min(
                30.0,
                float(federation.get("resource_timeout_seconds", 10.0)),
            ),
        )
        if self.federation_leader_resource_base_url:
            parsed_leader_resources = urllib.parse.urlsplit(
                self.federation_leader_resource_base_url
            )
            if (
                parsed_leader_resources.scheme not in {"http", "https"}
                or not parsed_leader_resources.netloc
            ):
                raise ValueError("invalid federation leader_resource_base_url")
        self.federation_catalog_refresh_after_monotonic = 0.0
        self._federation_event_tasks: set[asyncio.Task] = set()
        self._federation_record_wakeup = asyncio.Event()
        self.federation_peer_timeout_seconds = max(
            4.0,
            min(30.0, float(federation.get("peer_timeout_seconds", 7.0))),
        )
        live_config = config.get("live_dashboard", {})
        if not isinstance(live_config, dict):
            live_config = {}
        self.server_console_path = Path(
            live_config.get(
                "console_log_path",
                "/var/lib/armagetronad/consolelog.txt",
            )
        )
        self.server_console_entries: collections.deque[dict[str, object]] = (
            collections.deque(maxlen=SERVER_CONSOLE_HISTORY_LINES)
        )
        self.server_console_sequence = 0
        self.server_console_last_published_sequence = 0
        self.server_console_stream_until_monotonic = 0.0
        self.server_console_available = False
        self.live_dashboard: FirebaseLiveDashboardPublisher | None = None
        self.live_dashboard_chat: FirebaseLiveDashboardPublisher | None = None
        if (
            isinstance(live_config, dict)
            and live_config.get("enabled") is True
            and self.federation_leader
            and self.repository.firebase is not None
        ):
            self.live_dashboard = FirebaseLiveDashboardPublisher(
                self.repository.firebase,
                str(live_config.get("database_url", "")),
                self.store,
            )
            self.live_dashboard_chat = self.live_dashboard
        elif (
            isinstance(live_config, dict)
            and live_config.get("chat_enabled") is True
            and self.repository.firebase is not None
        ):
            self.live_dashboard_chat = FirebaseLiveDashboardPublisher(
                self.repository.firebase,
                str(live_config.get("database_url", "")),
                self.store,
            )

    @property
    def federation_follower(self) -> bool:
        return getattr(self, "federation_role", "off") == "follower"

    @property
    def federation_leader(self) -> bool:
        return getattr(self, "federation_role", "off") == "leader"

    async def _start_federation_import(self) -> None:
        if self.federation_import_socket is None:
            return
        path = self.federation_import_socket
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: FederationControllerProtocol(self),
            family=socket.AF_UNIX,
            local_addr=str(path),
        )
        self._federation_transport = transport
        os.chmod(path, 0o660)
        LOG.info(
            "federation import ready: remotes=%s socket=%s",
            ",".join(sorted(self.federation_remote_regions)),
            path,
        )

    async def _publish_federation_control(
        self,
        kind: str,
        payload: dict[str, object],
    ) -> bool:
        publish_socket = getattr(self, "federation_publish_socket", None)
        if self.federation_role == "off" or publish_socket is None:
            return False
        data = json.dumps(
            {"kind": kind, "payload": payload},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(data) > MAX_FEDERATION_CONTROLLER_EVENT_BYTES:
            LOG.error("local federation publish event is too large")
            return False
        if self._federation_publish_transport is None:
            self._federation_publish_transport = socket.socket(
                socket.AF_UNIX, socket.SOCK_DGRAM
            )
            self._federation_publish_transport.setblocking(False)
        try:
            await asyncio.get_running_loop().sock_sendto(
                self._federation_publish_transport,
                data,
                str(publish_socket),
            )
            return True
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            LOG.exception("unable to publish local federation control event")
            return False

    async def _prepare_federated_leader_map(
        self,
        entry: MapEntry,
        size_factor: float,
    ) -> None:
        if not self.federation_leader:
            return
        activation_at_ns = time.time_ns() + int(
            self.federation_map_prepare_lead_seconds * 1_000_000_000
        )
        transition_id = f"{activation_at_ns:x}-{entry.key.encode('utf-8').hex()[:24]}"
        map_sha256 = hashlib.sha256(entry.local_path.read_bytes()).hexdigest()
        payload = {
            "transition_id": transition_id,
            "map_key": entry.key,
            "map_sha256": map_sha256,
            "size_factor": size_factor,
            "activate_at_ns": activation_at_ns,
        }
        published = False
        # Map preparation is idempotent and immutable. Three compact copies
        # avoid a second catalog synchronization protocol for UDP loss.
        for attempt in range(3):
            published = await self._publish_federation_control(
                "map_prepare", payload
            ) or published
            if attempt < 2:
                await asyncio.sleep(0.03)
        if not published:
            # Observed CURRENT_MAP remains the fail-safe synchronization path;
            # do not delay the live leader if its local sidecar is unavailable.
            return
        LOG.info(
            "armed federation map transition: map=%s activate_at_ns=%d",
            entry.key,
            activation_at_ns,
        )
        delay = (activation_at_ns - time.time_ns()) / 1_000_000_000
        if delay > 0:
            await asyncio.sleep(delay)

    @staticmethod
    def _federation_text(value: object, maximum: int) -> str:
        return clean_console_text(value).replace("\x00", "")[:maximum]

    def _federation_player_payload(
        self,
        payload: object,
    ) -> tuple[str, dict[str, object]] | None:
        if not isinstance(payload, dict):
            return None
        player_id = str(payload.get("player_id", ""))
        if (
            not player_id
            or len(player_id) > MAX_FEDERATION_PLAYER_NAME_CHARACTERS
            or any(character.isspace() or ord(character) < 32 for character in player_id)
        ):
            return None
        display_name = self._federation_text(
            payload.get("display_name", player_id),
            MAX_FEDERATION_PLAYER_NAME_CHARACTERS,
        ) or player_id
        item: dict[str, object] = {
            "player_id": player_id,
            "display_name": display_name,
            "colored_name": self._federation_text(
                payload.get("colored_name", ""),
                MAX_FEDERATION_PLAYER_NAME_CHARACTERS * 2,
            ),
            "authenticated_name": self._federation_text(
                payload.get("authenticated_name", ""),
                MAX_FEDERATION_PLAYER_NAME_CHARACTERS,
            ),
            "active": bool(payload.get("active", False)),
            "alive": bool(payload.get("alive", False)),
            "connected": bool(payload.get("connected", True)),
        }
        try:
            ping = float(payload.get("ping", 0.0))
        except (TypeError, ValueError):
            ping = 0.0
        item["ping"] = ping if math.isfinite(ping) and 0 <= ping <= 30 else 0.0
        return player_id.casefold(), item

    def _federation_command_player(
        self,
        key: str,
        item: dict[str, object],
        server_id: str | None = None,
    ) -> Player:
        effective_server_id = server_id or self.federation_remote_server_id
        remote_key = f"{effective_server_id}\0{key}"
        player = self.federation_command_players.get(remote_key)
        if player is None:
            player = Player(
                str(item["player_id"]),
                str(item["display_name"]),
                federation_server_id=(
                    effective_server_id
                ),
            )
            self.federation_command_players[remote_key] = player
        player.log_name = str(item["player_id"])
        player.display_name = str(item["display_name"])
        player.colored_name = str(item.get("colored_name", "")) or None
        player.auth_name = str(item.get("authenticated_name", "")) or None
        player.connected = bool(item.get("connected", True))
        player.active = bool(item.get("active", False))
        player.alive = bool(item.get("alive", False))
        player.respawn_enabled = player.active
        return player

    async def handle_federation_datagram(self, data: bytes) -> None:
        try:
            event = json.loads(data.decode("utf-8"))
            if not isinstance(event, dict) or event.get("version") not in {1, 2}:
                raise ValueError("invalid envelope")
            server_id = str(event.get("server_id", ""))
            if server_id not in self.federation_remote_regions:
                raise ValueError("unexpected remote server")
            sent_ns = int(event.get("sent_ns", 0))
            if sent_ns <= 0:
                raise ValueError("invalid timestamp")
            kind = str(event.get("kind", ""))
            payload = event.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("invalid payload")
            # The authenticated network receiver already rejects replays.  A
            # global timestamp boundary is unsafe, though: UDP can deliver a
            # newer heartbeat before an older command.  Only order state that
            # can actually revert something, and keep independent state
            # domains independent.  Edge-triggered messages are all delivered.
            player_action = (
                str(payload.get("action", "")).casefold()
                if kind == "player_event"
                else ""
            )
            state_bucket = {
                "player_snapshot": "presence",
                "map_prepare": "map",
                "map_commit": "map",
                "countdown_state": "countdown",
                "round_sync": "round_sync",
            }.get(kind)
            if (
                kind == "controller_message"
                and payload.get("scope") == "server_state"
            ):
                state_bucket = "server_state"
            if kind == "player_event":
                state_bucket = (
                    "round"
                    if player_action in {
                        "round_started",
                        "round_finished",
                        "round_ended",
                        "new_round",
                    }
                    else "presence"
                )
            if state_bucket:
                bucket_key = f"{server_id}:{state_bucket}"
                last_sent_ns = self.federation_last_state_sent_ns.get(
                    bucket_key, 0
                )
                if sent_ns < last_sent_ns:
                    return
                self.federation_last_state_sent_ns[bucket_key] = sent_ns
            self.federation_last_sent_ns = max(
                self.federation_last_sent_ns, sent_ns
            )
            self.federation_last_received_monotonic = time.monotonic()
            self.federation_peer_last_received_monotonic[server_id] = time.monotonic()
            self.federation_last_boot_id = str(event.get("boot_id", ""))
            if kind == "chat":
                await self._handle_federation_chat(server_id, payload)
            elif kind == "command":
                await self._handle_federation_command(server_id, payload)
            elif kind == "controller_message":
                scope = str(payload.get("scope", ""))
                if scope.startswith("federation_catalog_exclusion_"):
                    await self._handle_federation_catalog_exclusion_message(
                        server_id, payload
                    )
                elif scope.startswith("federation_preference_"):
                    await self._handle_federation_preference_message(
                        server_id, payload
                    )
                elif server_id == self.federation_leader_server_id:
                    await self._handle_federation_controller_message(payload)
            elif kind == "countdown_state":
                if server_id == self.federation_leader_server_id:
                    await self._handle_federation_countdown(payload)
            elif kind == "player_snapshot":
                await self._handle_federation_snapshot(server_id, payload)
                round_active = payload.get("round_active")
                if isinstance(round_active, bool):
                    round_bucket = f"{server_id}:round"
                    round_sent_ns = self.federation_last_state_sent_ns.get(
                        round_bucket, 0
                    )
                    if sent_ns >= round_sent_ns:
                        self.federation_last_state_sent_ns[round_bucket] = sent_ns
                        self._handle_federation_round_state(
                            server_id,
                            {
                                "action": (
                                    "round_started"
                                    if round_active
                                    else "round_finished"
                                ),
                                "map_key": payload.get("current_map", ""),
                                "started_at": payload.get(
                                    "round_started_at", ""
                                ),
                            }
                        )
            elif kind == "player_event":
                await self._handle_federation_player_event(server_id, payload)
            elif kind == "map_commit":
                if server_id == self.federation_leader_server_id:
                    await self._handle_federation_map(payload)
            elif kind == "map_prepare":
                if server_id == self.federation_leader_server_id:
                    await self._handle_federation_map_prepare(payload)
            elif kind == "round_sync":
                await self._handle_federation_round_sync(server_id, payload)
            elif kind == "records_delta":
                await self._handle_federation_records_delta(server_id, payload)
            elif kind != "heartbeat":
                raise ValueError("unsupported event kind")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            LOG.warning("dropping invalid local federation controller event")
        except Exception:
            LOG.exception("error processing local federation controller event")

    @staticmethod
    def _federation_record_string(
        value: object,
        label: str,
        maximum: int,
    ) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ValueError(f"invalid federation record {label}")
        if any(ord(character) < 32 for character in value):
            raise ValueError(f"invalid federation record {label}")
        return value

    def _validated_federation_record(
        self,
        value: object,
    ) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("invalid federation record")
        event_id = self._federation_record_string(
            value.get("event_id"), "event ID", 64
        )
        if re.fullmatch(r"[0-9a-f]{64}", event_id) is None:
            raise ValueError("invalid federation record event ID")
        map_key = self._federation_record_string(
            value.get("map_key"),
            "map key",
            MAX_FEDERATION_RECORD_KEY_CHARACTERS,
        )
        identity_key = self._federation_record_string(
            value.get("identity_key"),
            "identity",
            MAX_FEDERATION_RECORD_IDENTITY_CHARACTERS,
        )
        if not identity_key.startswith("auth:") or identity_key != identity_key.casefold():
            raise ValueError("federated PBs require a canonical authenticated identity")
        username = self._federation_record_string(
            value.get("username"),
            "username",
            MAX_FEDERATION_PLAYER_NAME_CHARACTERS,
        )
        seconds_value = value.get("best_seconds")
        if isinstance(seconds_value, bool) or not isinstance(
            seconds_value, (int, float)
        ):
            raise ValueError("invalid federation record time")
        best_seconds = float(seconds_value)
        maximum_seconds = float(self.config.get("maximum_record_seconds", 7200))
        if (
            not math.isfinite(best_seconds)
            or best_seconds < 0
            or best_seconds > maximum_seconds
        ):
            raise ValueError("invalid federation record time")
        turns_value = value.get("best_turns")
        if turns_value is None:
            best_turns = None
        elif (
            isinstance(turns_value, bool)
            or not isinstance(turns_value, int)
            or turns_value < 0
            or turns_value > 1_000_000_000
        ):
            raise ValueError("invalid federation record turns")
        else:
            best_turns = turns_value
        achieved_value = value.get("achieved_at")
        if isinstance(achieved_value, bool) or not isinstance(
            achieved_value, (int, float)
        ):
            raise ValueError("invalid federation record timestamp")
        achieved_at = float(achieved_value)
        if (
            not math.isfinite(achieved_at)
            or achieved_at <= 0
            or achieved_at > time.time() + 300
        ):
            raise ValueError("invalid federation record timestamp")
        has_replay = value.get("has_replay", False)
        if not isinstance(has_replay, bool):
            raise ValueError("invalid federation replay availability")
        return {
            "event_id": event_id,
            "map_key": map_key,
            "identity_key": identity_key,
            "username": username,
            "best_seconds": best_seconds,
            "best_turns": best_turns,
            "achieved_at": achieved_at,
            "has_replay": has_replay,
        }

    async def _handle_federation_records_delta(
        self,
        server_id: str,
        payload: dict[str, object],
    ) -> None:
        operation = payload.get("operation")
        if operation == "snapshot_request":
            if not self.federation_leader:
                # The hub relays follower-originated control packets to every
                # enrolled peer. Other followers are not snapshot authorities
                # and simply ignore the request.
                return
            offset = payload.get("offset", 0)
            if (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset < 0
                or offset > 1_000_000
            ):
                raise ValueError("invalid federation PB snapshot offset")
            await self._publish_federation_record_snapshot(server_id, offset)
            return
        if operation == "snapshot_complete":
            target_server_id = str(payload.get("target_server_id", ""))
            if target_server_id != self.federation_local_server_id:
                return
            if (
                not self.federation_follower
                or server_id != self.federation_leader_server_id
            ):
                raise ValueError("invalid federation PB snapshot authority")
            offset = payload.get("snapshot_offset")
            if (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset != getattr(self, "_federation_record_snapshot_offset", 0)
            ):
                return
            complete = getattr(self, "_federation_record_snapshot_complete", None)
            if complete is not None:
                complete.set()
            progress = getattr(self, "_federation_record_snapshot_progress", None)
            if progress is not None:
                progress.set()
            LOG.info("authenticated PB snapshot is current at %d record(s)", offset)
            return
        if operation == "ack":
            target_server_id = str(payload.get("target_server_id", ""))
            if target_server_id and target_server_id != self.federation_local_server_id:
                return
            values = payload.get("event_ids")
            if (
                not isinstance(values, list)
                or len(values) > MAX_FEDERATION_RECORDS_PER_BATCH
            ):
                raise ValueError("invalid federation PB acknowledgment")
            event_ids = []
            for value in values:
                if not isinstance(value, str) or re.fullmatch(
                    r"[0-9a-f]{64}", value
                ) is None:
                    raise ValueError("invalid federation PB acknowledgment")
                event_ids.append(value)
            acknowledged = self.store.acknowledge_federation_records(event_ids)
            if acknowledged:
                wakeup = getattr(self, "_federation_record_wakeup", None)
                if wakeup is not None:
                    wakeup.set()
                LOG.debug(
                    "peer %s acknowledged %d federated PB(s)",
                    server_id,
                    acknowledged,
                )
            return
        if operation != "upsert":
            raise ValueError("invalid federation PB operation")
        target_server_id = str(payload.get("target_server_id", ""))
        if target_server_id and target_server_id != self.federation_local_server_id:
            return
        values = payload.get("records")
        if (
            not isinstance(values, list)
            or not values
            or len(values) > MAX_FEDERATION_RECORDS_PER_BATCH
        ):
            raise ValueError("invalid federation PB batch")
        records = [self._validated_federation_record(value) for value in values]
        snapshot_offset = payload.get("snapshot_offset")
        snapshot_next_offset = payload.get("snapshot_next_offset")
        snapshot_complete = payload.get("snapshot_complete")
        is_snapshot_page = snapshot_offset is not None
        if is_snapshot_page:
            if (
                not self.federation_follower
                or server_id != self.federation_leader_server_id
                or isinstance(snapshot_offset, bool)
                or not isinstance(snapshot_offset, int)
                or isinstance(snapshot_next_offset, bool)
                or not isinstance(snapshot_next_offset, int)
                or snapshot_offset < 0
                or snapshot_next_offset != snapshot_offset + len(records)
                or not isinstance(snapshot_complete, bool)
            ):
                raise ValueError("invalid federation PB snapshot page")
            if snapshot_offset > getattr(
                self, "_federation_record_snapshot_offset", 0
            ):
                return
        changed = 0
        event_ids = []
        for record in records:
            event_ids.append(str(record.pop("event_id")))
            changed += int(self.store.apply_federated_record(**record))
        await self._publish_federation_control(
            "records_delta",
            {
                "operation": "ack",
                "target_server_id": server_id,
                "event_ids": event_ids,
            },
        )
        if changed:
            LOG.info(
                "merged %d authenticated PB(s) from peer %s",
                changed,
                server_id,
            )
        if is_snapshot_page and snapshot_offset == getattr(
            self, "_federation_record_snapshot_offset", 0
        ):
            self._federation_record_snapshot_offset = snapshot_next_offset
            if snapshot_complete:
                complete = getattr(
                    self, "_federation_record_snapshot_complete", None
                )
                if complete is not None:
                    complete.set()
                progress = getattr(
                    self, "_federation_record_snapshot_progress", None
                )
                if progress is not None:
                    progress.set()
                LOG.info(
                    "authenticated PB snapshot completed with %d record(s)",
                    snapshot_next_offset,
                )
            else:
                progress = getattr(
                    self, "_federation_record_snapshot_progress", None
                )
                if progress is not None:
                    progress.set()

    async def _publish_federation_record_snapshot(
        self,
        target_server_id: str,
        offset: int,
    ) -> None:
        records = self.store.federation_record_snapshot(
            self.federation_local_server_id,
            MAX_FEDERATION_RECORDS_PER_BATCH,
            offset,
        )
        if not records:
            await self._publish_federation_control(
                "records_delta",
                {
                    "operation": "snapshot_complete",
                    "target_server_id": target_server_id,
                    "snapshot_offset": offset,
                },
            )
            return
        while len(records) > 1:
            candidate = {
                "kind": "records_delta",
                "payload": {
                    "operation": "upsert",
                    "target_server_id": target_server_id,
                    "snapshot_offset": offset,
                    "snapshot_next_offset": offset + len(records),
                    "snapshot_complete": False,
                    "records": records,
                },
            }
            size = len(json.dumps(
                candidate,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"))
            if size <= MAX_FEDERATION_CONTROLLER_EVENT_BYTES - 1024:
                break
            records.pop()
        next_offset = offset + len(records)
        complete = not self.store.federation_record_snapshot(
            self.federation_local_server_id,
            1,
            next_offset,
        )
        await self._publish_federation_control(
            "records_delta",
            {
                "operation": "upsert",
                "target_server_id": target_server_id,
                "snapshot_offset": offset,
                "snapshot_next_offset": next_offset,
                "snapshot_complete": complete,
                "records": records,
            },
        )

    async def _federation_record_snapshot_sync(self) -> None:
        if not self.federation_follower:
            return
        self._federation_record_snapshot_offset = 0
        self._federation_record_snapshot_complete = asyncio.Event()
        self._federation_record_snapshot_progress = asyncio.Event()
        while not self._federation_record_snapshot_complete.is_set():
            self._federation_record_snapshot_progress.clear()
            await self._publish_federation_control(
                "records_delta",
                {
                    "operation": "snapshot_request",
                    "offset": self._federation_record_snapshot_offset,
                },
            )
            try:
                await asyncio.wait_for(
                    self._federation_record_snapshot_progress.wait(),
                    timeout=2.0,
                )
            except TimeoutError:
                continue

    async def federation_record_sync(self) -> None:
        """Retry durable PB deltas until the peer acknowledges each version."""
        if self.federation_role == "off":
            return
        queued = self.store.seed_federation_record_outbox(
            self.federation_local_server_id
        )
        LOG.info("queued %d authenticated PB(s) for federation", queued)
        wakeup = getattr(self, "_federation_record_wakeup", None)
        if wakeup is None:
            wakeup = asyncio.Event()
            self._federation_record_wakeup = wakeup
        previous_event_ids: tuple[str, ...] = ()
        retry_seconds = 1.0
        while True:
            try:
                wakeup.clear()
                records = self.store.pending_federation_records(
                    MAX_FEDERATION_RECORDS_PER_BATCH
                )
                if not records:
                    previous_event_ids = ()
                    retry_seconds = 1.0
                    await wakeup.wait()
                    continue
                # Leave enough room for the sidecar's authenticated envelope.
                while len(records) > 1:
                    candidate = {
                        "kind": "records_delta",
                        "payload": {"operation": "upsert", "records": records},
                    }
                    size = len(
                        json.dumps(
                            candidate,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                    )
                    if size <= MAX_FEDERATION_CONTROLLER_EVENT_BYTES - 1024:
                        break
                    records.pop()
                event_ids = tuple(str(record["event_id"]) for record in records)
                if event_ids != previous_event_ids:
                    previous_event_ids = event_ids
                    retry_seconds = 1.0
                await self._publish_federation_control(
                    "records_delta",
                    {"operation": "upsert", "records": records},
                )
                try:
                    await asyncio.wait_for(
                        wakeup.wait(),
                        timeout=retry_seconds,
                    )
                except TimeoutError:
                    retry_seconds = min(retry_seconds * 2, 60.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("unable to synchronize federated PB records")
                await asyncio.sleep(min(retry_seconds, 60.0))

    async def _handle_federation_chat(
        self,
        server_id: str,
        payload: dict[str, object],
    ) -> None:
        if not self.federation_sync_chat:
            return
        parsed = self._federation_player_payload(payload)
        if parsed is None:
            return
        _, player = parsed
        message = self._federation_text(
            payload.get("message", ""), MAX_FEDERATION_CHAT_CHARACTERS
        )
        if not message:
            return
        await self.broadcast(
            f"[{self.federation_remote_regions.get(server_id, 'REMOTE')}] "
            f"{player['display_name']}: {message}",
            federate=False,
        )

    async def _handle_federation_command(
        self,
        server_id: str,
        payload: dict[str, object],
    ) -> None:
        if not self.federation_leader:
            return
        parsed = self._federation_player_payload(payload)
        if parsed is None:
            return
        key, item = parsed
        command = str(payload.get("command", "")).casefold()
        if (
            command in FEDERATION_LOCAL_COMMANDS
            or not re.fullmatch(r"/[a-z0-9_]{1,47}", command)
        ):
            return
        try:
            access_level = int(payload.get("access_level", 255))
        except (TypeError, ValueError):
            return
        if access_level < 0 or access_level > 255:
            return
        arguments = self._federation_text(
            payload.get("arguments", ""), MAX_FEDERATION_CHAT_CHARACTERS
        )
        player = self._federation_command_player(key, item, server_id)
        item["_server_id"] = server_id
        self.federation_remote_players[f"{server_id}\0{key}"] = item
        await self._dispatch_command(command, player, access_level, arguments)

    async def _handle_federation_controller_message(
        self,
        payload: dict[str, object],
    ) -> None:
        scope = str(payload.get("scope", ""))
        if scope == "server_state":
            if not self.federation_follower:
                return
            current_map_key = str(payload.get("current_map_key", ""))
            next_map_key = str(payload.get("next_map_key", ""))
            for map_key in (current_map_key, next_map_key):
                if (
                    len(map_key) > 512
                    or "\x00" in map_key
                    or "\r" in map_key
                    or "\n" in map_key
                ):
                    return
            changed = (
                self.federation_leader_current_map_key != current_map_key
                or self.federation_leader_next_map_key != next_map_key
            )
            self.federation_leader_current_map_key = current_map_key
            self.federation_leader_next_map_key = next_map_key
            if changed:
                # Force the one-second refresher to replace any stale local
                # SERVER_OPTIONS immediately after authoritative state lands.
                self._server_options_last = None
            return
        if scope not in {"broadcast", "broadcast_block", "private", "private_block", "center", "center_private"}:
            return
        target_server_id = str(payload.get("target_server_id", ""))
        if target_server_id and target_server_id != self.federation_local_server_id:
            return
        target = str(payload.get("target_player_id", ""))
        if target and (len(target) > 128 or any(character.isspace() for character in target)):
            return
        if scope.endswith("_block"):
            raw_lines = payload.get("lines", [])
            if not isinstance(raw_lines, list) or len(raw_lines) > 128:
                return
            lines = [self._federation_text(line, 1024) for line in raw_lines]
            styled = style_console_block(lines)
            if not styled:
                return
            if scope == "broadcast_block":
                await self.sink.send(
                    f"CONSOLE_MESSAGE {readline_console_block(styled)}"
                )
            elif target:
                await self.sink.send(
                    f"PLAYER_MESSAGE {target} {quote_console_block(styled)}"
                )
            return
        message = self._federation_text(payload.get("message", ""), 4096)
        if scope == "center":
            await self.sink.send(padded_center_command(message))
        elif scope == "broadcast":
            styled = style_console_message(message)
            await self.sink.send(
                f"CONSOLE_MESSAGE {readline_console_text(styled)}"
            )
        elif target:
            styled = style_console_message(message)
            command = "CENTER_PLAYER_MESSAGE" if scope == "center_private" else "PLAYER_MESSAGE"
            await self.sink.send(f"{command} {target} {quote_console(styled)}")

    async def _handle_federation_countdown(
        self,
        payload: dict[str, object],
    ) -> None:
        if not self.federation_follower:
            return
        active = bool(payload.get("active", False))
        if not active:
            self._clear_final_countdown_state()
            return
        map_key = str(payload.get("map_key", ""))
        try:
            end_epoch = float(payload.get("end_epoch", 0.0))
        except (TypeError, ValueError):
            return
        if not map_key or not math.isfinite(end_epoch) or end_epoch <= 0:
            return
        if self.current is not None and self.current.key != map_key:
            return
        self.final_countdown_active = True
        self.final_countdown_end_epoch = end_epoch
        self.final_countdown_map_key = map_key
        self.final_countdown_announcement = None
        self.store.set_json("final_countdown_active", True)
        self.store.set_json("final_countdown_end_epoch", end_epoch)
        self.store.set_json("final_countdown_map_key", map_key)
        self.finalists = {
            id(player)
            for player in self.active_players()
            if player.alive and player.respawn_enabled
        }
        for task in self.respawn_tasks.values():
            task.cancel()
        self.respawn_tasks.clear()
        for task in self.freeze_tasks.values():
            task.cancel()
        self.freeze_tasks.clear()
        idle_seconds = float(self.config.get("final_countdown_idle_seconds", 10))
        if idle_seconds > 0:
            await self.sink.send(f"KILL_IDLE_PLAYERS {idle_seconds:.9g}")

    async def _handle_federation_snapshot(
        self,
        server_id: str,
        payload: dict[str, object],
    ) -> None:
        players = payload.get("players", [])
        if payload.get("current_map"):
            self.federation_remote_maps[server_id] = str(payload["current_map"])
            if server_id == self.federation_leader_server_id:
                self.federation_remote_map_key = str(payload["current_map"])
        if not isinstance(players, list) or len(players) > 256:
            return
        incoming: dict[str, dict[str, object]] = {}
        for raw_player in players:
            parsed = self._federation_player_payload(raw_player)
            if parsed is not None and parsed[1]["connected"]:
                remote_key = f"{server_id}\0{parsed[0]}"
                parsed[1]["_server_id"] = server_id
                incoming[remote_key] = parsed[1]
        previous = {
            key: item
            for key, item in self.federation_remote_players.items()
            if item.get("_server_id") == server_id
        }
        if self.federation_sync_presence and server_id in self.federation_snapshots_received:
            joined = incoming.keys() - previous.keys()
            left = previous.keys() - incoming.keys()
            for key in sorted(joined):
                await self.broadcast(
                    self._federation_presence_message(
                        server_id, incoming[key], entered=True
                    ),
                    federate=False,
                )
            for key in sorted(left):
                await self.broadcast(
                    self._federation_presence_message(
                        server_id,
                        previous[key],
                        entered=False,
                    ),
                    federate=False,
                )
        for key, item in incoming.items():
            player_key = str(item["player_id"]).casefold()
            self._federation_command_player(player_key, item, server_id)
        for key in previous.keys() - incoming.keys():
            player = self.federation_command_players.get(key)
            if player is not None:
                player.connected = False
                player.active = False
                player.alive = False
        for key in previous:
            self.federation_remote_players.pop(key, None)
        self.federation_remote_players.update(incoming)
        self.federation_snapshots_received.add(server_id)
        self.federation_snapshot_received = True
        if self.federation_leader:
            await self._resolve_votes_after_eligibility_change()
        if self.federation_sync_maps and payload.get("current_map"):
            if server_id == self.federation_leader_server_id:
                await self._handle_federation_map(
                    {
                        "map_key": payload.get("current_map"),
                        "size_factor": payload.get("size_factor"),
                        "observed": True,
                    }
                )

    def _federation_colored_player_name(
        self,
        server_id: str,
        player: dict[str, object],
        display_server_tags: bool = False,
    ) -> str:
        """Render a peer name with the recipient's server-tag preference."""
        colored_name = normalize_console_colors(player.get("colored_name", ""))
        if not colored_name:
            colored_name = (
                f"{COLOR_RESET}"
                f"{self._federation_text(player.get('display_name', ''), 128)}"
            )
        region = self.federation_remote_regions.get(server_id, "REMOTE")
        expected_prefix = f"[{region}] "
        already_tagged = plain_console_text(colored_name).startswith(expected_prefix)
        colored_prefix = f"{COLOR_FEDERATION_TAG}{expected_prefix}{COLOR_RESET}"
        if already_tagged:
            if colored_name.startswith(colored_prefix):
                colored_name = colored_name[len(colored_prefix):]
            else:
                colored_name = (
                    f"{COLOR_RESET}"
                    f"{self._federation_text(player.get('display_name', ''), 128)}"
                )
        if display_server_tags:
            return f"{colored_prefix}{colored_name}"
        return colored_name

    def _federation_presence_message(
        self,
        server_id: str,
        player: dict[str, object],
        *,
        entered: bool,
        display_server_tags: bool = False,
    ) -> str:
        """Match Armagetron's native player join/leave templates and colors."""
        name = self._federation_colored_player_name(
            server_id,
            player,
            display_server_tags=display_server_tags,
        )
        active = bool(player.get("active", False))
        if entered:
            event = "entered the game." if active else "entered as spectator."
            return f"{name} {COLOR_PLAYER_ENTERED}{event}{COLOR_RESET}"
        if active:
            return f"{name} {COLOR_PLAYER_LEFT}left the game.{COLOR_RESET}"
        return (
            f"{COLOR_PLAYER_LEFT}Spectator {name} "
            f"{COLOR_PLAYER_LEFT}left.{COLOR_RESET}"
        )

    async def _handle_federation_player_event(
        self,
        server_id: str,
        payload: dict[str, object],
    ) -> None:
        action = str(payload.get("action", "")).casefold()
        if action in {"round_started", "round_finished", "round_ended", "new_round"}:
            self._handle_federation_round_state(server_id, payload)
            return
        parsed = self._federation_player_payload(payload)
        if parsed is None:
            return
        player_key, player = parsed
        key = f"{server_id}\0{player_key}"
        player["_server_id"] = server_id
        if action == "renamed":
            previous_player_id = str(payload.get("previous_player_id", ""))
            previous_key = f"{server_id}\0{previous_player_id.casefold()}"
            if (
                previous_player_id
                and len(previous_player_id) <= MAX_FEDERATION_PLAYER_NAME_CHARACTERS
                and not any(
                    character.isspace() or ord(character) < 32
                    for character in previous_player_id
                )
                and previous_key != key
            ):
                self.federation_remote_players.pop(previous_key, None)
                previous_command = self.federation_command_players.get(previous_key)
                if previous_command is not None:
                    previous_command.connected = False
                    previous_command.active = False
                    previous_command.alive = False
        previous = self.federation_remote_players.get(key)
        if action == "left" or not player["connected"]:
            self.federation_remote_players.pop(key, None)
            if self.federation_sync_presence and previous is not None:
                await self.broadcast(
                    self._federation_presence_message(
                        server_id, previous, entered=False
                    ),
                    federate=False,
                )
            command_player = self.federation_command_players.get(key)
            if command_player is not None:
                command_player.connected = False
                command_player.active = False
                command_player.alive = False
            if self.federation_leader:
                await self._resolve_votes_after_eligibility_change()
            return
        self.federation_remote_players[key] = player
        self._federation_command_player(player_key, player, server_id)
        if (
            self.federation_sync_presence
            and action == "entered"
            and previous is None
            and server_id in self.federation_snapshots_received
        ):
            # PLAYER_COLORED_NAME normally follows PLAYER_ENTERED immediately.
            # Let that event land so the original join message uses the
            # player's real color sequence instead of a temporary white name.
            await asyncio.sleep(0.05)
            current = self.federation_remote_players.get(key)
            if current is not None:
                await self.broadcast(
                    self._federation_presence_message(
                        server_id, current, entered=True
                    ),
                    federate=False,
                )
        if self.federation_leader:
            await self._resolve_votes_after_eligibility_change()

    def _federation_round_active_for_current(self) -> bool:
        current = getattr(self, "current", None)
        return bool(
            getattr(self, "federation_leader", False)
            and current is not None
            and any(
                active and map_key == current.key
                for active, map_key, _ in getattr(
                    self, "federation_remote_rounds", {}
                ).values()
            )
        )

    def _round_is_active(self) -> bool:
        return bool(
            getattr(self, "round_active", False)
            or self._federation_round_active_for_current()
        )

    def _expected_round_sync_map(self) -> str:
        if self.transitioning and self.transition_target_key:
            return self.transition_target_key
        return self.current.key if self.current else ""

    async def _publish_round_sync_reliably(
        self,
        action: str,
        map_key: str,
        release_at: float | None = None,
        ready_at: float | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "action": action,
            "map_key": map_key,
        }
        if release_at is not None:
            payload["release_at"] = round(release_at, 6)
        if ready_at is not None:
            payload["ready_at"] = round(ready_at, 6)
        for attempt in range(3):
            await self._publish_federation_control("round_sync", payload)
            if attempt < 2:
                await asyncio.sleep(0.03)

    async def _release_federated_round_if_ready(self, map_key: str) -> None:
        now_monotonic = time.monotonic()
        healthy_peers = {
            server_id
            for server_id, seen_at in self.federation_peer_last_received_monotonic.items()
            if now_monotonic - seen_at <= self.federation_peer_timeout_seconds
        }
        # Empty regions receive the same release but must not hold active
        # racers behind the engine's safety timeout. Until a presence snapshot
        # arrives, require the peer conservatively. Once it does, only peers
        # with an active local racer participate in the readiness barrier.
        required_peers = {
            server_id
            for server_id in healthy_peers
            if server_id not in self.federation_snapshots_received
            or any(
                str(player.get("_server_id", "")) == server_id
                and bool(player.get("connected", True))
                and bool(player.get("active", True))
                for player in self.federation_remote_players.values()
            )
        }
        peers_ready = all(
            self.federation_remote_round_ready.get(server_id, ("", 0.0))[0]
            == map_key
            and self.federation_remote_round_ready.get(server_id, ("", 0.0))[1]
            > self.federation_round_last_release_at
            for server_id in required_peers
        )
        if (
            not self.federation_leader
            or not getattr(self, "federation_round_sync_enabled", False)
            or self.federation_local_round_ready_key != map_key
            or self.federation_local_round_ready_at
            <= self.federation_round_last_release_at
            or not peers_ready
        ):
            return
        release_at = time.time() + self.federation_round_sync_release_lead_seconds
        self.federation_round_release_key = map_key
        self.federation_round_release_at = release_at
        self.federation_round_last_release_at = release_at
        await self.sink.send(f"FEDERATION_ROUND_RELEASE_AT {release_at:.6f}")
        await self._publish_round_sync_reliably(
            "release",
            map_key,
            release_at,
        )
        LOG.info(
            "releasing synchronized federation round: map=%s at=%.6f",
            map_key,
            release_at,
        )

    async def _handle_local_federation_round_ready(self, payload: str) -> None:
        if not getattr(self, "federation_round_sync_enabled", False):
            return
        parts = payload.split(maxsplit=1)
        map_key = parts[0] if parts else ""
        if not map_key or map_key != self._expected_round_sync_map():
            LOG.warning(
                "ignoring round-ready event for unexpected map: ready=%s expected=%s",
                map_key,
                self._expected_round_sync_map(),
            )
            return
        try:
            ready_at = float(parts[1]) if len(parts) > 1 else time.time()
        except (TypeError, ValueError):
            return
        if not math.isfinite(ready_at) or ready_at <= 0:
            return
        self.federation_local_round_ready_key = map_key
        self.federation_local_round_ready_at = ready_at
        if self.federation_leader:
            await self._release_federated_round_if_ready(map_key)
        elif self.federation_follower:
            await self._publish_round_sync_reliably(
                "ready", map_key, ready_at=ready_at
            )

    async def _handle_federation_round_sync(
        self,
        server_id: str,
        payload: dict[str, object],
    ) -> None:
        if not getattr(self, "federation_round_sync_enabled", False):
            return
        action = str(payload.get("action", "")).casefold()
        map_key = self._federation_text(payload.get("map_key", ""), 512)
        if not map_key or map_key != self._expected_round_sync_map():
            return
        if action == "ready" and self.federation_leader:
            try:
                ready_at = float(payload.get("ready_at", time.time()))
            except (TypeError, ValueError):
                return
            if not math.isfinite(ready_at) or ready_at <= 0:
                return
            self.federation_remote_round_ready_key = map_key
            self.federation_remote_round_ready_at = ready_at
            self.federation_remote_round_ready[server_id] = (map_key, ready_at)
            await self._release_federated_round_if_ready(map_key)
            return
        if (
            action != "release"
            or not self.federation_follower
            or server_id != self.federation_leader_server_id
        ):
            return
        try:
            release_at = float(payload.get("release_at", 0))
        except (TypeError, ValueError):
            return
        now = time.time()
        if not math.isfinite(release_at) or release_at < now - 5 or release_at > now + 5:
            return
        if (
            self.federation_round_release_key == map_key
            and math.isclose(
                getattr(self, "federation_round_release_at", 0.0),
                release_at,
                abs_tol=0.000001,
            )
        ):
            return
        self.federation_round_release_key = map_key
        self.federation_round_release_at = release_at
        await self.sink.send(f"FEDERATION_ROUND_RELEASE_AT {release_at:.6f}")
        LOG.info(
            "accepted synchronized federation round release: map=%s at=%.6f",
            map_key,
            release_at,
        )

    def _adopt_federation_round_start(self) -> None:
        if not self._federation_round_active_for_current():
            return
        current = self.current
        assert current is not None
        map_key = current.key
        if self.transitioning:
            if (
                self.transition_target_key != map_key
                or not self.transition_map_confirmed
            ):
                return
            self.round_started_epoch = time.time()
            self.deadline_epoch = (
                self.round_started_epoch + self._map_open_play_seconds()
            )
            self.store.set_json("deadline_epoch", self.deadline_epoch)
            self.store.set_json(
                "round_started_epoch", self.round_started_epoch
            )
            self._complete_map_transition()
        elif self.round_started_epoch is None:
            self.round_started_epoch = time.time()
            self.deadline_epoch = (
                self.round_started_epoch + self._map_open_play_seconds()
            )
            self.store.set_json("deadline_epoch", self.deadline_epoch)
            self.store.set_json(
                "round_started_epoch", self.round_started_epoch
            )
        if self.federation_remote_round_adopted_key == map_key:
            return
        self.federation_remote_round_adopted_key = map_key
        LOG.info(
            "federation authority adopted active peer round: server=%s map=%s",
            ",".join(
                sorted(
                    server_id
                    for server_id, (active, remote_map, _) in self.federation_remote_rounds.items()
                    if active and remote_map == map_key
                )
            ),
            map_key,
        )
        if not self.round_active:
            self._begin_helpful_message_round()

    def _handle_federation_round_state(
        self,
        server_id: str,
        payload: dict[str, object],
    ) -> None:
        if not self.federation_leader:
            return
        action = str(payload.get("action", "")).casefold()
        map_key = self._federation_text(payload.get("map_key", ""), 512)
        if action == "round_started":
            if not map_key:
                return
            self.federation_remote_round_active = True
            self.federation_remote_round_map_key = map_key
            self.federation_remote_round_started_at = self._federation_text(
                payload.get("started_at", ""), 64
            )
            self.federation_remote_rounds[server_id] = (
                True,
                map_key,
                self.federation_remote_round_started_at,
            )
            self._adopt_federation_round_start()
            return
        if action not in {"round_finished", "round_ended", "new_round"}:
            return
        if (
            map_key
            and self.federation_remote_round_map_key
            and map_key != self.federation_remote_round_map_key
        ):
            return
        self.federation_remote_rounds[server_id] = (False, "", "")
        remaining = [
            (remote_server_id, remote_map, started_at)
            for remote_server_id, (active, remote_map, started_at)
            in self.federation_remote_rounds.items()
            if active
        ]
        if remaining:
            _remote_server_id, remote_map, started_at = remaining[0]
            self.federation_remote_round_active = True
            self.federation_remote_round_map_key = remote_map
            self.federation_remote_round_started_at = started_at
            return
        self.federation_remote_round_active = False
        self.federation_remote_round_map_key = ""
        self.federation_remote_round_started_at = ""
        self.federation_remote_round_adopted_key = ""
        if not self.round_active:
            self._cancel_helpful_message()

    async def _handle_federation_map(self, payload: dict[str, object]) -> None:
        if not self.federation_sync_maps:
            return
        map_key = str(payload.get("map_key", ""))
        if (
            not map_key
            or len(map_key) > 512
            or "\x00" in map_key
            or "\r" in map_key
            or "\n" in map_key
        ):
            return
        try:
            size_factor = float(payload.get("size_factor", 0.0))
        except (TypeError, ValueError):
            return
        if not math.isfinite(size_factor) or abs(size_factor) > 1000:
            return
        entry = await self._find_federation_map(map_key)
        if entry is None:
            LOG.warning("leader map is unavailable locally: %s", map_key)
            return

        def already_applied() -> bool:
            return bool(
                self.current is not None
                and self.current.key == entry.key
                and self.current_size_factor is not None
                and math.isclose(
                    self.current_size_factor,
                    size_factor,
                    abs_tol=1e-6,
                )
            )

        if already_applied():
            if self.federation_prepared_map_key == entry.key:
                self.federation_prepared_map_key = None
                self.federation_prepared_map_activate_ns = 0
                self.federation_prepared_map_sha256 = ""
            return
        if self.transitioning and self.transition_target_key == entry.key:
            return
        async with self.map_lock:
            # Map commits and heartbeat snapshots are intentionally delivered
            # at least once. Several copies can pass the optimistic check above
            # while the first waits on cache I/O, so make the mutation itself
            # idempotent under the lock as well.
            if already_applied():
                if self.federation_prepared_map_key == entry.key:
                    self.federation_prepared_map_key = None
                    self.federation_prepared_map_activate_ns = 0
                    self.federation_prepared_map_sha256 = ""
                return
            if self.transitioning and self.transition_target_key == entry.key:
                return
            await asyncio.to_thread(self.repository.cache_for_server, entry)
            LOG.info(
                "applying leader map: leader=%s map=%s size=%s",
                self.federation_remote_server_id,
                entry.key,
                format_size_factor(size_factor),
            )
            self._clear_final_countdown_state()
            self.current = entry
            self.current_spec = entry.key
            self.current_size_factor = size_factor
            if self.federation_prepared_map_key == entry.key:
                self.federation_prepared_map_key = None
                self.federation_prepared_map_activate_ns = 0
                self.federation_prepared_map_sha256 = ""
            self.round_started_epoch = None
            self.deadline_epoch = None
            self.store.set_json("current_key", entry.key)
            self.store.set_json("deadline_epoch", None)
            self.store.set_json("round_started_epoch", None)
            self._clear_all_votes()
            self._begin_map_transition(entry.key)
            self.round_active = False
            self._cancel_helpful_message()
            self._reset_attempts()
            await self.sink.send(
                f"SIZE_FACTOR {format_size_factor(size_factor)}",
                f"MAP_FILE {quote_console(entry.key)}",
                "START_NEW_MATCH",
                "KILL_ALL",
                "GET_CURRENT_MAP",
            )
            await self.broadcast(
                f"Following {self.federation_remote_region}: "
                f"{self._display_map_name(entry)} by {entry.author}",
                federate=False,
            )

    async def _find_federation_map(
        self,
        map_key: str,
        expected_sha256: str = "",
    ) -> MapEntry | None:
        """Resolve the exact leader map, using Firebase only as a fallback."""

        entry = self.repository.find_by_spec(map_key)
        if entry is not None and expected_sha256:
            actual_sha256 = hashlib.sha256(entry.local_path.read_bytes()).hexdigest()
            if actual_sha256 != expected_sha256:
                LOG.error(
                    "local immutable map differs from leader digest: "
                    "map=%s local=%s leader=%s",
                    map_key,
                    actual_sha256,
                    expected_sha256,
                )
                entry = None
        if entry is not None:
            return entry

        leader_base_url = getattr(
            self, "federation_leader_resource_base_url", ""
        )
        if expected_sha256 and leader_base_url:
            async with self.map_lock:
                entry = self.repository.find_by_spec(map_key)
                if entry is not None:
                    actual_sha256 = hashlib.sha256(
                        entry.local_path.read_bytes()
                    ).hexdigest()
                    if actual_sha256 == expected_sha256:
                        return entry
                try:
                    entry = await asyncio.to_thread(
                        self.repository.fetch_federated_resource,
                        leader_base_url,
                        map_key,
                        expected_sha256,
                        getattr(self, "federation_resource_timeout_seconds", 10.0),
                    )
                    LOG.info(
                        "installed leader map over federation overlay: "
                        "map=%s sha256=%s",
                        map_key,
                        expected_sha256,
                    )
                    return entry
                except Exception:
                    LOG.exception(
                        "unable to fetch exact leader map over federation overlay: %s",
                        map_key,
                    )

        firebase = getattr(self.repository, "firebase", None)
        if firebase is None:
            return None
        if time.monotonic() < getattr(
            self,
            "federation_catalog_refresh_after_monotonic",
            0.0,
        ):
            return None
        async with self.map_lock:
            entry = self.repository.find_by_spec(map_key)
            if entry is not None:
                return entry
            now = time.monotonic()
            if now < getattr(
                self,
                "federation_catalog_refresh_after_monotonic",
                0.0,
            ):
                return None
            cooldown = max(
                5.0,
                float(
                    getattr(self, "config", {}).get(
                        "federation_catalog_refresh_cooldown_seconds",
                        15,
                    )
                ),
            )
            self.federation_catalog_refresh_after_monotonic = now + cooldown
            state = await asyncio.to_thread(firebase.get_catalog_state)
            signature = (
                int(state.get("catalogVersion") or 0),
                str(state.get("generation") or ""),
                str(state.get("serverManifestSha256") or ""),
            )
            if not all(signature):
                raise FirebaseCatalogError(
                    "leader map catalog state is incomplete"
                )
            if signature != self.catalog_state_signature:
                await asyncio.to_thread(
                    self.repository.sync,
                    catalog_state=state,
                )
                self._reconcile_rotation()
                self.catalog_state_signature = signature
                LOG.info(
                    "Firebase catalog version %d applied immediately for "
                    "leader map %s (generation %s)",
                    signature[0],
                    map_key,
                    signature[1],
                )
            return self.repository.find_by_spec(map_key)

    async def _handle_federation_map_prepare(
        self,
        payload: dict[str, object],
    ) -> None:
        if not self.federation_sync_maps:
            return
        try:
            activate_at_ns = int(payload.get("activate_at_ns", 0))
        except (TypeError, ValueError):
            return
        now_ns = time.time_ns()
        if not now_ns - 1_000_000_000 <= activate_at_ns <= now_ns + 60_000_000_000:
            return
        map_key = str(payload.get("map_key", ""))
        if (
            not map_key
            or len(map_key) > 512
            or "\x00" in map_key
            or "\r" in map_key
            or "\n" in map_key
        ):
            return
        map_sha256 = str(payload.get("map_sha256", ""))
        if map_sha256 and not re.fullmatch(r"[0-9a-f]{64}", map_sha256):
            return
        try:
            size_factor = float(payload.get("size_factor", 0.0))
        except (TypeError, ValueError):
            return
        if not math.isfinite(size_factor) or abs(size_factor) > 1000:
            return
        async with self.federation_map_prepare_lock:
            if (
                self.federation_prepared_map_key == map_key
                and self.federation_prepared_map_activate_ns == activate_at_ns
                and self.federation_prepared_map_sha256 == map_sha256
            ):
                return
            entry = await self._find_federation_map(map_key, map_sha256)
            if entry is None:
                LOG.warning(
                    "leader prepared map is unavailable locally: %s", map_key
                )
                return
            await asyncio.to_thread(self.repository.cache_for_server, entry)
            self.federation_prepared_map_key = entry.key
            self.federation_prepared_map_activate_ns = activate_at_ns
            self.federation_prepared_map_sha256 = map_sha256
            LOG.info(
                "pre-cached leader map: map=%s leader_activate_at_ns=%d",
                entry.key,
                activate_at_ns,
            )

    def _apply_advertised_game_encoding(self, advertised: object) -> None:
        current = getattr(
            self,
            "game_text_encoding",
            canonical_game_text_encoding(DEFAULT_GAME_TEXT_ENCODING),
        )
        encoding = canonical_game_text_encoding(advertised, current)
        if not getattr(self, "game_text_encoding_auto", True):
            if encoding != current:
                LOG.warning(
                    "Armagetron advertised %s but controller encoding is locked to %s",
                    encoding,
                    current,
                )
            return
        if encoding != current:
            LOG.info(
                "Armagetron text encoding changed from %s to %s",
                current,
                encoding,
            )
        self.game_text_encoding = encoding
        sink = getattr(self, "sink", None)
        if hasattr(sink, "set_encoding"):
            sink.set_encoding(encoding)

    def _decode_game_bytes(self, data: bytes, context: str) -> str:
        encoding = getattr(
            self,
            "game_text_encoding",
            canonical_game_text_encoding(DEFAULT_GAME_TEXT_ENCODING),
        )
        return decode_game_text(data, encoding, context)

    async def initialize(self, start_http: bool = True) -> None:
        LOG.info("using Armagetron text encoding %s", self.game_text_encoding)
        if (
            self.config.get("repository_auto_sync", True)
            or not (self.repository.checkout / ".git").is_dir()
        ):
            await asyncio.to_thread(self.repository.sync)
        else:
            await asyncio.to_thread(self.repository.scan)
        self._migrate_spawn_preferences()
        if self.repository.firebase is not None:
            reviews = await asyncio.to_thread(self.repository.list_map_reviews)
            review_keys = {
                str(item.get("sourceResourcePath") or "")
                for item in reviews
            }
            catalog_keys = set(self.repository.firebase_maps_by_key)
            retained_exclusions = {
                key
                for key in self.excluded_map_keys
                if key in catalog_keys and key not in review_keys
            }
            if retained_exclusions != self.excluded_map_keys:
                removed = self.excluded_map_keys - retained_exclusions
                LOG.info(
                    "removed %d stale/reviewed key(s) from permanent exclusions",
                    len(removed),
                )
                self.excluded_map_keys = retained_exclusions
                self.repository.excluded_keys = self.excluded_map_keys
                self.store.set_json(
                    "excluded_map_keys", sorted(self.excluded_map_keys)
                )
                self.excluded_map_reasons = {
                    key: reason
                    for key, reason in self.excluded_map_reasons.items()
                    if key in self.excluded_map_keys
                }
                self.store.set_json(
                    "excluded_map_reasons", self.excluded_map_reasons
                )
        self._reconcile_rotation()
        self._restore_runtime_context()
        if self.transitioning:
            self._schedule_transition_watchdog(self.transition_target_key)
        if start_http:
            self.mirror = MirrorServer(
                self.repository.public_dir,
                self.config.get("public_bind", "0.0.0.0"),
                int(self.config.get("public_port", 8080)),
            )
            self.mirror.start()
        await self._start_federation_import()
        # CURRENT_MAP is emitted during grid creation before ROUND_STARTED.
        # Keeping the writer enabled makes transition confirmation ordered and
        # removes the timing race that can leave federated commands blocked.
        initialization_commands = [
            "LADDERLOG_WRITE_CURRENT_MAP 1",
            "GET_CURRENT_MAP",
        ]
        if self.federation_role != "off":
            initialization_commands.extend([
                f"FEDERATION_ROUND_SYNC {int(self.federation_round_sync_enabled)}",
                "FEDERATION_ROUND_SYNC_TIMEOUT "
                f"{self.federation_round_sync_timeout_seconds:g}",
                "FEDERATION_ROUND_RELEASE_AT 0",
                "LADDERLOG_WRITE_FEDERATION_ROUND_READY 1",
            ])
        await self.sink.send(*initialization_commands)
        await self._resume_controller_reload()

    def _restore_runtime_context(self) -> None:
        """Recover non-record state when the controller restarts mid-round."""
        online_path = Path(self.config.get("online_players_file", ""))
        try:
            online_lines = self._decode_game_bytes(
                online_path.read_bytes(),
                "online player snapshot",
            ).splitlines()
            first_line = online_lines[0]
            entry = self.repository.find_by_spec(first_line)
            if entry:
                self.current = entry
                self.current_spec = first_line
                if (
                    self.transitioning
                    and self.transition_target_key == entry.key
                ):
                    self.transition_map_confirmed = True
            self._bootstrap_players_from_lines(online_lines[1:], authoritative=True)
        except (OSError, IndexError):
            pass

        if (
            self.final_countdown_active
            and self.current
            and self.final_countdown_map_key != self.current.key
        ):
            self._clear_final_countdown_state()

        ladder_path = Path(self.config.get("ladderlog", ""))
        try:
            with ladder_path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                start = max(0, size - 1024 * 1024)
                handle.seek(start)
                if start:
                    # A byte-size tail can begin inside a protocol line or a
                    # multibyte character; only reconstruct complete events.
                    handle.readline()
                data = self._decode_game_bytes(
                    handle.read(),
                    "ladderlog recovery tail",
                )
        except OSError:
            return
        round_state: bool | None = None
        latest_game_time: float | None = None
        for line in data.splitlines():
            event, _, payload = line.partition(" ")
            if event == "ROUND_STARTED":
                round_state = True
            elif event in {
                "NEW_ROUND",
                "ROUND_FINISHED",
                "ROUND_ENDED",
                "SHUTDOWN",
            }:
                round_state = False
            elif event == "GAME_TIME":
                with contextlib.suppress(ValueError, IndexError):
                    latest_game_time = float(payload.split()[-1])
            elif event == "PLAYER_ENTERED_GRID":
                self._handle_player_entered(payload, True, clear_center=False)
            elif event == "PLAYER_LEAVES_SPECTATORS":
                self._handle_player_entered(payload, True, clear_center=False)
            elif event == "PLAYER_ENTERED_SPECTATOR":
                self._handle_player_entered(
                    payload, False, clear_center=False
                )
            elif event == "PLAYER_JOINS_SPECTATORS":
                self._handle_player_entered(payload, False, clear_center=False)
            elif event == "PLAYER_LEFT":
                self._handle_player_left(payload)
            elif event == "PLAYER_LOGIN":
                self._handle_player_login(payload)
            elif event == "PLAYER_LOGOUT":
                self._handle_player_logout(payload)
            elif event == "PLAYER_RENAMED":
                self._handle_player_renamed(payload)
            elif event == "PLAYER_COLORED_NAME":
                self._handle_player_colored_name(payload)
            elif event == "PLAYER_AI_ENTERED":
                self._handle_player_ai_entered(payload)
            elif event == "ONLINE_PLAYER":
                self._handle_online_player(payload)
            elif event == "ONLINE_PLAYERS_ALIVE":
                self._handle_online_status(payload, True)
            elif event == "ONLINE_PLAYERS_DEAD":
                self._handle_online_status(payload, False)
        if round_state is not None:
            self.round_active = round_state
        if self.round_active and self.current:
            self._set_round_started_map(self.current.key)
        if (
            self.transitioning
            and self.transition_map_confirmed
            and self.round_active
        ):
            # The controller may restart after CURRENT_MAP and ROUND_STARTED
            # were already emitted. The live map plus the reconstructed round
            # state are enough to acknowledge that completed transition.
            LOG.info(
                "completing restored map transition: %s",
                self.transition_target_key,
            )
            self._complete_map_transition()
        if latest_game_time is not None:
            self.last_game_time = latest_game_time
            self.last_game_monotonic = time.monotonic()

    def close(self) -> None:
        for task in self.respawn_tasks.values():
            task.cancel()
        for task in self.freeze_tasks.values():
            task.cancel()
        for task in self.center_clear_tasks.values():
            task.cancel()
        if self._display_task:
            self._display_task.cancel()
        if self._transition_watchdog_task:
            self._transition_watchdog_task.cancel()
        if self._helpful_message_task:
            self._helpful_message_task.cancel()
        if self._controller_reload_task:
            self._controller_reload_task.cancel()
        for task in self._federation_event_tasks:
            task.cancel()
        self._federation_event_tasks.clear()
        if self._federation_publish_transport:
            self._federation_publish_transport.close()
            self._federation_publish_transport = None
        if self._federation_transport:
            self._federation_transport.close()
            self._federation_transport = None
        if self.federation_import_socket:
            with contextlib.suppress(FileNotFoundError):
                self.federation_import_socket.unlink()
        if self.mirror:
            self.mirror.close()
        for capture in list(self.replay_captures.values()):
            if capture.outcome == "death":
                capture.outcome = "controller_stop"
            self._persist_replay_capture(capture)
        self.replay_captures.clear()
        self.active_replay_tokens.clear()
        self.store.close()

    def request_controller_reload(self, requested_by: str = "system") -> bool:
        if self._controller_reload_task and not self._controller_reload_task.done():
            return False
        self._controller_reload_task = asyncio.create_task(
            self._drain_for_controller_reload(requested_by),
            name="controller-reload-drain",
        )
        return True

    def _controller_reload_alive_players(self) -> list[Player]:
        unique: dict[int, Player] = {}
        for player in self.players.values():
            if (
                player.connected
                and player.active
                and player.alive
                and not player.is_ai
            ):
                unique[id(player)] = player
        return list(unique.values())

    async def _drain_for_controller_reload(self, requested_by: str) -> None:
        self.respawns_paused = True
        self.controller_reload_draining = True
        now = time.time()
        resume_identity_keys = sorted(
            {
                player.identity_key
                for player in self.players.values()
                if player.connected
                and player.active
                and player.respawn_enabled
                and not player.is_ai
            }
        )
        deadline_remaining = (
            max(0.0, self.deadline_epoch - now)
            if self.deadline_epoch is not None
            else None
        )
        final_countdown_remaining = (
            max(0.0, self.final_countdown_end_epoch - now)
            if self.final_countdown_active
            and self.final_countdown_end_epoch is not None
            else None
        )
        self.controller_reload_state = {
            "version": 1,
            "pending": True,
            "map_key": self.current.key if self.current else None,
            "requested_at": now,
            "requested_by": clean_console_text(requested_by),
            "deadline_remaining": deadline_remaining,
            "final_countdown_remaining": final_countdown_remaining,
            "resume_identity_keys": resume_identity_keys,
        }
        self.store.set_json("controller_reload", self.controller_reload_state)

        for task in self.respawn_tasks.values():
            task.cancel()
        self.respawn_tasks.clear()
        held_players: list[Player] = []
        for player in {id(item): item for item in self.players.values()}.values():
            if player.pending_respawn:
                held_players.append(player)
                self._cancel_player_freeze(player)
        if held_players:
            await self.sink.send(
                *(f"KILL_SILENT {player.target}" for player in held_players)
            )

        alive = self._controller_reload_alive_players()
        if alive:
            await self.broadcast(
                "Controller reload pending. Respawns are paused; "
                f"waiting for {len(alive)} active "
                f"{'run' if len(alive) == 1 else 'runs'} to finish.",
                federate=False,
            )
        while self._controller_reload_alive_players() or self.finishes_in_progress:
            await asyncio.sleep(0.05)

        await self.broadcast(
            "Active runs are complete. Reloading the controller; "
            "respawns will resume shortly.",
            federate=False,
        )
        await asyncio.sleep(0.1)
        self.stop_event.set()

    async def _resume_controller_reload(self) -> None:
        state = self.controller_reload_state
        if not state.get("pending"):
            self.respawns_paused = False
            recovered = self._schedule_startup_respawns()
            if recovered:
                LOG.info(
                    "scheduled %d dead racer(s) after controller startup",
                    recovered,
                )
            return
        same_map = bool(
            self.current
            and state.get("map_key")
            and self.current.key == state.get("map_key")
        )
        resume_grace = max(
            1.0,
            float(self.config.get("controller_reload_resume_grace_seconds", 5)),
        )
        now = time.time()
        if same_map and state.get("deadline_remaining") is not None:
            self.deadline_epoch = now + max(
                resume_grace, float(state["deadline_remaining"])
            )
            self.store.set_json("deadline_epoch", self.deadline_epoch)
        if (
            same_map
            and self.final_countdown_active
            and state.get("final_countdown_remaining") is not None
        ):
            self.final_countdown_end_epoch = now + max(
                resume_grace, float(state["final_countdown_remaining"])
            )
            self.store.set_json(
                "final_countdown_end_epoch", self.final_countdown_end_epoch
            )

        resume_identity_keys = set(state.get("resume_identity_keys", []))
        self.controller_reload_state = {}
        self.store.set_json("controller_reload", {})
        self.respawns_paused = False
        self.controller_reload_draining = False
        recovered = 0
        if same_map:
            recovered = self._schedule_startup_respawns(resume_identity_keys)
        if recovered:
            LOG.info(
                "scheduled %d dead racer(s) after graceful controller reload",
                recovered,
            )
        await self.broadcast(
            "Controller reload complete. Respawning resumed.",
            federate=False,
        )

    def _schedule_startup_respawns(
        self,
        identity_keys: set[str] | None = None,
    ) -> int:
        """Recover eligible dead racers after any mid-round controller start."""
        if (
            not self.current
            or not self.round_active
            or self.transitioning
            or self.final_countdown_active
            or getattr(self, "respawns_paused", False)
        ):
            return 0
        scheduled = 0
        for player in {id(item): item for item in self.players.values()}.values():
            if (
                identity_keys is not None
                and player.identity_key not in identity_keys
            ):
                continue
            if (
                player.connected
                and player.active
                and player.respawn_enabled
                and not player.alive
                and not player.pending_respawn
                and not player.is_ai
                and id(player) not in self.respawn_tasks
            ):
                self._schedule_respawn(player, delay_seconds=0.1)
                scheduled += 1
        return scheduled

    @staticmethod
    def _validated_federation_catalog_exclusions(value: object) -> set[str]:
        if (
            not isinstance(value, list)
            or len(value) > MAX_FEDERATION_CATALOG_EXCLUSIONS
        ):
            raise ValueError("invalid federation catalog exclusions")
        exclusions: set[str] = set()
        for raw_key in value:
            if not isinstance(raw_key, str):
                raise ValueError("invalid federation catalog exclusion key")
            key = raw_key.strip()
            if (
                not key
                or len(key) > 512
                or not key.endswith(MAP_SUFFIX)
                or key.startswith("/")
                or ".." in Path(key).parts
                or any(ord(character) < 32 for character in key)
            ):
                raise ValueError("invalid federation catalog exclusion key")
            exclusions.add(key)
        if len(exclusions) != len(value):
            raise ValueError("duplicate federation catalog exclusion key")
        return exclusions

    async def _publish_federation_catalog_exclusions(
        self,
        target_server_id: str = "",
    ) -> bool:
        if not getattr(self, "federation_leader", False):
            return False
        exclusions = sorted(getattr(self, "excluded_map_keys", set()))
        if len(exclusions) > MAX_FEDERATION_CATALOG_EXCLUSIONS:
            LOG.error(
                "refusing to publish %d catalog exclusions; maximum is %d",
                len(exclusions),
                MAX_FEDERATION_CATALOG_EXCLUSIONS,
            )
            return False
        payload: dict[str, object] = {
            "scope": "federation_catalog_exclusion_snapshot",
            "exclusions": exclusions,
        }
        if target_server_id:
            payload["target_server_id"] = target_server_id
        return await self._publish_federation_control(
            "controller_message", payload
        )

    async def _apply_federation_catalog_exclusions(
        self,
        exclusions: set[str],
    ) -> None:
        if exclusions == getattr(self, "excluded_map_keys", set()):
            return
        self.excluded_map_keys = set(exclusions)
        self.excluded_map_reasons = {}
        self.repository.excluded_keys = self.excluded_map_keys
        self.store.set_json(
            "excluded_map_keys", sorted(self.excluded_map_keys)
        )
        self.store.set_json("excluded_map_reasons", {})
        await asyncio.to_thread(self.repository.scan)
        self._reconcile_rotation()
        LOG.info(
            "applied leader catalog exclusions: %d excluded, %d available",
            len(self.excluded_map_keys),
            len(self.repository.catalog),
        )

    async def _handle_federation_catalog_exclusion_message(
        self,
        server_id: str,
        payload: dict[str, object],
    ) -> None:
        scope = str(payload.get("scope", ""))
        if scope == "federation_catalog_exclusion_request":
            if self.federation_leader:
                await self._publish_federation_catalog_exclusions(server_id)
            return
        if scope != "federation_catalog_exclusion_snapshot":
            raise ValueError("invalid federation catalog exclusion scope")
        if (
            not self.federation_follower
            or server_id != self.federation_leader_server_id
            or str(payload.get("target_server_id", ""))
            not in {"", self.federation_local_server_id}
        ):
            return
        exclusions = self._validated_federation_catalog_exclusions(
            payload.get("exclusions")
        )
        map_lock = getattr(self, "map_lock", None)
        if map_lock is None:
            await self._apply_federation_catalog_exclusions(exclusions)
        else:
            async with map_lock:
                await self._apply_federation_catalog_exclusions(exclusions)
        complete = getattr(
            self, "_federation_catalog_exclusion_complete", None
        )
        if complete is not None:
            complete.set()

    async def federation_catalog_exclusion_sync(self) -> None:
        if self.federation_role == "off":
            return
        if self.federation_leader:
            while True:
                await self._publish_federation_catalog_exclusions()
                await asyncio.sleep(30.0)
        self._federation_catalog_exclusion_complete = asyncio.Event()
        while not self._federation_catalog_exclusion_complete.is_set():
            await self._publish_federation_control(
                "controller_message",
                {"scope": "federation_catalog_exclusion_request"},
            )
            try:
                await asyncio.wait_for(
                    self._federation_catalog_exclusion_complete.wait(),
                    timeout=2.0,
                )
            except TimeoutError:
                continue

    @staticmethod
    def _federation_preference_key(
        preference_type: str,
        identity_key: str,
        map_key: str = "",
    ) -> str:
        preference_type = str(preference_type)
        identity_key = str(identity_key)
        map_key = str(map_key)
        if preference_type not in {"start", "tags", "spawn"}:
            raise ValueError("invalid federation preference type")
        if (
            not identity_key
            or len(identity_key) > 256
            or any(ord(character) < 32 for character in identity_key)
        ):
            raise ValueError("invalid federation preference identity")
        if preference_type == "spawn":
            if (
                not map_key
                or len(map_key) > 512
                or any(ord(character) < 32 for character in map_key)
            ):
                raise ValueError("invalid federation preference map")
        elif map_key:
            raise ValueError("unexpected federation preference map")
        return json.dumps(
            [preference_type, map_key, identity_key],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def _parse_federation_preference_key(
        cls,
        key: object,
    ) -> tuple[str, str, str]:
        if not isinstance(key, str) or len(key) > 1024:
            raise ValueError("invalid federation preference key")
        try:
            value = json.loads(key)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid federation preference key") from exc
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError("invalid federation preference key")
        preference_type, map_key, identity_key = map(str, value)
        canonical = cls._federation_preference_key(
            preference_type,
            identity_key,
            map_key,
        )
        if canonical != key:
            raise ValueError("non-canonical federation preference key")
        return preference_type, map_key, identity_key

    def _current_federation_preference_keys(self) -> set[str]:
        keys = {
            self._federation_preference_key("start", identity_key)
            for identity_key in self.start_preferences
        }
        keys.update(
            self._federation_preference_key("tags", identity_key)
            for identity_key in self.display_server_tag_preferences
        )
        for map_key, preferences in self.spawn_preferences.items():
            if not isinstance(preferences, dict):
                continue
            for identity_key in preferences:
                with contextlib.suppress(ValueError):
                    keys.add(
                        self._federation_preference_key(
                            "spawn", identity_key, map_key
                        )
                    )
        return keys

    def _seed_federation_preference_versions(self) -> None:
        """Give legacy preferences a common baseline before versioned sync."""
        cleaned: dict[str, list[object]] = {}
        valid_server_ids = {
            "standalone",
            self.federation_local_server_id,
            self.federation_leader_server_id,
            *self.federation_remote_regions,
        }
        for key, raw_version in self.federation_preference_versions.items():
            try:
                self._parse_federation_preference_key(key)
                if (
                    not isinstance(raw_version, list)
                    or len(raw_version) != 2
                    or isinstance(raw_version[0], bool)
                ):
                    raise ValueError
                updated_at_ns = int(raw_version[0])
                server_id = str(raw_version[1])
                if updated_at_ns < 1 or server_id not in valid_server_ids:
                    raise ValueError
            except (TypeError, ValueError):
                continue
            cleaned[key] = [updated_at_ns, server_id]
        baseline_server_id = (
            self.federation_leader_server_id
            or self.federation_local_server_id
            or "standalone"
        )
        for key in self._current_federation_preference_keys():
            cleaned.setdefault(key, [1, baseline_server_id])
        if cleaned != self.federation_preference_versions:
            self.federation_preference_versions = cleaned
            self._save_federation_preference_versions()

    def _save_federation_preference_versions(self) -> None:
        if not hasattr(self, "store"):
            return
        self.store.set_json(
            "federation_preference_versions",
            self.federation_preference_versions,
        )

    def _persist_federation_preferences(self) -> None:
        if hasattr(self, "store") and hasattr(self, "start_preferences"):
            self._save_start_preferences()
        if (
            hasattr(self, "store")
            and hasattr(self, "display_server_tag_preferences")
        ):
            self._save_display_server_tag_preferences()
        if (
            hasattr(self, "spawn_preferences_path")
            or "_save_spawn_preferences" in self.__dict__
        ):
            self._save_spawn_preferences()
        self._save_federation_preference_versions()

    def _federation_preference_value(
        self,
        preference_type: str,
        map_key: str,
        identity_key: str,
    ) -> tuple[bool, object]:
        if preference_type == "start":
            preferences = getattr(self, "start_preferences", {})
            return (
                identity_key in preferences,
                preferences.get(identity_key),
            )
        if preference_type == "tags":
            preferences = getattr(
                self, "display_server_tag_preferences", {}
            )
            return (
                identity_key in preferences,
                preferences.get(identity_key),
            )
        preferences = getattr(self, "spawn_preferences", {}).get(map_key)
        if not isinstance(preferences, dict):
            return False, None
        return identity_key in preferences, preferences.get(identity_key)

    def _federation_preference_entry(self, key: str) -> dict[str, object]:
        preference_type, map_key, identity_key = (
            self._parse_federation_preference_key(key)
        )
        version = self.federation_preference_versions.get(key)
        if not isinstance(version, list) or len(version) != 2:
            raise ValueError("missing federation preference version")
        exists, value = self._federation_preference_value(
            preference_type, map_key, identity_key
        )
        entry: dict[str, object] = {
            "key": key,
            "updated_at_ns": int(version[0]),
            "source_server_id": str(version[1]),
            "operation": "set" if exists else "delete",
        }
        if exists:
            entry["value"] = value
        return entry

    def _validated_federation_preference_entry(
        self,
        value: object,
    ) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("invalid federation preference")
        key = str(value.get("key", ""))
        preference_type, map_key, identity_key = (
            self._parse_federation_preference_key(key)
        )
        updated_value = value.get("updated_at_ns")
        if (
            isinstance(updated_value, bool)
            or not isinstance(updated_value, int)
            or updated_value < 1
        ):
            raise ValueError("invalid federation preference version")
        source_server_id = str(value.get("source_server_id", ""))
        if source_server_id not in {
            "standalone",
            getattr(self, "federation_local_server_id", ""),
            getattr(self, "federation_leader_server_id", ""),
            *getattr(self, "federation_remote_regions", {}),
        }:
            raise ValueError("invalid federation preference source")
        operation = str(value.get("operation", ""))
        if operation not in {"set", "delete"}:
            raise ValueError("invalid federation preference operation")
        result: dict[str, object] = {
            "key": key,
            "preference_type": preference_type,
            "map_key": map_key,
            "identity_key": identity_key,
            "updated_at_ns": updated_value,
            "source_server_id": source_server_id,
            "operation": operation,
        }
        if operation == "delete":
            return result
        preference_value = value.get("value")
        if preference_type == "start":
            preference_value = str(preference_value).casefold()
            if preference_value not in {
                "brake", "immediate", "countdown", "respawn"
            }:
                raise ValueError("invalid federated start preference")
        elif preference_type == "tags":
            if not isinstance(preference_value, bool):
                raise ValueError("invalid federated tag preference")
        elif (
            isinstance(preference_value, bool)
            or not isinstance(preference_value, int)
            or preference_value < 1
            or preference_value > 100_000
        ):
            raise ValueError("invalid federated spawn preference")
        result["value"] = preference_value
        return result

    def _apply_federation_preference_entry(
        self,
        raw_entry: object,
        *,
        authority_wins_ties: bool = False,
        persist: bool = True,
    ) -> tuple[bool, str, str]:
        entry = self._validated_federation_preference_entry(raw_entry)
        key = str(entry["key"])
        incoming_version = (
            int(entry["updated_at_ns"]),
            str(entry["source_server_id"]),
        )
        current_raw = self.federation_preference_versions.get(key, [0, ""])
        current_version = (int(current_raw[0]), str(current_raw[1]))
        if incoming_version < current_version:
            return False, str(entry["preference_type"]), str(entry["identity_key"])
        if incoming_version == current_version:
            current_exists, current_value = self._federation_preference_value(
                str(entry["preference_type"]),
                str(entry["map_key"]),
                str(entry["identity_key"]),
            )
            incoming_exists = str(entry["operation"]) == "set"
            if (
                current_exists == incoming_exists
                and (not incoming_exists or current_value == entry.get("value"))
            ):
                return (
                    False,
                    str(entry["preference_type"]),
                    str(entry["identity_key"]),
                )
            if not authority_wins_ties:
                return (
                    False,
                    str(entry["preference_type"]),
                    str(entry["identity_key"]),
                )

        preference_type = str(entry["preference_type"])
        map_key = str(entry["map_key"])
        identity_key = str(entry["identity_key"])
        operation = str(entry["operation"])
        if preference_type == "start":
            if not hasattr(self, "start_preferences"):
                self.start_preferences = {}
            if operation == "set":
                self.start_preferences[identity_key] = str(entry["value"])
            else:
                self.start_preferences.pop(identity_key, None)
        elif preference_type == "tags":
            if not hasattr(self, "display_server_tag_preferences"):
                self.display_server_tag_preferences = {}
            if operation == "set":
                self.display_server_tag_preferences[identity_key] = bool(
                    entry["value"]
                )
            else:
                self.display_server_tag_preferences.pop(identity_key, None)
        else:
            if not hasattr(self, "spawn_preferences"):
                self.spawn_preferences = {}
            preferences = self.spawn_preferences.get(map_key)
            if not isinstance(preferences, dict):
                preferences = {}
            if operation == "set":
                preferences[identity_key] = int(entry["value"])
                self.spawn_preferences[map_key] = preferences
            else:
                preferences.pop(identity_key, None)
                if preferences:
                    self.spawn_preferences[map_key] = preferences
                else:
                    self.spawn_preferences.pop(map_key, None)
        self.federation_preference_versions[key] = [
            incoming_version[0], incoming_version[1]
        ]
        if persist:
            self._persist_federation_preferences()
        return True, preference_type, identity_key

    def _set_local_federation_preference(
        self,
        preference_type: str,
        identity_key: str,
        value: object | None,
        map_key: str = "",
    ) -> str:
        key = self._federation_preference_key(
            preference_type, identity_key, map_key
        )
        if not hasattr(self, "federation_preference_versions"):
            self.federation_preference_versions = {}
        if not hasattr(self, "federation_preference_pending"):
            self.federation_preference_pending = {}
        if not hasattr(self, "_federation_preference_snapshot_cache"):
            self._federation_preference_snapshot_cache = {}
        previous = self.federation_preference_versions.get(key, [0, ""])
        updated_at_ns = max(time.time_ns(), int(previous[0]) + 1)
        entry: dict[str, object] = {
            "key": key,
            "updated_at_ns": updated_at_ns,
            "source_server_id": (
                getattr(self, "federation_local_server_id", "")
                or "standalone"
            ),
            "operation": "delete" if value is None else "set",
        }
        if value is not None:
            entry["value"] = value
        self._apply_federation_preference_entry(entry)
        if getattr(self, "federation_role", "off") != "off":
            self.federation_preference_pending[key] = self._federation_preference_entry(
                key
            )
            self._federation_preference_snapshot_cache.clear()
        return key

    async def _refresh_players_for_federation_preference(
        self,
        preference_type: str,
        identity_key: str,
    ) -> None:
        players = {
            id(player): player
            for player in self.players.values()
            if player.identity_key == identity_key
        }.values()
        for player in players:
            if preference_type == "start":
                self._start_mode_for(player)
            elif preference_type == "tags":
                await self._apply_display_server_tag_preference(player)

    async def _publish_federation_preference_update(self, key: str) -> bool:
        if getattr(self, "federation_role", "off") == "off":
            return False
        try:
            entry = self._federation_preference_entry(key)
        except ValueError:
            self.federation_preference_pending.pop(key, None)
            return False
        published = await self._publish_federation_control(
            "controller_message",
            {"scope": "federation_preference_update", "entry": entry},
        )
        if published and self.federation_leader:
            self.federation_preference_pending.pop(key, None)
        return published

    def _federation_preference_entries(self) -> list[dict[str, object]]:
        entries = []
        for key in sorted(self.federation_preference_versions):
            with contextlib.suppress(ValueError):
                entries.append(self._federation_preference_entry(key))
        return entries

    async def _publish_federation_preference_snapshot(
        self,
        target_server_id: str,
        offset: int,
    ) -> None:
        cached = self._federation_preference_snapshot_cache.get(target_server_id)
        if offset == 0 or cached is None or cached[0] < time.monotonic():
            cached = (time.monotonic() + 30.0, self._federation_preference_entries())
            self._federation_preference_snapshot_cache[target_server_id] = cached
        entries = list(
            cached[1][offset : offset + MAX_FEDERATION_PREFERENCES_PER_BATCH]
        )
        while len(entries) > 1:
            candidate = {
                "kind": "controller_message",
                "payload": {
                    "scope": "federation_preference_snapshot",
                    "target_server_id": target_server_id,
                    "snapshot_offset": offset,
                    "snapshot_next_offset": offset + len(entries),
                    "snapshot_complete": False,
                    "entries": entries,
                },
            }
            size = len(json.dumps(
                candidate,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"))
            if size <= MAX_FEDERATION_CONTROLLER_EVENT_BYTES - 1024:
                break
            entries.pop()
        next_offset = offset + len(entries)
        complete = next_offset >= len(cached[1])
        await self._publish_federation_control(
            "controller_message",
            {
                "scope": "federation_preference_snapshot",
                "target_server_id": target_server_id,
                "snapshot_offset": offset,
                "snapshot_next_offset": next_offset,
                "snapshot_complete": complete,
                "entries": entries,
            },
        )

    async def _handle_federation_preference_message(
        self,
        server_id: str,
        payload: dict[str, object],
    ) -> None:
        scope = str(payload.get("scope", ""))
        if scope == "federation_preference_snapshot_request":
            if not self.federation_leader:
                return
            offset = payload.get("offset", 0)
            if (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset < 0
                or offset > 1_000_000
            ):
                raise ValueError("invalid federation preference snapshot offset")
            await self._publish_federation_preference_snapshot(server_id, offset)
            return
        if scope == "federation_preference_update":
            if self.federation_leader:
                entry = self._validated_federation_preference_entry(
                    payload.get("entry")
                )
                if str(entry["source_server_id"]) != server_id:
                    raise ValueError("forged federation preference source")
                changed, preference_type, identity_key = (
                    self._apply_federation_preference_entry(entry)
                )
                if changed:
                    self._federation_preference_snapshot_cache.clear()
                    await self._refresh_players_for_federation_preference(
                        preference_type, identity_key
                    )
                key = str(entry["key"])
                authoritative = self._federation_preference_entry(key)
                await self._publish_federation_control(
                    "controller_message",
                    {
                        "scope": "federation_preference_update",
                        "entry": authoritative,
                    },
                )
                return
            if server_id != self.federation_leader_server_id:
                return
            entry = self._validated_federation_preference_entry(
                payload.get("entry")
            )
            key = str(entry["key"])
            local_raw = self.federation_preference_versions.get(key, [0, ""])
            local_version = (int(local_raw[0]), str(local_raw[1]))
            incoming_version = (
                int(entry["updated_at_ns"]),
                str(entry["source_server_id"]),
            )
            changed, preference_type, identity_key = (
                self._apply_federation_preference_entry(
                    entry, authority_wins_ties=True
                )
            )
            if local_version > incoming_version:
                self.federation_preference_pending[key] = (
                    self._federation_preference_entry(key)
                )
            pending = self.federation_preference_pending.get(key)
            if pending is not None:
                pending_version = (
                    int(pending["updated_at_ns"]),
                    str(pending["source_server_id"]),
                )
                incoming_version = (
                    int(entry["updated_at_ns"]),
                    str(entry["source_server_id"]),
                )
                if incoming_version >= pending_version:
                    self.federation_preference_pending.pop(key, None)
            if changed:
                await self._refresh_players_for_federation_preference(
                    preference_type, identity_key
                )
            return
        if scope != "federation_preference_snapshot":
            raise ValueError("invalid federation preference scope")
        if not self.federation_follower or server_id != self.federation_leader_server_id:
            raise ValueError("invalid federation preference snapshot authority")
        if str(payload.get("target_server_id", "")) != self.federation_local_server_id:
            return
        offset = payload.get("snapshot_offset")
        next_offset = payload.get("snapshot_next_offset")
        complete = payload.get("snapshot_complete")
        entries = payload.get("entries")
        expected = getattr(self, "_federation_preference_snapshot_offset", 0)
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or isinstance(next_offset, bool)
            or not isinstance(next_offset, int)
            or offset < 0
            or not isinstance(complete, bool)
            or not isinstance(entries, list)
            or len(entries) > MAX_FEDERATION_PREFERENCES_PER_BATCH
            or next_offset != offset + len(entries)
        ):
            raise ValueError("invalid federation preference snapshot page")
        if offset != expected:
            return
        if offset == 0:
            self._federation_preference_snapshot_seen = set()
        changed_players: set[tuple[str, str]] = set()
        snapshot_changed = False
        for raw_entry in entries:
            entry = self._validated_federation_preference_entry(raw_entry)
            key = str(entry["key"])
            self._federation_preference_snapshot_seen.add(key)
            local_raw = self.federation_preference_versions.get(key, [0, ""])
            local_version = (int(local_raw[0]), str(local_raw[1]))
            incoming_version = (
                int(entry["updated_at_ns"]),
                str(entry["source_server_id"]),
            )
            changed, preference_type, identity_key = (
                self._apply_federation_preference_entry(
                    entry,
                    authority_wins_ties=True,
                    persist=False,
                )
            )
            if changed:
                snapshot_changed = True
                changed_players.add((preference_type, identity_key))
            if local_version > incoming_version:
                self.federation_preference_pending[key] = (
                    self._federation_preference_entry(key)
                )
            pending = self.federation_preference_pending.get(key)
            if pending is not None:
                pending_version = (
                    int(pending["updated_at_ns"]),
                    str(pending["source_server_id"]),
                )
                incoming_version = (
                    int(entry["updated_at_ns"]),
                    str(entry["source_server_id"]),
                )
                if incoming_version >= pending_version:
                    self.federation_preference_pending.pop(key, None)
        if snapshot_changed:
            self._persist_federation_preferences()
        for preference_type, identity_key in changed_players:
            await self._refresh_players_for_federation_preference(
                preference_type, identity_key
            )
        self._federation_preference_snapshot_offset = next_offset
        if complete:
            for key in (
                set(self.federation_preference_versions)
                - self._federation_preference_snapshot_seen
            ):
                with contextlib.suppress(ValueError):
                    self.federation_preference_pending[key] = (
                        self._federation_preference_entry(key)
                    )
            complete_event = getattr(
                self, "_federation_preference_snapshot_complete", None
            )
            if complete_event is not None:
                complete_event.set()
            LOG.info(
                "federation preference snapshot completed with %d entries",
                next_offset,
            )
        progress = getattr(self, "_federation_preference_snapshot_progress", None)
        if progress is not None:
            progress.set()

    async def federation_preference_sync(self) -> None:
        if self.federation_role == "off":
            return
        while True:
            if self.federation_follower:
                self._federation_preference_snapshot_offset = 0
                self._federation_preference_snapshot_seen: set[str] = set()
                self._federation_preference_snapshot_complete = asyncio.Event()
                self._federation_preference_snapshot_progress = asyncio.Event()
                while not self._federation_preference_snapshot_complete.is_set():
                    self._federation_preference_snapshot_progress.clear()
                    await self._publish_federation_control(
                        "controller_message",
                        {
                            "scope": "federation_preference_snapshot_request",
                            "offset": self._federation_preference_snapshot_offset,
                        },
                    )
                    try:
                        await asyncio.wait_for(
                            self._federation_preference_snapshot_progress.wait(),
                            timeout=2.0,
                        )
                    except TimeoutError:
                        continue
            for _ in range(15):
                for key in list(self.federation_preference_pending):
                    await self._publish_federation_preference_update(key)
                await asyncio.sleep(2.0)

    def _save_spawn_preferences(self) -> None:
        atomic_write_json(
            self.spawn_preferences_path,
            {"version": 2, "preferences": self.spawn_preferences},
        )

    def _migrate_spawn_preferences(self) -> None:
        """Replace revision-specific resource keys with stable map identities."""
        if not self.spawn_preferences:
            return
        active_by_rating_key = {
            entry.rating_key: entry
            for entry in self.repository.catalog.values()
        }
        migrated: dict[str, dict[str, int]] = {}
        unresolved: dict[str, dict[str, int]] = {}

        # Preserve already-migrated values first so a stale legacy alias cannot
        # overwrite a newer preference written by this controller version.
        for key, preferences in self.spawn_preferences.items():
            if key.startswith(("map-id:", "logical:", "resource:")) and isinstance(
                preferences, dict
            ):
                migrated[key] = dict(preferences)

        for key, preferences in self.spawn_preferences.items():
            if key.startswith(("map-id:", "logical:", "resource:")):
                continue
            if not isinstance(preferences, dict):
                continue
            entry = self.repository.find_by_spec(key)
            if entry is None:
                unresolved[key] = dict(preferences)
                continue
            active_entry = active_by_rating_key.get(entry.rating_key, entry)
            destination = migrated.setdefault(
                map_spawn_preferences_key(active_entry), {}
            )
            for identity_key, number in preferences.items():
                destination.setdefault(identity_key, number)

        updated = {**unresolved, **migrated}
        if updated != self.spawn_preferences:
            self.spawn_preferences = updated
            self._save_spawn_preferences()
            LOG.info(
                "migrated spawn preferences to stable map identities "
                "(%d map(s), %d unresolved legacy key(s))",
                len(migrated),
                len(unresolved),
            )

    def _spawn_preferences_for(
        self,
        entry: MapEntry,
        create: bool = False,
    ) -> dict[str, int]:
        """Return preferences for a map and absorb any remaining legacy aliases."""
        stable_key = map_spawn_preferences_key(entry)
        preferences = self.spawn_preferences.get(stable_key)
        aliases = (entry.key, f"logical:{entry.rating_key}")
        changed = False
        for alias in aliases:
            if alias == stable_key:
                continue
            legacy = self.spawn_preferences.pop(alias, None)
            if not isinstance(legacy, dict):
                continue
            if preferences is None:
                preferences = {}
            for identity_key, number in legacy.items():
                preferences.setdefault(identity_key, number)
            changed = True
        if preferences is None and create:
            preferences = {}
            changed = True
        if preferences is not None:
            self.spawn_preferences[stable_key] = preferences
        if changed:
            self._save_spawn_preferences()
        return preferences if preferences is not None else {}

    def _save_start_preferences(self) -> None:
        self.store.set_json("start_preferences", self.start_preferences)

    def _save_display_server_tag_preferences(self) -> None:
        self.store.set_json(
            "display_server_tag_preferences",
            self.display_server_tag_preferences,
        )

    def _display_server_tags_for(self, player: Player) -> bool:
        enabled = bool(
            getattr(self, "display_server_tag_preferences", {}).get(
                player.identity_key,
                False,
            )
        )
        player.display_server_tags = enabled
        return enabled

    async def _apply_display_server_tag_preference(self, player: Player) -> None:
        enabled = self._display_server_tags_for(player)
        if player.connected and not player.is_ai and not player.federation_server_id:
            await self.sink.send(
                f"FEDERATION_DISPLAY_SERVER_TAGS {player.target} "
                f"{1 if enabled else 0}"
            )

    def _migrate_display_server_tag_preference(
        self,
        previous_identity: str,
        player: Player,
    ) -> None:
        if not hasattr(self, "display_server_tag_preferences"):
            self.display_server_tag_preferences = {}
        preferences = getattr(self, "display_server_tag_preferences", {})
        previous = bool(
            preferences.get(previous_identity, player.display_server_tags)
        )
        if player.identity_key not in preferences:
            self._set_local_federation_preference(
                "tags", player.identity_key, previous
            )
        player.display_server_tags = bool(
            preferences.get(player.identity_key, False)
        )

    def _start_mode_for(self, player: Player) -> str:
        mode = getattr(player, "start_mode", "immediate").casefold()
        preferences = getattr(self, "start_preferences", {})
        saved = str(preferences.get(player.identity_key, mode)).casefold()
        if saved not in {"brake", "immediate", "countdown", "respawn"}:
            saved = "immediate"
        player.start_mode = saved
        return saved

    def _preferred_spawn_index(self, player: Player) -> int | None:
        if not self.current or not self.current.spawns:
            return None
        map_preferences = self._spawn_preferences_for(self.current)
        try:
            number = int(map_preferences.get(player.identity_key, 0))
        except (TypeError, ValueError):
            return None
        if 1 <= number <= len(self.current.spawns):
            return number - 1
        return None

    async def _command_rate_allowed(self, player: Player) -> bool:
        now = time.monotonic()
        window_seconds = max(
            1.0, float(self.config.get("command_rate_window_seconds", 5.0))
        )
        maximum = max(1, int(self.config.get("command_rate_maximum", 4)))
        player_key = id(player)
        window = self.command_windows.setdefault(player_key, collections.deque())
        while window and now - window[0] >= window_seconds:
            window.popleft()
        if len(window) < maximum:
            window.append(now)
            return True
        warning_interval = max(
            1.0,
            float(
                self.config.get(
                    "command_rate_warning_interval_seconds", window_seconds
                )
            ),
        )
        last_warning = self.command_warning_times.get(player_key, -math.inf)
        if now - last_warning >= warning_interval:
            self.command_warning_times[player_key] = now
            await self.private(player, "Command rate limit reached. Please wait.")
        return False

    def _save_rotation(self) -> None:
        self.store.set_json("rotation", list(self.rotation))
        self.store.set_json("queue", list(self.queue))
        self.store.set_json("cycle_played", sorted(self.cycle_played))

    def _display_map_name(self, entry: MapEntry) -> str:
        repository = getattr(self, "repository", None)
        if repository is not None and hasattr(repository, "display_name"):
            return repository.display_name(entry)
        return entry.name

    @staticmethod
    def _excluded_key_parts(key: str) -> tuple[str, str, str]:
        """Return a readable name, author, and version from a resource key."""
        parts = key.split("/")
        author = parts[0] if len(parts) > 1 else "Unknown"
        filename = parts[-1]
        stem = (
            filename[: -len(MAP_SUFFIX)]
            if filename.endswith(MAP_SUFFIX)
            else filename
        )
        if "-" in stem:
            name, version = stem.rsplit("-", 1)
        else:
            name, version = stem, "?"
        return name or filename, author or "Unknown", version

    def _excluded_map_rows(self) -> list[tuple[str, str, str, str, str]]:
        """Return key/name/author/version/selector rows for excluded maps."""
        parsed = [
            (key, *self._excluded_key_parts(key))
            for key in self.excluded_map_keys
        ]
        parsed.sort(
            key=lambda row: (
                row[1].casefold(),
                row[2].casefold(),
                row[3].casefold(),
                row[0].casefold(),
            )
        )
        totals = collections.Counter(row[1].casefold() for row in parsed)
        positions: collections.Counter[str] = collections.Counter()
        rows = []
        for key, name, author, version in parsed:
            positions[name.casefold()] += 1
            selector = (
                f"{name} {positions[name.casefold()]}"
                if totals[name.casefold()] > 1
                else name
            )
            rows.append((key, name, author, version, selector))
        return rows

    def _search_excluded_maps(
        self,
        query: str,
    ) -> list[tuple[str, str, str, str, str]]:
        query_fold = query.strip().casefold()
        normalized = normalized_map_name(query)
        exact = []
        partial = []
        for row in self._excluded_map_rows():
            key, name, author, version, selector = row
            names = {
                key.casefold(),
                name.casefold(),
                selector.casefold(),
                f"{name} by {author}".casefold(),
                f"{selector} by {author}".casefold(),
                Path(key).name[: -len(MAP_SUFFIX)].casefold(),
            }
            normalized_names = {normalized_map_name(item) for item in names}
            if query_fold in names or (normalized and normalized in normalized_names):
                exact.append(row)
            elif query_fold and any(query_fold in item for item in names):
                partial.append(row)
            elif normalized and any(normalized in item for item in normalized_names):
                partial.append(row)
        return exact or partial

    @staticmethod
    def _review_map_rows(
        reviews: Sequence[dict],
    ) -> list[tuple[str, str, str, str, str, str, str]]:
        """Return review-id/key/name/author/version/status/selector rows."""
        parsed = [
            (
                str(review.get("_id") or review.get("submissionId") or ""),
                str(review.get("sourceResourcePath") or ""),
                str(review.get("mapName") or "Untitled"),
                str(review.get("authorName") or "Unknown"),
                str(review.get("mapVersion") or "?"),
                str(review.get("status") or "pending"),
            )
            for review in reviews
        ]
        parsed = [row for row in parsed if row[0]]
        parsed.sort(
            key=lambda row: (
                row[2].casefold(),
                row[3].casefold(),
                row[4].casefold(),
                row[0].casefold(),
            )
        )
        totals = collections.Counter(row[2].casefold() for row in parsed)
        positions: collections.Counter[str] = collections.Counter()
        rows = []
        for review_id, key, name, author, version, status in parsed:
            positions[name.casefold()] += 1
            selector = (
                f"{name} {positions[name.casefold()]}"
                if totals[name.casefold()] > 1
                else name
            )
            rows.append(
                (review_id, key, name, author, version, status, selector)
            )
        return rows

    def _search_map_reviews(
        self,
        reviews: Sequence[dict],
        query: str,
    ) -> list[tuple[str, str, str, str, str, str, str]]:
        query_fold = query.strip().casefold()
        normalized = normalized_map_name(query)
        exact = []
        partial = []
        for row in self._review_map_rows(reviews):
            review_id, key, name, author, version, _, selector = row
            names = {
                review_id.casefold(),
                key.casefold(),
                name.casefold(),
                selector.casefold(),
                f"{name} by {author}".casefold(),
                f"{selector} by {author}".casefold(),
            }
            normalized_names = {normalized_map_name(item) for item in names}
            if query_fold in names or (normalized and normalized in normalized_names):
                exact.append(row)
            elif query_fold and any(query_fold in item for item in names):
                partial.append(row)
            elif normalized and any(normalized in item for item in normalized_names):
                partial.append(row)
        return exact or partial

    async def _exclude_map_key(self, key: str, reason: str = "") -> None:
        """Persistently remove one canonical resource from every map selector."""
        self.excluded_map_keys.add(key)
        if not hasattr(self, "excluded_map_reasons"):
            self.excluded_map_reasons = {}
        if reason.strip():
            self.excluded_map_reasons[key] = reason.strip()
        self.repository.excluded_keys = self.excluded_map_keys
        self.store.set_json("excluded_map_keys", sorted(self.excluded_map_keys))
        self.store.set_json("excluded_map_reasons", self.excluded_map_reasons)
        self.repository.catalog.pop(key, None)
        source_to_key = getattr(self.repository, "source_to_key", {})
        for source, mapped_key in list(source_to_key.items()):
            if mapped_key == key:
                source_to_key.pop(source, None)
        self.rotation = collections.deque(item for item in self.rotation if item != key)
        self.queue = collections.deque(item for item in self.queue if item != key)
        self.cycle_played.discard(key)
        self._save_rotation()
        await self._publish_federation_catalog_exclusions()

    def _reconcile_rotation(self) -> None:
        available = set(self.repository.catalog)
        self.cycle_played.intersection_update(available)
        seen: set[str] = set()
        retained = []
        for key in self.rotation:
            if key in available and key not in self.cycle_played and key not in seen:
                retained.append(key)
                seen.add(key)
        # A repository refresh may add maps, but must never re-add maps already
        # consumed in this shuffle cycle.
        additions = list(available - seen - self.cycle_played)
        random.SystemRandom().shuffle(additions)
        retained.extend(additions)
        self.rotation = collections.deque(retained)
        self.queue = collections.deque(key for key in self.queue if key in available)
        self._save_rotation()

    def _refill_rotation(self) -> None:
        current_key = self.current.key if self.current else None
        keys = [
            key for key in self.repository.catalog
            if key != current_key
        ]
        random.SystemRandom().shuffle(keys)
        self.cycle_played.clear()
        self.rotation = collections.deque(keys)

    def _peek_next(self) -> MapEntry | None:
        current_key = self.current.key if self.current else None
        for key in self.queue:
            if key != current_key:
                return self.repository.catalog.get(key)
        if not self.rotation:
            self._refill_rotation()
        for key in self.rotation:
            if key != current_key:
                return self.repository.catalog.get(key)
        return None

    def _server_options_text(self) -> str:
        current = self.current
        current_name = self._display_map_name(current) if current else "Unknown"
        current_author = current.author if current else "Unknown"
        if getattr(self, "federation_follower", False):
            next_key = ""
            if (
                current is not None
                and getattr(self, "federation_leader_current_map_key", "")
                == current.key
            ):
                next_key = getattr(self, "federation_leader_next_map_key", "")
            next_entry = self.repository.catalog.get(next_key) if next_key else None
            if next_entry is not None:
                next_name = self._display_map_name(next_entry)
                next_author = next_entry.author
            elif next_key:
                next_name, next_author, _version = self._excluded_key_parts(next_key)
                next_name = next_name.replace("_", " ")
            else:
                next_name = next_author = "Unknown"
        else:
            next_entry = self._peek_next()
            next_name = self._display_map_name(next_entry) if next_entry else "Unknown"
            next_author = next_entry.author if next_entry else "Unknown"
        return clean_console_text(
            f"Current map: {current_name} by {current_author} | "
            f"Next Map: {next_name} by {next_author}"
        )

    async def _refresh_server_options_once(self) -> None:
        options = self._server_options_text()
        changed = options != self._server_options_last
        if changed:
            await self.sink.send(
                f"SERVER_OPTIONS {readline_console_text(options)}"
            )
            self._server_options_last = options
        if not getattr(self, "federation_leader", False):
            return
        now = time.monotonic()
        last_publish = getattr(
            self,
            "_federation_server_state_last_publish_monotonic",
            0.0,
        )
        if not changed and now - last_publish < 10.0:
            return
        next_entry = self._peek_next()
        published = await self._publish_federation_control(
            "controller_message",
            {
                "scope": "server_state",
                "current_map_key": self.current.key if self.current else "",
                "next_map_key": next_entry.key if next_entry else "",
            },
        )
        if published:
            self._federation_server_state_last_publish_monotonic = now

    async def server_options_refresher(self) -> None:
        interval = max(
            0.25,
            float(self.config.get("server_options_refresh_seconds", 1.0)),
        )
        while not self.stop_event.is_set():
            try:
                await self._refresh_server_options_once()
            except Exception:
                LOG.exception("server options refresh failed")
            await asyncio.sleep(interval)

    def _take_next(self) -> MapEntry | None:
        current_key = self.current.key if self.current else None
        key = None
        while self.queue and key is None:
            candidate = self.queue.popleft()
            if candidate == current_key:
                LOG.warning("discarding current map from next-map queue: %s", candidate)
                continue
            key = candidate
            with contextlib.suppress(ValueError):
                self.rotation.remove(key)
        if key is None:
            if not self.rotation:
                self._refill_rotation()
            while self.rotation and key is None:
                candidate = self.rotation.popleft()
                if candidate == current_key:
                    LOG.warning("discarding current map from rotation head: %s", candidate)
                    continue
                key = candidate
        if key is None:
            # A restored rotation can contain only the active map at the end
            # of a shuffle cycle. Refill once after discarding it so another
            # available map is still selected.
            self._refill_rotation()
            if self.rotation:
                key = self.rotation.popleft()
        if key:
            self.cycle_played.add(key)
        self._save_rotation()
        return self.repository.catalog.get(key) if key else None

    def _set_round_started_map(self, key: str | None) -> None:
        self.round_started_map_key = key
        self.store.set_json("round_started_map_key", key)

    def _begin_map_transition(self, target_key: str) -> None:
        """Wait for the target map before accepting its ROUND_STARTED event."""
        self._set_round_started_map(None)
        self.federation_local_round_ready_key = ""
        self.federation_remote_round_ready_key = ""
        getattr(self, "federation_remote_round_ready", {}).clear()
        self.federation_local_round_ready_at = 0.0
        self.federation_remote_round_ready_at = 0.0
        self.federation_round_release_key = ""
        self.federation_remote_round_active = False
        self.federation_remote_round_map_key = ""
        self.federation_remote_round_started_at = ""
        self.federation_remote_round_adopted_key = ""
        getattr(self, "federation_remote_rounds", {}).clear()
        self.transitioning = True
        self.transition_target_key = target_key
        self.transition_map_confirmed = False
        self.transition_observed_key = None
        self.transition_started_epoch = time.time()
        self.transition_round_started_pending = False
        self.store.set_json("transitioning", True)
        self.store.set_json("transition_target_key", target_key)
        self._schedule_transition_watchdog(target_key)

    def _complete_map_transition(self) -> None:
        self.transitioning = False
        self.transition_target_key = None
        self.transition_map_confirmed = False
        self.transition_observed_key = None
        self.transition_started_epoch = None
        self.transition_round_started_pending = False
        self.store.set_json("transitioning", False)
        self.store.set_json("transition_target_key", None)
        task = getattr(self, "_transition_watchdog_task", None)
        self._transition_watchdog_task = None
        if task:
            with contextlib.suppress(RuntimeError):
                if task is not asyncio.current_task():
                    task.cancel()

    def _schedule_transition_watchdog(self, target_key: str | None) -> None:
        if not target_key:
            return
        old_task = getattr(self, "_transition_watchdog_task", None)
        if old_task and not old_task.done():
            old_task.cancel()
        self._transition_watchdog_task = asyncio.create_task(
            self._watch_map_transition(target_key),
            name=f"map-transition-{target_key}",
        )

    async def _watch_map_transition(self, target_key: str) -> None:
        """Recover when Armagetron rejects a requested map and falls back."""
        timeout = max(
            0.05,
            float(self.config.get("map_transition_timeout_seconds", 20.0)),
        )
        probe_delay = max(
            0.05,
            float(self.config.get("map_transition_probe_seconds", 1.0)),
        )
        confirmations_required = max(
            1,
            int(self.config.get("map_transition_failure_confirmations", 2)),
        )
        mismatch_count = 0
        try:
            await asyncio.sleep(timeout)
            while (
                self.transitioning
                and self.transition_target_key == target_key
                and not self.transition_map_confirmed
            ):
                # CURRENT_MAP is authoritative. Requiring repeated fresh replies
                # prevents a slow but valid map change from being mistaken for a
                # load failure merely because an old ROUND_STARTED was queued.
                self.transition_observed_key = None
                await self.sink.send("GET_CURRENT_MAP")
                await asyncio.sleep(probe_delay)
                if (
                    not self.transitioning
                    or self.transition_target_key != target_key
                    or self.transition_map_confirmed
                ):
                    return
                observed_key = self.transition_observed_key
                if observed_key and observed_key != target_key:
                    mismatch_count += 1
                    LOG.warning(
                        "map transition probe %d/%d: requested=%s active=%s",
                        mismatch_count,
                        confirmations_required,
                        target_key,
                        observed_key,
                    )
                    if mismatch_count >= confirmations_required:
                        await self._recover_failed_map_transition(
                            target_key,
                            observed_key,
                        )
                        return
                else:
                    mismatch_count = 0
                await asyncio.sleep(probe_delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("map transition watchdog failed for %s", target_key)
        finally:
            if (
                getattr(self, "_transition_watchdog_task", None)
                is asyncio.current_task()
            ):
                self._transition_watchdog_task = None

    async def _recover_failed_map_transition(
        self,
        target_key: str,
        active_key: str,
    ) -> None:
        if not self.transitioning or self.transition_target_key != target_key:
            return
        failed_entry = self.repository.catalog.get(target_key)
        failed_name = (
            self._display_map_name(failed_entry)
            if failed_entry
            else self._excluded_key_parts(target_key)[0]
        )
        failed_author = (
            failed_entry.author
            if failed_entry
            else self._excluded_key_parts(target_key)[1]
        )
        try:
            await asyncio.to_thread(
                publish_repository_map_status,
                self.repository,
                target_key,
                "inactive",
                "Server automatically deactivated a map that failed to load",
            )
        except Exception:
            # Transition recovery must not wait indefinitely on an external
            # catalog. The durable local exclusion remains authoritative for
            # this server until a later admin/retry reconciles Firebase.
            LOG.exception("unable to publish failed-map exclusion to Firebase")
        await self._exclude_map_key(
            target_key,
            "Server automatically deactivated a map that failed to load",
        )
        LOG.error(
            "map failed to load; excluded requested=%s active=%s",
            target_key,
            active_key,
        )
        self._complete_map_transition()
        try:
            await self.broadcast(
                f"ERROR: Failed to load {failed_name} by {failed_author}. "
                "It was added to the exclusion list; advancing to the next map."
            )
        finally:
            await self.activate_next_map("previous map failed to load")

    def _effective_map_size_factor(self, entry: MapEntry) -> float:
        default = float(self.config.get("default_size_factor", 0))
        try:
            embedded = self.repository.map_size_factor(entry)
        except (OSError, ET.ParseError, TypeError, ValueError):
            LOG.exception("unable to read embedded SIZE_FACTOR for %s", entry.key)
            return default
        if embedded is None:
            return default
        value = float(embedded)
        if not math.isfinite(value) or abs(value) > 1000:
            LOG.warning("ignoring invalid embedded SIZE_FACTOR for %s", entry.key)
            return default
        return value

    async def activate_next_map(self, reason: str) -> None:
        if self.federation_follower and not reason.startswith("admin "):
            LOG.info(
                "leader controls automatic map advance; ignoring local trigger: %s",
                reason,
            )
            return
        if getattr(self, "controller_reload_draining", False):
            LOG.info(
                "deferring map advance during controller reload drain: %s",
                reason,
            )
            return
        async with self.map_lock:
            previous_key = self.current.key if self.current else ""
            entry = self._take_next()
            if not entry:
                LOG.error("cannot advance map: repository catalog is empty")
                return
            await asyncio.to_thread(self.repository.cache_for_server, entry)
            # Announce the value the map will actually apply when its grid is
            # built. Publishing the default first makes a follower alternate
            # between that value and an embedded map setting, reloading the
            # same map and issuing KILL_ALL on every federation snapshot.
            size_factor = self._effective_map_size_factor(entry)
            await self._prepare_federated_leader_map(entry, size_factor)
            LOG.info("advancing to %s (%s)", entry.key, reason)
            self._clear_final_countdown_state()
            self.current = entry
            self.current_spec = entry.key
            self.current_size_factor = size_factor
            self.round_started_epoch = None
            self.deadline_epoch = time.time() + self._map_open_play_seconds(entry)
            self.store.set_json("current_key", entry.key)
            self.store.set_json("deadline_epoch", self.deadline_epoch)
            self.store.set_json("round_started_epoch", None)
            self._clear_all_votes()
            self._begin_map_transition(entry.key)
            self.round_active = False
            self._cancel_helpful_message()
            self._reset_attempts()
            self._publish_dashboard_map_change(previous_key)
            # START_NEW_MATCH only schedules a reset after the current round.  Since
            # this controller respawns dead racers, that round may otherwise never
            # become empty.  transitioning is already true, so the deaths emitted
            # by KILL_ALL are deliberately not respawned.
            await self.sink.send(
                f"SIZE_FACTOR {format_size_factor(size_factor)}",
                f"MAP_FILE {quote_console(entry.key)}",
                "START_NEW_MATCH",
                "KILL_ALL",
                "GET_CURRENT_MAP",
            )
            await self.broadcast(
                f"Next map: {self._display_map_name(entry)} by {entry.author}"
            )

    def _reset_attempts(self) -> None:
        for task in self.respawn_tasks.values():
            task.cancel()
        self.respawn_tasks.clear()
        for task in self.freeze_tasks.values():
            task.cancel()
        self.freeze_tasks.clear()
        for task in self.center_clear_tasks.values():
            task.cancel()
        self.center_clear_tasks.clear()
        for player in self.players.values():
            player.generation += 1
            player.pending_respawn = False
            player.alive = False
            player.spawn_cursor = self._preferred_spawn_index(player) or 0
            player.last_spawn_index = None
            player.respawn_created_game = None
            player.attempt_started_game = None
            player.attempt_number = 0
            self._clear_checkpoint_run(player)

    @staticmethod
    def _clear_checkpoint_run(player: Player) -> None:
        player.checkpoints_collected.clear()
        player.checkpoint_notice_monotonic = None
        player.checkpoint_snapshot = None
        player.checkpoint_respawn_requested = False
        player.checkpoint_respawn_speed = None
        player.checkpoint_respawn_used = False
        player.pending_respawn_kind = ""
        player.no_cp_elapsed = 0.0
        player.no_cp_segment_started_game = None
        player.last_checkpoint_respawn_monotonic = None
        player.last_checkpoint_game = None

    def _map_play_seconds(self, entry: object | None = None) -> float:
        entry = entry or getattr(self, "current", None)
        minimum = max(
            120.0,
            float(self.config.get("minimum_map_duration_seconds", 120)),
        )
        maximum = max(
            minimum,
            float(self.config.get("map_duration_seconds", 300)),
        )
        if entry is None:
            return max(0.0, maximum)
        records = self.store.records(map_records_key(entry))
        return map_play_seconds(
            records,
            maximum,
            float(self.config.get("map_time_racer_multiplier", 1.25)),
            float(self.config.get("map_time_target_finishes", 5)),
            minimum,
        )

    def _map_open_play_seconds(self, entry: object | None = None) -> float:
        entry = entry or getattr(self, "current", None)
        minimum = max(
            120.0,
            float(self.config.get("minimum_map_duration_seconds", 120)),
        )
        maximum = max(
            minimum,
            float(self.config.get("map_duration_seconds", 300)),
        )
        if entry is None:
            return max(0.0, maximum)
        records = self.store.records(map_records_key(entry))
        return map_open_play_seconds(
            records,
            maximum,
            float(self.config.get("map_time_racer_multiplier", 1.25)),
            float(self.config.get("map_time_target_finishes", 5)),
            minimum,
        )

    def _begin_new_attempt(self, player: Player, event_game: float) -> None:
        self._clear_checkpoint_run(player)
        player.attempt_started_game = event_game
        player.no_cp_segment_started_game = event_game
        player.attempt_number += 1

    @staticmethod
    def _resume_checkpoint_attempt(player: Player, event_game: float) -> bool:
        snapshot = player.checkpoint_snapshot
        if snapshot is None:
            return False
        player.attempt_started_game = snapshot.attempt_started_game
        player.checkpoints_collected = set(snapshot.checkpoints_collected)
        player.no_cp_elapsed = snapshot.no_cp_elapsed
        player.no_cp_segment_started_game = event_game
        player.checkpoint_respawn_used = True
        player.checkpoint_respawn_requested = False
        player.checkpoint_respawn_speed = None
        player.pending_respawn_kind = ""
        return True

    def _cancel_player_freeze(self, player: Player, clear_attempt: bool = True) -> None:
        player.generation += 1
        player.pending_respawn = False
        player.respawn_created_game = None
        player.manual_restart_pending = False
        if clear_attempt:
            player.attempt_started_game = None
            self._clear_checkpoint_run(player)
        task = self.respawn_tasks.pop(id(player), None)
        if task:
            task.cancel()
        task = self.freeze_tasks.pop(id(player), None)
        if task:
            task.cancel()
        task = self.center_clear_tasks.pop(id(player), None)
        if task:
            task.cancel()

    def _clear_final_countdown_state(self) -> None:
        self.final_countdown_active = False
        self.final_countdown_end_epoch = None
        self.final_countdown_map_key = None
        self.final_countdown_announcement = None
        self.finalists.clear()
        getattr(self, "federation_finalists", set()).clear()
        self.store.set_json("final_countdown_active", False)
        self.store.set_json("final_countdown_end_epoch", None)
        self.store.set_json("final_countdown_map_key", None)

    async def _publish_federation_message(
        self,
        scope: str,
        *,
        message: str | None = None,
        lines: Sequence[object] | None = None,
        player: Player | None = None,
    ) -> None:
        if getattr(self, "federation_role", "off") == "off":
            return
        payload: dict[str, object] = {"scope": scope}
        if message is not None:
            payload["message"] = str(message)
        if lines is not None:
            payload["lines"] = [str(line) for line in lines]
        if player is not None and player.federation_server_id:
            payload["target_server_id"] = player.federation_server_id
            payload["target_player_id"] = player.target
        await self._publish_federation_control("controller_message", payload)

    async def broadcast(self, message: str, federate: bool = True) -> None:
        styled = style_console_message(message)
        await self.sink.send(f"CONSOLE_MESSAGE {readline_console_text(styled)}")
        if federate:
            await self._publish_federation_message(
                "broadcast", message=message
            )

    async def _write_dashboard_chat(self, message: dict[str, object]) -> None:
        try:
            dashboard = getattr(self, "live_dashboard_chat", None)
            if dashboard is None:
                return
            await asyncio.to_thread(dashboard.publish_chat, message)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("unable to publish live dashboard chat")

    async def _write_dashboard_activity(self, finish: dict[str, object]) -> None:
        try:
            dashboard = getattr(self, "live_dashboard_chat", None)
            if dashboard is None:
                return
            await asyncio.to_thread(dashboard.publish_activity, finish)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("unable to publish live dashboard activity")

    def _publish_dashboard_map_change(self, previous_key: str) -> None:
        if (
            getattr(self, "live_dashboard", None) is None
            or getattr(self, "live_dashboard_chat", None) is None
            or self.current is None
            or not previous_key
            or previous_key == self.current.key
        ):
            return
        live_config = self.config.get("live_dashboard", {})
        payload = {
            **self._dashboard_map_metadata(self.current),
            "kind": "map_change",
            "mapName": self.current.name,
            "serverId": clean_console_text(self.federation_local_server_id)[:32],
            "region": clean_console_text(
                str(live_config.get("local_region", "LOCAL"))
            )[:16],
        }
        task = asyncio.create_task(
            self._write_dashboard_activity(payload),
            name="live-dashboard-map-change",
        )
        self._federation_event_tasks.add(task)
        task.add_done_callback(self._federation_event_tasks.discard)

    def _publish_dashboard_chat(
        self,
        server_id: str,
        region: str,
        name: str,
        message: str,
        authenticated: bool,
    ) -> None:
        if getattr(self, "live_dashboard_chat", None) is None:
            return
        clean_message = clean_console_text(message).strip()
        if not clean_message or clean_message.startswith("/"):
            return
        task = asyncio.create_task(
            self._write_dashboard_chat({
                "kind": "chat",
                "serverId": clean_console_text(server_id)[:32],
                "region": clean_console_text(region)[:16],
                "name": plain_console_text(name).strip()[:128] or "Player",
                "message": plain_console_text(clean_message).strip()[:512],
                "authenticated": bool(authenticated),
            }),
            name="live-dashboard-chat",
        )
        self._federation_event_tasks.add(task)
        task.add_done_callback(self._federation_event_tasks.discard)

    def _publish_dashboard_presence(self, action: str, player: Player) -> None:
        if (
            getattr(self, "live_dashboard_chat", None) is None
            or action not in {"join", "leave"}
            or player.is_ai
        ):
            return
        live_config = self.config.get("live_dashboard", {})
        if action == "join":
            message = "entered the game." if player.active else "entered as spectator."
        else:
            message = "left the game." if player.active else "left as spectator."
        task = asyncio.create_task(
            self._write_dashboard_chat({
                "kind": action,
                "serverId": clean_console_text(
                    self.federation_local_server_id
                )[:32],
                "region": clean_console_text(
                    str(live_config.get("local_region", "LOCAL"))
                )[:16],
                "name": plain_console_text(player.display_name).strip()[:128]
                or "Player",
                "message": message,
                "authenticated": bool(player.auth_name),
            }),
            name=f"live-dashboard-{action}",
        )
        self._federation_event_tasks.add(task)
        task.add_done_callback(self._federation_event_tasks.discard)

    def _publish_dashboard_finish_activity(
        self,
        player: Player,
        *,
        seconds: float,
        rank: int,
        turns: int | None,
        improved: bool,
        best_seconds: float,
        best_turns: int | None,
        previous_best: float | None,
        previous_best_turns: int | None,
        pb_rank: int | None,
        no_cp_seconds: float | None,
        no_cp_rank: int | None,
    ) -> None:
        if getattr(self, "live_dashboard_chat", None) is None or self.current is None:
            return
        reference_seconds = (
            previous_best
            if improved and previous_best is not None
            else None if improved else best_seconds
        )
        reference_turns = (
            previous_best_turns
            if improved and previous_best is not None
            else None if improved else best_turns
        )
        live_config = self.config.get("live_dashboard", {})
        payload = {
            **self._dashboard_map_metadata(self.current),
            "kind": "finish",
            "mapName": self.current.name,
            "serverId": clean_console_text(self.federation_local_server_id)[:32],
            "region": clean_console_text(
                str(live_config.get("local_region", "LOCAL"))
            )[:16],
            "playerId": public_player_id(player.identity_key),
            "name": plain_console_text(player.record_name).strip()[:128] or "Player",
            "authenticated": bool(player.auth_name),
            "seconds": round(seconds, 6),
            "rank": max(1, int(rank)),
            "referenceRank": None if pb_rank is None else max(1, int(pb_rank)),
            "referenceSeconds": (
                None if reference_seconds is None else round(reference_seconds, 6)
            ),
            "splitSeconds": (
                None
                if reference_seconds is None
                else round(seconds - reference_seconds, 6)
            ),
            "noCheckpointSeconds": (
                None if no_cp_seconds is None else round(no_cp_seconds, 6)
            ),
            "noCheckpointRank": (
                None if no_cp_rank is None else max(1, int(no_cp_rank))
            ),
            "noCheckpointSplitSeconds": (
                None
                if no_cp_seconds is None
                else round(no_cp_seconds - best_seconds, 6)
            ),
            "turns": turns,
            "referenceTurns": reference_turns,
            "turnsSplit": (
                None
                if turns is None or reference_turns is None
                else turns - reference_turns
            ),
            "personalBest": bool(improved),
        }
        task = asyncio.create_task(
            self._write_dashboard_activity(payload),
            name="live-dashboard-finish",
        )
        self._federation_event_tasks.add(task)
        task.add_done_callback(self._federation_event_tasks.discard)

    def _dashboard_map_metadata(self, entry: MapEntry | None) -> dict[str, object]:
        if entry is None:
            return {}
        return {
            "mapKey": map_records_key(entry),
            "resourcePath": entry.key,
            "mapId": entry.map_id,
            "name": entry.name,
            "author": entry.author,
            "version": entry.version,
            "storagePath": entry.storage_path,
            "sizeFactor": round(float(self.current_size_factor or 0), 6),
            "checkpointCount": len(entry.checkpoint_ids),
            "checkpointMode": entry.checkpoint_mode,
        }

    def _dashboard_players(
        self,
    ) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]]]:
        # The federation transport projects a remote cycle into the local game
        # so that scoreboards and nameplates work across servers. The engine's
        # ONLINE_PLAYER snapshot reports that projection as an unauthenticated
        # local player as well. Prefer the authoritative remote snapshot when
        # both entries have the same visible name, otherwise the public live
        # view lists one physical racer twice with two different player IDs.
        remote_names = {
            plain_console_text(item.get("display_name", ""))
            .strip()
            .lstrip("|")
            .casefold()
            for item in self.federation_remote_players.values()
            if item.get("connected", True)
        }
        local = [
            {
                "playerId": public_player_id(player.identity_key),
                "name": plain_console_text(player.display_name).strip()[:128],
                "active": bool(player.active),
                "alive": bool(player.alive),
                "authenticated": bool(player.auth_name),
            }
            for player in self.players.values()
            if (
                player.connected
                and not player.is_ai
                and not (
                    not player.auth_name
                    and plain_console_text(player.display_name)
                    .strip()
                    .lstrip("|")
                    .casefold()
                    in remote_names
                )
            )
        ]
        remote: dict[str, list[dict[str, object]]] = {
            server_id: [] for server_id in self.federation_remote_regions
        }
        for item in self.federation_remote_players.values():
            if not item.get("connected", True):
                continue
            server_id = str(item.get("_server_id", self.federation_remote_server_id))
            if server_id not in remote:
                continue
            remote[server_id].append({
                "playerId": public_player_id(
                    "auth:" + str(item.get("authenticated_name", "")).casefold()
                    if item.get("authenticated_name")
                    else "guest:"
                    + server_id.casefold()
                    + ":"
                    + plain_console_text(item.get("display_name", "")).casefold()
                ),
                "name": plain_console_text(item.get("display_name", "")).strip()[:128],
                "active": bool(item.get("active")),
                "alive": bool(item.get("alive")),
                "authenticated": bool(item.get("authenticated_name")),
            })
        key = lambda item: (not bool(item["active"]), str(item["name"]).casefold())
        return sorted(local, key=key), {
            server_id: sorted(players, key=key)
            for server_id, players in remote.items()
        }

    def _dashboard_live_state(self) -> dict[str, object]:
        live_config = self.config.get("live_dashboard", {})
        local_players, remote_players = self._dashboard_players()
        now = time.time()
        now_monotonic = time.monotonic()
        time_left = max(0, int((self.deadline_epoch or now) - now))
        map_metadata = self._dashboard_map_metadata(self.current)
        current_records = self.store.records(map_records_key(self.current)) if self.current else []
        replay_player_ids = self.store.dashboard_replay_player_ids(
            map_records_key(self.current)
        ) if self.current else set()
        current_leaderboard = [
            {
                "rank": rank,
                "playerId": public_player_id(record.identity_key),
                "name": record.username[:128],
                "seconds": round(record.best_seconds, 6),
                "turns": record.best_turns,
                "authenticated": record.authenticated,
                "achievedAt": (
                    int(record.achieved_at * 1000)
                    if record.achieved_at is not None else None
                ),
                "hasReplay": public_player_id(record.identity_key) in replay_player_ids,
            }
            for rank, record in enumerate(current_records[:10], 1)
        ]
        return {
            "map": map_metadata,
            "nextMap": self._dashboard_map_metadata(self._peek_next()),
            "roundActive": self._round_is_active(),
            "timeRemainingSeconds": time_left,
            "leaderboard": current_leaderboard,
            "servers": {
                self.federation_local_server_id: {
                    "id": self.federation_local_server_id,
                    "region": str(live_config.get("local_region", "LOCAL"))[:16],
                    "online": True,
                    "mapKey": map_records_key(self.current) if self.current else "",
                    "players": local_players,
                },
                **{
                    server_id: {
                        "id": server_id,
                        "region": self.federation_remote_regions[server_id],
                        "online": (
                            server_id in self.federation_snapshots_received
                            and now_monotonic
                            - self.federation_peer_last_received_monotonic.get(
                                server_id, 0.0
                            )
                            <= self.federation_peer_timeout_seconds
                        ),
                        "mapKey": self.federation_remote_maps.get(server_id, ""),
                        "players": remote_players.get(server_id, []),
                    }
                    for server_id in self.federation_remote_regions
                },
            },
        }

    def _server_management_status(self) -> dict[str, object]:
        now_epoch = time.time()
        now_monotonic = time.monotonic()
        unique_players = {
            id(player): player
            for player in self.players.values()
            if player.connected and not player.is_ai
        }.values()
        players = [
            {
                "target": clean_console_text(player.target)[:128],
                "playerId": public_player_id(player.identity_key),
                "name": plain_console_text(player.display_name).strip()[:128],
                "authName": plain_console_text(player.auth_name or "").strip()[:128],
                "active": bool(player.active),
                "alive": bool(player.alive),
                "afk": bool(player.afk),
                "activityAgeSeconds": round(
                    max(0.0, now_monotonic - player.last_activity_monotonic), 1
                ) if player.last_activity_monotonic is not None else None,
            }
            for player in unique_players
        ]
        players.sort(key=lambda item: (
            not bool(item["active"]), str(item["name"]).casefold()
        ))
        queued = []
        for position, key in enumerate(list(self.queue)[:25], 1):
            entry = self.repository.catalog.get(key)
            queued.append({
                "position": position,
                "mapKey": key,
                "name": self._display_map_name(entry) if entry else key,
                "author": entry.author if entry else "Unknown",
                "version": entry.version if entry else "",
            })
        try:
            disk = shutil.disk_usage(self.store.path.parent)
            disk_free = int(disk.free)
            disk_total = int(disk.total)
        except OSError:
            disk_free = 0
            disk_total = 0
        try:
            database_bytes = int(self.store.path.stat().st_size)
        except OSError:
            database_bytes = 0
        try:
            load_one, load_five, load_fifteen = os.getloadavg()
        except OSError:
            load_one = load_five = load_fifteen = 0.0
        game_event_age = (
            max(0.0, now_monotonic - self.last_game_monotonic)
            if self.last_game_monotonic is not None else None
        )
        federation_age = (
            max(0.0, now_monotonic - self.federation_last_received_monotonic)
            if self.federation_last_received_monotonic > 0 else None
        )
        return {
            "region": str(
                self.config.get("live_dashboard", {}).get("local_region", "")
            )[:16],
            "online": True,
            "role": self.federation_role,
            "pid": os.getpid(),
            "startedAt": int(self.started_at_epoch * 1000),
            "uptimeSeconds": max(0, int(now_epoch - self.started_at_epoch)),
            "roundActive": self._round_is_active(),
            "transitioning": bool(self.transitioning),
            "finalCountdownActive": bool(self.final_countdown_active),
            "controllerReloadPending": bool(self.controller_reload_state.get("pending")),
            "respawnsPaused": bool(self.respawns_paused),
            "timeRemainingSeconds": max(
                0, int((self.deadline_epoch or now_epoch) - now_epoch)
            ),
            "roundStartedAt": int(self.round_started_epoch * 1000)
            if self.round_started_epoch else None,
            "map": self._dashboard_map_metadata(self.current),
            "nextMap": self._dashboard_map_metadata(self._peek_next()),
            "players": players,
            "playerCount": len(players),
            "activePlayerCount": sum(bool(player["active"]) for player in players),
            "alivePlayerCount": sum(bool(player["alive"]) for player in players),
            "queue": queued,
            "queueCount": len(self.queue),
            "rotationRemaining": len(self.rotation),
            "catalogMapCount": len(self.repository.catalog),
            "excludedMapCount": len(self.excluded_map_keys),
            "catalogVersion": int(self.repository.firebase_catalog_version),
            "federationPeerOnline": bool(self.federation_snapshot_received),
            "federationEventAgeSeconds": round(federation_age, 2)
            if federation_age is not None else None,
            "gameEventAgeSeconds": round(game_event_age, 2)
            if game_event_age is not None else None,
            "consoleAvailable": bool(
                getattr(self, "server_console_available", False)
            ),
            "consoleStreamActive": bool(
                time.monotonic()
                < getattr(self, "server_console_stream_until_monotonic", 0.0)
            ),
            "system": {
                "load1": round(load_one, 3),
                "load5": round(load_five, 3),
                "load15": round(load_fifteen, 3),
                "cpuCount": int(os.cpu_count() or 1),
                "maxResidentBytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                * 1024,
                "databaseBytes": database_bytes,
                "diskFreeBytes": disk_free,
                "diskTotalBytes": disk_total,
            },
        }

    @staticmethod
    def _server_management_field(
        command: dict[str, object],
        name: str,
        maximum: int,
    ) -> str:
        return clean_console_text(command.get(name, "")).strip()[:maximum]

    def _server_management_player(self, command: dict[str, object]) -> Player:
        uid = re.sub(
            r"[^A-Za-z0-9_-]", "_",
            self._server_management_field(command, "requestedBy", 128),
        )[:64] or "admin"
        name = plain_console_text(
            self._server_management_field(command, "requestedName", 80)
        ).strip() or "Web admin"
        return Player(f"web-admin-{uid}", name, auth_name=name, active=False)

    def _require_federation_authority(self) -> None:
        if self.federation_follower:
            raise ValueError("Map rotation is controlled by the federation leader.")

    @staticmethod
    def _sanitize_server_console_line(value: object) -> str:
        text = plain_console_text(value).replace("\x00", "").strip()
        text = re.sub(
            r"^\[\d{4}/\d{2}/\d{2}-\d{2}:\d{2}:\d{2}\]\s*",
            "",
            text,
        )
        if not text:
            return ""
        if SERVER_CONSOLE_SENSITIVE_RE.search(text):
            return "[sensitive console output withheld]"
        return text[:600]

    def _record_server_console_line(self, value: object) -> None:
        message = self._sanitize_server_console_line(value)
        if not message:
            return
        self.server_console_sequence += 1
        self.server_console_entries.append({
            "sequence": self.server_console_sequence,
            "at": int(time.time() * 1000),
            "message": message,
        })

    async def follow_server_console(self) -> None:
        live_config = self.config.get("live_dashboard", {})
        if (
            self.live_dashboard_chat is None
            or not isinstance(live_config, dict)
            or live_config.get("management_enabled") is not True
        ):
            return
        await self.sink.send("CONSOLE_LOG 1")
        path = self.server_console_path
        handle = None
        inode = None
        first_open = True
        while not self.stop_event.is_set():
            try:
                stat = path.stat()
                if (
                    handle is None
                    or inode != stat.st_ino
                    or handle.tell() > stat.st_size
                ):
                    if handle:
                        handle.close()
                    handle = path.open("rb")
                    if first_open and stat.st_size > 131_072:
                        handle.seek(stat.st_size - 131_072)
                        handle.readline()
                    inode = stat.st_ino
                    first_open = False
                self.server_console_available = True
                raw_line = handle.readline()
                if raw_line:
                    self._record_server_console_line(
                        self._decode_game_bytes(raw_line, "server console")
                    )
                    continue
                if (
                    stat.st_size > SERVER_CONSOLE_MAX_FILE_BYTES
                    and handle.tell() >= stat.st_size
                ):
                    handle.close()
                    handle = None
                    inode = None
                    path.write_bytes(b"")
            except FileNotFoundError:
                self.server_console_available = False
            except OSError:
                self.server_console_available = False
                LOG.exception("following the game console log failed")
            await asyncio.sleep(0.05)
        if handle:
            handle.close()

    async def _publish_server_console(self, dashboard, server_id: str) -> None:
        if (
            time.monotonic()
            >= getattr(self, "server_console_stream_until_monotonic", 0.0)
        ):
            return
        last_sequence = getattr(
            self, "server_console_last_published_sequence", 0
        )
        entries = [
            dict(entry)
            for entry in getattr(self, "server_console_entries", ())
            if int(entry.get("sequence", 0)) > last_sequence
        ][:SERVER_CONSOLE_BATCH_SIZE]
        if not entries:
            return
        await asyncio.to_thread(
            dashboard.publish_admin_console, server_id, entries
        )
        self.server_console_last_published_sequence = int(
            entries[-1]["sequence"]
        )

    async def _execute_server_management_command(
        self,
        command: dict[str, object],
    ) -> tuple[str, dict[str, object]]:
        command_type = self._server_management_field(command, "type", 64)
        if command_type not in SERVER_MANAGEMENT_COMMANDS:
            raise ValueError("That server-management command is not supported.")
        actor = self._server_management_player(command)
        target = self._server_management_field(command, "target", 128)
        message = self._server_management_field(command, "message", 512)
        reason = self._server_management_field(command, "reason", 240)
        map_key = self._server_management_field(command, "mapKey", 1024)

        if command_type == "announce":
            if not message:
                raise ValueError("Enter an announcement.")
            federate = command.get("scope") == "federation"
            if federate and not self.federation_leader:
                raise ValueError("Federation-wide announcements must target the leader.")
            await self.broadcast(message, federate=federate)
            return "Announcement delivered.", {"scope": "federation" if federate else "local"}

        if command_type == "start_console_stream":
            now = time.monotonic()
            if now >= getattr(
                self, "server_console_stream_until_monotonic", 0.0
            ):
                sequence = getattr(self, "server_console_sequence", 0)
                self.server_console_last_published_sequence = max(
                    0, sequence - SERVER_CONSOLE_INITIAL_LINES
                )
            self.server_console_stream_until_monotonic = max(
                getattr(self, "server_console_stream_until_monotonic", 0.0),
                now + SERVER_CONSOLE_STREAM_SECONDS,
            )
            return (
                "Live console stream enabled for 90 seconds.",
                {"streamSeconds": int(SERVER_CONSOLE_STREAM_SECONDS)},
            )

        if command_type == "direct_message":
            player = self.player_for(target)
            if player is None or not player.connected:
                raise ValueError("That player is no longer connected to this server.")
            if not message:
                raise ValueError("Enter a private message.")
            await self.private(player, message)
            return f"Message delivered to {plain_console_text(player.display_name)}.", {}

        if command_type in {"kick", "ban", "silence", "voice", "kill"}:
            player = self.player_for(target)
            if player is None or not player.connected:
                raise ValueError("That player is no longer connected to this server.")
            game_target = quote_console(player.target)
            display_name = plain_console_text(player.display_name).strip()
            if command_type == "kick":
                await self.sink.send(
                    f"KICK {game_target} {readline_console_text(reason or 'Removed by a server administrator')}"
                )
            elif command_type == "ban":
                duration = max(1, min(int(command.get("durationMinutes", 60) or 60), 10_080))
                await self.sink.send(
                    f"BAN {game_target} {duration} {readline_console_text(reason or 'Banned by a server administrator')}"
                )
                return f"Banned {display_name} for {duration} minutes.", {"durationMinutes": duration}
            elif command_type == "silence":
                await self.sink.send(f"SILENCE {game_target}")
            elif command_type == "voice":
                await self.sink.send(f"VOICE {game_target}")
            else:
                await self.sink.send(f"KILL {game_target}")
            past = {
                "kick": "Kicked", "silence": "Silenced",
                "voice": "Unsilenced", "kill": "Killed",
            }[command_type]
            return f"{past} {display_name}.", {}

        if command_type in {"queue_map", "remove_queued_map", "change_map"}:
            self._require_federation_authority()
            entry = self.repository.find_by_spec(map_key)
            if entry is None or entry.key not in self.repository.catalog:
                raise ValueError("That map is not in the active server catalog.")
            if command_type == "remove_queued_map":
                if entry.key not in self.queue:
                    raise ValueError("That map is not currently queued.")
                self.queue.remove(entry.key)
                self._save_rotation()
                return f"Removed {self._display_map_name(entry)} from the queue.", {}
            if self.current and entry.key == self.current.key:
                raise ValueError("That map is already active.")
            with contextlib.suppress(ValueError):
                self.queue.remove(entry.key)
            if command_type == "change_map":
                self.queue.appendleft(entry.key)
                self._save_rotation()
                await self.broadcast(
                    f"{actor.record_name} selected {self._display_map_name(entry)} by {entry.author}."
                )
                await self.activate_next_map("admin web console")
                return f"Changing to {self._display_map_name(entry)}.", {"mapKey": entry.key}
            self.queue.append(entry.key)
            self._save_rotation()
            await self.broadcast(
                f"{actor.record_name} queued {self._display_map_name(entry)} by {entry.author} "
                f"(position {len(self.queue)})."
            )
            return f"Queued {self._display_map_name(entry)} at position {len(self.queue)}.", {"mapKey": entry.key}

        if command_type == "clear_queue":
            self._require_federation_authority()
            count = len(self.queue)
            self.queue.clear()
            self._save_rotation()
            if count:
                await self.broadcast(f"{actor.record_name} cleared {count} map(s) from the queue.")
            return f"Cleared {count} queued map(s).", {"removed": count}

        if command_type == "force_skip":
            self._require_federation_authority()
            if self.transitioning:
                raise ValueError("A map change is already in progress.")
            self._clear_vote("skip")
            await self.broadcast(f"{actor.record_name} force-skipped the map.")
            await self.activate_next_map("admin web console force skip")
            return "Advanced to the next map.", {}

        if command_type == "end_map":
            self._require_federation_authority()
            if not self.current or not self._round_is_active():
                raise ValueError("There is no active map to end.")
            if self.transitioning or self.final_countdown_active:
                raise ValueError("A map transition or final countdown is already active.")
            self.final_countdown_announcement = None
            self.deadline_epoch = time.time()
            self.store.set_json("deadline_epoch", self.deadline_epoch)
            return "Started the end-of-map sequence.", {}

        if command_type == "reload_maps":
            async with self.map_lock:
                before = set(self.repository.catalog)
                await asyncio.to_thread(self.repository.sync, True)
                self._reconcile_rotation()
                after = set(self.repository.catalog)
            return (
                f"Reloaded {len(after)} maps ({len(after - before)} added, "
                f"{len(before - after)} removed).",
                {"mapCount": len(after), "added": len(after - before), "removed": len(before - after)},
            )

        if command_type == "restart_round":
            if self.transitioning:
                raise ValueError("A map transition is already in progress.")
            self._reset_attempts()
            await self.sink.send("START_NEW_MATCH", "KILL_ALL")
            await self.broadcast(
                f"{actor.record_name} restarted the round.", federate=False
            )
            return "Round restart requested.", {}

        if command_type == "set_engine_option":
            option = self._server_management_field(command, "option", 64).upper()
            if option not in SERVER_MANAGEMENT_ENGINE_OPTIONS:
                raise ValueError("That engine option is not available in the web console.")
            try:
                value = float(command.get("value"))
            except (TypeError, ValueError) as exc:
                raise ValueError("Enter a numeric engine-option value.") from exc
            minimum, maximum = SERVER_MANAGEMENT_ENGINE_OPTIONS[option]
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(f"{option} must be between {minimum:g} and {maximum:g}.")
            rendered = f"{value:.9g}"
            await self.sink.send(f"{option} {rendered}")
            return f"Set {option} to {rendered} until the server is restarted or reconfigured.", {"option": option, "value": value}

        if command_type == "reload_controller":
            if not self.request_controller_reload(actor.record_name):
                raise ValueError("A graceful controller reload is already pending.")
            return "Graceful controller reload scheduled after active runs finish.", {}

        raise ValueError("That server-management command is not implemented.")

    async def server_management_worker(self) -> None:
        live_config = self.config.get("live_dashboard", {})
        dashboard = self.live_dashboard_chat
        if (
            dashboard is None
            or not isinstance(live_config, dict)
            or live_config.get("management_enabled") is not True
        ):
            return
        server_id = self.federation_local_server_id
        next_status = 0.0
        next_prune = 0.0
        while not self.stop_event.is_set():
            try:
                monotonic_now = time.monotonic()
                if monotonic_now >= next_status:
                    status = self._server_management_status()
                    await asyncio.to_thread(
                        dashboard.publish_admin_status, server_id, status
                    )
                    next_status = monotonic_now + 15.0
                commands = await asyncio.to_thread(
                    dashboard.queued_admin_commands, server_id
                )
                for command_id, command in commands:
                    expires_at = int(command.get("expiresAt", 0) or 0)
                    if expires_at <= int(time.time() * 1000):
                        await asyncio.to_thread(
                            dashboard.update_admin_command,
                            server_id,
                            command_id,
                            "expired",
                            result="Command expired before the server could safely execute it.",
                        )
                        continue
                    await asyncio.to_thread(
                        dashboard.update_admin_command,
                        server_id,
                        command_id,
                        "running",
                        result="Server accepted the command.",
                    )
                    try:
                        result, details = await self._execute_server_management_command(command)
                    except Exception as exc:
                        LOG.warning(
                            "admin command failed: server=%s id=%s type=%s error=%s",
                            server_id, command_id, command.get("type"), exc,
                        )
                        await asyncio.to_thread(
                            dashboard.update_admin_command,
                            server_id,
                            command_id,
                            "failed",
                            result=str(exc),
                        )
                    else:
                        LOG.info(
                            "admin command completed: server=%s id=%s type=%s actor=%s",
                            server_id, command_id, command.get("type"),
                            command.get("requestedBy"),
                        )
                        await asyncio.to_thread(
                            dashboard.update_admin_command,
                            server_id,
                            command_id,
                            "succeeded",
                            result=result,
                            details=details,
                        )
                await self._publish_server_console(dashboard, server_id)
                if monotonic_now >= next_prune:
                    await asyncio.to_thread(
                        dashboard.prune_admin_commands, server_id
                    )
                    next_prune = monotonic_now + 300.0
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("server management worker failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=2.0)
            except TimeoutError:
                pass

    def _dashboard_maps_by_record_key(self) -> dict[str, dict[str, object]]:
        return {
            map_records_key(entry): {
                "mapId": entry.map_id,
                "name": entry.name,
                "author": entry.author,
                "version": entry.version,
                "storagePath": entry.storage_path,
            }
            for entry in self.repository.catalog.values()
            if entry.storage_path
        }

    async def live_dashboard_publisher(self) -> None:
        if self.live_dashboard is None:
            return
        next_leaderboards = 0.0
        while not self.stop_event.is_set():
            try:
                state = self._dashboard_live_state()
                await asyncio.to_thread(self.live_dashboard.publish_live, state)
                replay_writes = await asyncio.to_thread(
                    self.live_dashboard.publish_replay_batch,
                    self.federation_local_server_id,
                )
                if replay_writes:
                    LOG.info("published %d racing replay(s)", replay_writes)
                now = time.monotonic()
                if now >= next_leaderboards:
                    rows = self.store.dashboard_record_rows()
                    maps = self._dashboard_maps_by_record_key()
                    writes = await asyncio.to_thread(
                        self.live_dashboard.publish_leaderboards,
                        rows,
                        maps,
                    )
                    if writes:
                        self.store.set_json(
                            "live_dashboard_leaderboard_hashes",
                            self.live_dashboard.leaderboard_hashes,
                        )
                        self.store.set_json(
                            "live_dashboard_profile_hashes",
                            self.live_dashboard.profile_hashes,
                        )
                        self.store.set_json(
                            "live_dashboard_map_catalog",
                            self.live_dashboard.map_catalog,
                        )
                        LOG.info("published %d live dashboard leaderboard(s)", writes)
                    next_leaderboards = now + 60.0
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("unable to publish live racing dashboard")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=5.0)
            except TimeoutError:
                pass

    async def live_dashboard_follower_replays(self) -> None:
        dashboard = self.live_dashboard_chat
        if dashboard is None or self.live_dashboard is not None:
            return
        while not self.stop_event.is_set():
            try:
                writes = await asyncio.to_thread(
                    dashboard.publish_replay_batch,
                    self.federation_local_server_id,
                )
                if writes:
                    LOG.info("published %d follower racing replay(s)", writes)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("unable to publish follower racing replays")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=5.0)
            except TimeoutError:
                pass

    async def broadcast_block(
        self,
        lines: Iterable[object],
        federate: bool = True,
    ) -> None:
        lines = list(lines)
        styled = style_console_block(lines)
        if styled:
            await self.sink.send(
                f"CONSOLE_MESSAGE {readline_console_block(styled)}"
            )
            if federate:
                await self._publish_federation_message(
                    "broadcast_block", lines=lines
                )

    async def private(self, player: Player | str, message: str) -> None:
        if isinstance(player, Player) and player.federation_server_id:
            await self._publish_federation_message(
                "private", message=message, player=player
            )
            return
        target = player.target if isinstance(player, Player) else player
        if not target or any(ch.isspace() for ch in target):
            return
        styled = style_console_message(message)
        await self.sink.send(f"PLAYER_MESSAGE {target} {quote_console(styled)}")

    async def private_block(
        self,
        player: Player | str,
        lines: Iterable[object],
    ) -> None:
        lines = list(lines)
        if isinstance(player, Player) and player.federation_server_id:
            await self._publish_federation_message(
                "private_block", lines=lines, player=player
            )
            return
        target = player.target if isinstance(player, Player) else player
        if not target or any(ch.isspace() for ch in target):
            return
        styled = style_console_block(lines)
        if styled:
            await self.sink.send(
                f"PLAYER_MESSAGE {target} {quote_console_block(styled)}"
            )

    async def center_private(self, player: Player, message: str) -> None:
        if player.federation_server_id:
            await self._publish_federation_message(
                "center_private", message=message, player=player
            )
            return
        if not player.target or any(ch.isspace() for ch in player.target):
            return
        styled = style_console_message(message)
        await self.sink.send(
            f"CENTER_PLAYER_MESSAGE {player.target} {quote_console(styled)}"
        )

    async def center_broadcast(self, message: object) -> None:
        await self.sink.send(padded_center_command(message))
        await self._publish_federation_message("center", message=str(message))

    def _checkpoint_center_text(
        self,
        player: Player,
        prefix: str = COLOR_RESET,
    ) -> str:
        required = tuple(getattr(self.current, "checkpoint_ids", ()) or ())
        if not required:
            return ""
        collected = player.checkpoints_collected
        if player.pending_respawn_kind == "spawn":
            collected = set()
        elif (
            player.pending_respawn_kind == "checkpoint"
            and player.checkpoint_snapshot is not None
        ):
            collected = set(player.checkpoint_snapshot.checkpoints_collected)
        count = sum(checkpoint_id in collected for checkpoint_id in required)
        return f"{prefix}{CHECKPOINT_CENTER_GAP}{count}/{len(required)}"

    async def _show_checkpoint_progress(
        self,
        player: Player,
        prefix: str = COLOR_RESET,
    ) -> bool:
        if not player.target or any(ch.isspace() for ch in player.target):
            return False
        message = self._checkpoint_center_text(player, prefix)
        if not message:
            return False
        await self.sink.send(
            f"CENTER_PLAYER_MESSAGE {player.target} "
            f"{quote_console_exact(message)}"
        )
        return True

    def _checkpoint_color_reset_commands(self, player: Player) -> tuple[str, ...]:
        if (
            not getattr(self.current, "checkpoint_ids", ())
            or not player.target
            or any(ch.isspace() for ch in player.target)
        ):
            return ()
        return (f"RESET_CHECKPOINT_PLAYER_COLORS {player.target}",)

    async def _show_go(self, player: Player) -> None:
        """Show the padded release cue, then erase it one second later."""
        if not player.target or any(ch.isspace() for ch in player.target):
            return
        if not await self._show_checkpoint_progress(player, "GO!"):
            cue = f"     {COLOR_SUCCESS}GO!{COLOR_RESET}     "
            await self.sink.send(
                f"CENTER_PLAYER_MESSAGE {player.target} "
                f"{quote_console_exact(cue)}"
            )
        old_task = self.center_clear_tasks.pop(id(player), None)
        if old_task:
            old_task.cancel()
        generation = player.generation
        self.center_clear_tasks[id(player)] = asyncio.create_task(
            self._clear_go(player, generation)
        )

    async def _clear_go(self, player: Player, generation: int) -> None:
        try:
            await asyncio.sleep(float(self.config.get("go_message_seconds", 1.0)))
            if (
                generation == player.generation
                and player.connected
                and player.active
            ):
                if not await self._show_checkpoint_progress(player):
                    await self.center_private(player, "")
        except asyncio.CancelledError:
            raise
        finally:
            current = self.center_clear_tasks.get(id(player))
            if current is asyncio.current_task():
                self.center_clear_tasks.pop(id(player), None)

    def player_for(self, name: str, create: bool = False) -> Player | None:
        player = self.aliases.get(name.casefold()) or self.players.get(name.casefold())
        if not player and create:
            player = Player(name, name)
            player.start_mode = str(
                getattr(self, "start_preferences", {}).get(
                    player.identity_key,
                    "immediate",
                )
            ).casefold()
            self.players[name.casefold()] = player
            self.aliases[name.casefold()] = player
        return player

    def register_alias(self, player: Player, name: str) -> None:
        if name:
            self.aliases[name.casefold()] = player

    def estimate_game_time(self) -> float | None:
        if self.last_game_time is None or self.last_game_monotonic is None:
            return None
        return self.last_game_time + max(0.0, time.monotonic() - self.last_game_monotonic)

    def _round_started_after_transition(self, payload: str) -> bool:
        """Identify a target-round event that arrived before CURRENT_MAP."""
        started = getattr(self, "transition_started_epoch", None)
        if started is None:
            return False
        try:
            event_time = datetime.datetime.strptime(
                payload[:19], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=datetime.timezone.utc).timestamp()
        except (TypeError, ValueError):
            return False
        # Ladderlog timestamps have one-second precision.
        return event_time >= math.floor(started)

    async def _handle_round_started(self, payload: str) -> None:
        round_was_active = self._round_is_active()
        if self.transitioning and not self.transition_map_confirmed:
            # CURRENT_MAP is emitted while the target grid is created, before
            # ROUND_STARTED. If that writer was disabled in an older config,
            # remember an event produced after this transition began and apply
            # it as soon as the explicit map probe confirms the target.
            if self._round_started_after_transition(payload):
                self.transition_round_started_pending = True
                LOG.info(
                    "deferring ROUND_STARTED until map confirmation: %s",
                    self.transition_target_key or "unknown",
                )
            else:
                LOG.info(
                    "ignoring stale ROUND_STARTED while waiting for map: %s",
                    self.transition_target_key or "unknown",
                )
            self.round_active = False
            return
        current_key = self.current.key if self.current else None
        if (
            not self.transitioning
            and current_key
            and getattr(self, "round_started_map_key", None) == current_key
            and not getattr(self, "federation_follower", False)
        ):
            # Native Armagetron must never replay a controller-managed map.
            LOG.error("native server attempted to repeat active map: %s", current_key)
            self.round_active = False
            await self.activate_next_map("native repeated the active map")
            return
        self.round_active = True
        if current_key:
            self._set_round_started_map(current_key)
        if self.transitioning:
            # Count the play window from when the selected map is playable.
            self.round_started_epoch = time.time()
            self.deadline_epoch = (
                self.round_started_epoch + self._map_open_play_seconds()
            )
            self.store.set_json("deadline_epoch", self.deadline_epoch)
            self.store.set_json("round_started_epoch", self.round_started_epoch)
            self._complete_map_transition()
        if not round_was_active:
            self._begin_helpful_message_round()
        for player in self.players.values():
            if (
                player.connected
                and player.active
                and player.alive
                and not player.pending_respawn
            ):
                self._begin_new_attempt(player, 0.0)
            elif (
                player.connected
                and player.active
                and player.respawn_enabled
                and not player.alive
                and not player.pending_respawn
                and not getattr(self, "respawns_paused", False)
                and id(player) not in self.respawn_tasks
            ):
                self._schedule_respawn(player, delay_seconds=0.1)
        displayed_during_intermission = False
        if self._display_task and self._display_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                displayed_during_intermission = bool(self._display_task.result())
        if not displayed_during_intermission:
            if self._display_task:
                self._display_task.cancel()
            self._display_task = asyncio.create_task(
                self._delayed_round_display(
                    expected_map_key=self.current.key if self.current else None
                )
            )

    async def handle_line(self, raw_line: str) -> None:
        line = raw_line.rstrip("\r\n")
        if not line:
            return
        event = line.split(" ", 1)[0]
        payload = line[len(event):].lstrip()
        try:
            if event == "ENCODING":
                tokens = payload.split()
                if tokens:
                    self._apply_advertised_game_encoding(tokens[0])
            elif event == "GAME_TIME":
                tokens = payload.split()
                if tokens:
                    self.last_game_time = float(tokens[-1])
                    self.last_game_monotonic = time.monotonic()
            elif event == "CURRENT_MAP":
                await self._handle_current_map(payload)
            elif event == "PLAYER_ENTERED_GRID":
                player = await self._handle_player_arrival(payload, True)
                if player:
                    self._publish_dashboard_presence("join", player)
            elif event == "PLAYER_LEAVES_SPECTATORS":
                await self._handle_player_arrival(payload, True)
            elif event == "PLAYER_ENTERED_SPECTATOR":
                player = await self._handle_player_arrival(payload, False)
                if player:
                    self._publish_dashboard_presence("join", player)
            elif event == "PLAYER_JOINS_SPECTATORS":
                await self._handle_player_arrival(payload, False)
            elif event == "PLAYER_AI_ENTERED":
                self._handle_player_ai_entered(payload)
            elif event == "PLAYER_LEFT":
                parts = payload.split(maxsplit=1)
                player = self.player_for(parts[0]) if parts else None
                if player:
                    self._publish_dashboard_presence("leave", player)
                self._handle_player_left(payload)
                await self._resolve_votes_after_eligibility_change()
            elif event == "PLAYER_LOGIN":
                player = self._handle_player_login(payload)
                if player:
                    await self._apply_display_server_tag_preference(player)
            elif event == "PLAYER_LOGOUT":
                player = self._handle_player_logout(payload)
                if player:
                    await self._apply_display_server_tag_preference(player)
            elif event == "PLAYER_RENAMED":
                player = self._handle_player_renamed(payload)
                if player:
                    await self._apply_display_server_tag_preference(player)
            elif event == "PLAYER_COLORED_NAME":
                self._handle_player_colored_name(payload)
            elif event == "CHAT":
                await self._handle_player_activity(payload)
                parts = payload.split(maxsplit=1)
                if len(parts) == 2:
                    player = self.player_for(parts[0])
                    if player and player.connected and not player.is_ai:
                        live_config = self.config.get("live_dashboard", {})
                        self._publish_dashboard_chat(
                            self.federation_local_server_id,
                            str(live_config.get("local_region", "LOCAL"))[:16],
                            player.display_name,
                            parts[1],
                            bool(player.auth_name),
                        )
            elif event == "PLAYER_ACTIVITY":
                await self._handle_player_activity_snapshot(payload)
            elif event == "ONLINE_PLAYER":
                self._handle_online_player(payload)
            elif event == "ONLINE_PLAYERS_ALIVE":
                self._handle_online_status(payload, True)
            elif event == "ONLINE_PLAYERS_DEAD":
                self._handle_online_status(payload, False)
            elif event == "NEW_ROUND":
                self.round_active = False
                if not self._round_is_active():
                    self._cancel_helpful_message()
                for player in self.players.values():
                    player.generation += 1
                    player.pending_respawn = False
                    player.alive = False
                    player.attempt_started_game = None
                    player.respawn_created_game = None
                    self._clear_checkpoint_run(player)
                if self._display_task:
                    self._display_task.cancel()
                expected_map_key = (
                    self.transition_target_key
                    if self.transitioning and self.transition_target_key
                    else self.current.key if self.current else None
                )
                self._display_task = asyncio.create_task(
                    self._delayed_round_display(
                        delay_seconds=float(
                            self.config.get(
                                "round_intermission_display_delay_seconds", 0.0
                            )
                        ),
                        allow_intermission=True,
                        expected_map_key=expected_map_key,
                    )
                )
            elif event == "ROUND_STARTED":
                await self._handle_round_started(payload)
            elif event == "FEDERATION_ROUND_READY":
                await self._handle_local_federation_round_ready(payload)
            elif event in {"ROUND_FINISHED", "ROUND_ENDED", "SHUTDOWN"}:
                self.round_active = False
                if not self._round_is_active():
                    self._cancel_helpful_message()
            elif event == "CYCLE_CREATED":
                self._handle_cycle_created(payload)
            elif event == "CYCLE_RELEASED":
                await self._handle_cycle_released(payload)
            elif event == "CYCLE_DESTROYED":
                await self._handle_cycle_destroyed(payload)
            elif event == "CYCLE_REPLAY_BEGIN":
                self._handle_replay_begin(payload)
            elif event == "CYCLE_REPLAY_STATE":
                self._handle_replay_state(payload)
            elif event == "CYCLE_REPLAY_INPUT":
                self._handle_replay_input(payload)
            elif event == "CYCLE_REPLAY_END":
                self._handle_replay_end(payload)
            elif event == "CYCLE_REPLAY_SETTINGS":
                self._handle_replay_settings(payload)
            elif event == "CHECKPOINT_PLAYER_ENTER":
                await self._handle_checkpoint(payload)
            elif event == "WINZONE_PLAYER_ENTER":
                await self._handle_winzone(payload)
            elif event == "COMMAND":
                await self._handle_command(payload)
        except Exception:
            LOG.exception("error processing ladderlog event: %s", line)

    async def _handle_current_map(self, payload: str) -> None:
        parts = payload.split(maxsplit=2)
        if not parts:
            return
        if len(parts) >= 3:
            with contextlib.suppress(ValueError):
                self.current_size_factor = float(parts[0])
        spec = parts[-1]
        entry = self.repository.find_by_spec(spec)
        if not entry:
            LOG.warning("active map is unavailable to TronnerRacing: %s", spec)
            saved_key = self.store.get_json("current_key", None)
            saved_entry = self.repository.catalog.get(saved_key)
            if saved_entry and not self.restoring_saved_map:
                self.restoring_saved_map = True
                LOG.info("restoring saved map after server restart: %s", saved_key)
                await asyncio.to_thread(self.repository.cache_for_server, saved_entry)
                self._begin_map_transition(saved_entry.key)
                await self.sink.send(
                    f"SIZE_FACTOR {format_size_factor(float(self.config.get('default_size_factor', 0)))}",
                    f"MAP_FILE {quote_console(saved_entry.key)}",
                    "START_NEW_MATCH",
                    "KILL_ALL",
                    "GET_CURRENT_MAP",
                )
            return
        self.restoring_saved_map = False
        previous_key = self.current.key if self.current else None
        if self.transitioning:
            self.transition_observed_key = entry.key
            # A failed load can leave online_players.txt briefly naming the
            # requested map even after the game has fallen back. Every fresh
            # CURRENT_MAP response must therefore be able to revoke a stale
            # confirmation as well as grant one.
            self.transition_map_confirmed = self.transition_target_key == entry.key
        changed = self.current_spec != spec
        self.current = entry
        self.current_spec = spec
        saved_key = self.store.get_json("current_key", None)
        if previous_key and previous_key != entry.key:
            self._set_round_started_map(None)
        if changed or self.deadline_epoch is None:
            if saved_key != entry.key or not self.deadline_epoch:
                self.round_started_epoch = None
                self.deadline_epoch = time.time() + self._map_open_play_seconds(entry)
            self.store.set_json("current_key", entry.key)
            self.store.set_json("deadline_epoch", self.deadline_epoch)
            self.store.set_json("round_started_epoch", self.round_started_epoch)
            self._clear_all_votes()
            self._reset_attempts()
        self._publish_dashboard_map_change(previous_key or "")
        LOG.info("active map: %s", entry.key)
        if (
            self.transitioning
            and self.transition_map_confirmed
            and getattr(self, "transition_round_started_pending", False)
        ):
            LOG.info(
                "completing map transition from deferred ROUND_STARTED: %s",
                entry.key,
            )
            self.transition_round_started_pending = False
            await self._handle_round_started("")
        elif self.transitioning and self.transition_map_confirmed:
            self._adopt_federation_round_start()

    def _handle_player_entered(
        self,
        payload: str,
        active: bool,
        clear_center: bool = True,
    ) -> Player | None:
        parts = payload.split(maxsplit=2)
        if len(parts) < 2:
            return None
        log_name = parts[0]
        # Grid/spectator entry includes an address between the log and display
        # names; team-menu join/leave events contain only the two names.
        display_name = parts[2] if len(parts) > 2 else parts[1]
        player = self.player_for(log_name, create=True)
        assert player
        player.log_name = log_name
        player.display_name = display_name
        player.connected = True
        player.forced_racing = False
        player.active = active
        # Native team-menu state is authoritative for scripted respawning.
        # Joining spectators opts the player out until a later grid/team entry
        # explicitly opts them back in.
        player.respawn_enabled = active
        player.is_ai = False
        self.online_snapshot_misses.pop(id(player), None)
        if not player.active:
            # Immediately stop the current respawn/freeze task and all of its
            # player-scoped output.
            player.alive = False
            # Spectators are already excluded from votes. Clear any racer AFK
            # state silently so entering or leaving spectate never produces an
            # AFK status announcement.
            player.afk = False
            self.finalists.discard(id(player))
            self._cancel_player_freeze(player)
            if clear_center:
                # Erase a countdown number that was already delivered before
                # the spectator event canceled subsequent freeze updates.
                asyncio.create_task(self.center_private(player, ""))
            getattr(self, "extend_votes", set()).discard(player.identity_key)
            getattr(self, "skip_votes", set()).discard(player.identity_key)
            player.suspended_votes.clear()
        self.players[log_name.casefold()] = player
        self.register_alias(player, log_name)
        return player

    async def _handle_player_arrival(
        self,
        payload: str,
        active: bool,
        force_racing: bool = False,
    ) -> Player | None:
        player = self._handle_player_entered(payload, active)
        if not player:
            return None
        if force_racing:
            player.active = True
            player.respawn_enabled = True
            player.forced_racing = True
            player.start_mode = "countdown"

        # Keep a native spectator out of the scripted spawn lifecycle. Leaving
        # spectator mode or entering the grid marks the player active,
        # re-enables respawning, and schedules their first spawn immediately
        # through this same path.
        await self._record_player_activity(player)
        await self._apply_display_server_tag_preference(player)
        await self._resolve_votes_after_eligibility_change()
        if (
            player.active
            and self.round_active
            and not self.transitioning
            and not self.final_countdown_active
            and not getattr(self, "respawns_paused", False)
            and not player.alive
            and not player.pending_respawn
            and id(player) not in self.respawn_tasks
        ):
            self._schedule_respawn(player, delay_seconds=0.0)
        return player

    def _handle_player_left(self, payload: str) -> None:
        parts = payload.split(maxsplit=2)
        if not parts:
            return
        player = self.player_for(parts[0])
        if player:
            self.online_snapshot_misses.pop(id(player), None)
            player.connected = False
            player.active = False
            player.forced_racing = False
            player.alive = False
            self.finalists.discard(id(player))
            self._cancel_player_freeze(player)
            self.command_windows.pop(id(player), None)
            self.command_warning_times.pop(id(player), None)
            getattr(self, "extend_votes", set()).discard(player.identity_key)
            getattr(self, "skip_votes", set()).discard(player.identity_key)
            player.suspended_votes.clear()
            player.afk = False
            player.last_activity_position = None
            player.activity_cycle_alive = False
            player.activity_snapshot_seen = False

    def _handle_player_login(self, payload: str) -> Player | None:
        parts = payload.split(maxsplit=1)
        if len(parts) < 2:
            return None
        player = self.player_for(parts[0], create=True)
        assert player
        previous_identity = player.identity_key
        player.auth_name = parts[1].strip()
        self.register_alias(player, player.auth_name)
        previous_start_mode = getattr(self, "start_preferences", {}).get(
            previous_identity,
            player.start_mode,
        )
        if player.identity_key not in getattr(self, "start_preferences", {}):
            self._set_local_federation_preference(
                "start", player.identity_key, previous_start_mode
            )
        player.start_mode = self.start_preferences.get(
            player.identity_key,
            "immediate",
        )
        for map_key, map_preferences in list(self.spawn_preferences.items()):
            if (
                isinstance(map_preferences, dict)
                and previous_identity in map_preferences
                and player.identity_key not in map_preferences
            ):
                self._set_local_federation_preference(
                    "spawn",
                    player.identity_key,
                    map_preferences[previous_identity],
                    map_key,
                )
        if self.current:
            preferred = self._preferred_spawn_index(player)
            if preferred is not None:
                player.spawn_cursor = preferred
        self._migrate_display_server_tag_preference(previous_identity, player)
        return player

    def _handle_player_ai_entered(self, payload: str) -> None:
        parts = payload.split(maxsplit=1)
        if not parts:
            return
        player = self.player_for(parts[0], create=True)
        assert player
        player.is_ai = True
        player.connected = True
        self.online_snapshot_misses.pop(id(player), None)
        player.active = True
        if len(parts) > 1:
            player.display_name = parts[1]

    def _handle_player_logout(self, payload: str) -> Player | None:
        parts = payload.split()
        if not parts:
            return None
        player = self.player_for(parts[0])
        if player:
            previous_identity = player.identity_key
            player.auth_name = None
            self._migrate_display_server_tag_preference(previous_identity, player)
        return player

    async def _handle_player_activity(self, payload: str) -> None:
        parts = payload.split(maxsplit=1)
        if not parts:
            return
        player = self.player_for(parts[0])
        if player and player.connected and not player.is_ai:
            await self._record_player_activity(player)

    def _handle_player_renamed(self, payload: str) -> Player | None:
        parts = payload.split(maxsplit=4)
        if len(parts) < 4:
            return None
        old_name, new_name = parts[0], parts[1]
        player = self.player_for(old_name, create=True)
        assert player
        previous_identity = player.identity_key
        self.players.pop(player.log_name.casefold(), None)
        player.log_name = new_name
        player.colored_name = None
        if parts[3] == "1":
            player.auth_name = new_name
        if len(parts) > 4:
            player.display_name = parts[4]
        self.players[new_name.casefold()] = player
        self.register_alias(player, old_name)
        self.register_alias(player, new_name)
        self._migrate_display_server_tag_preference(previous_identity, player)
        return player

    def _handle_player_colored_name(self, payload: str) -> None:
        parts = payload.split(maxsplit=1)
        if len(parts) < 2:
            return
        player = self.player_for(parts[0], create=True)
        assert player
        player.colored_name = normalize_console_colors(parts[1])
        self.register_alias(player, parts[0])

    def _handle_online_player(self, payload: str) -> None:
        parts = payload.split()
        if len(parts) < 7:
            return
        player = self.player_for(parts[0], create=True)
        assert player
        with contextlib.suppress(ValueError):
            player.owner_id = int(parts[1])
            red, green, blue = (
                max(0, min(15, int(value))) for value in parts[2:5]
            )
            player.color_code = (
                f"0x{red * 17:02x}{green * 17:02x}{blue * 17:02x}"
            )
        logged_in = parts[6] == "1"
        if logged_in:
            player.auth_name = parts[0]
        player.connected = True
        self.online_snapshot_misses.pop(id(player), None)
        # ONLINE_PLAYER includes a ping field for spectators; only the next
        # field is a native team. Script-forced racers intentionally have no
        # native team and remain active until they explicitly spectate.
        player.active = len(parts) >= 9 or player.forced_racing
        player.is_ai = False
        self.register_alias(player, parts[0])

    def _handle_online_status(self, payload: str, alive: bool) -> None:
        for name in payload.split():
            player = self.player_for(name)
            if not player:
                continue
            player.connected = True
            self.online_snapshot_misses.pop(id(player), None)
            player.alive = alive

    def _handle_replay_begin(self, payload: str) -> None:
        parts = payload.split()
        if len(parts) < 9 or not self.current:
            return
        token, player_name = parts[:2]
        player = self.player_for(player_name)
        if not player or player.is_ai or token in self.replay_captures:
            return
        try:
            game_time, x, y, xdir, ydir, speed = map(float, parts[2:8])
            turns = int(parts[8])
        except (TypeError, ValueError):
            return
        if not all(math.isfinite(value) for value in (game_time, x, y, xdir, ydir, speed)):
            return
        previous_token = self.active_replay_tokens.get(id(player))
        previous = self.replay_captures.get(previous_token or "")
        if previous is not None and previous.outcome == "death":
            previous.outcome = "replaced"
        map_identifier = self.current.map_id or self.current.rating_key
        revision_identifier = self.current.revision_id or self.current.key
        capture = ReplayCapture(
            token=token,
            player_log_name=player.log_name,
            identity_key=player.identity_key,
            username=player.record_name,
            authenticated=bool(player.auth_name),
            map_identifier=map_identifier,
            revision_identifier=revision_identifier,
            resource_key=self.current.key,
            started_at=time.time(),
            spawn_game_time=game_time,
            x=x,
            y=y,
            xdir=xdir,
            ydir=ydir,
            speed=max(0.0, speed),
            initial_turns=max(0, turns),
            size_factor=self.current_size_factor,
            start_mode=self._start_mode_for(player),
            checkpoint_spawn=player.pending_respawn_kind == "checkpoint",
            settings_identifier=(
                parts[9]
                if len(parts) >= 10
                else getattr(self, "active_replay_settings_identifier", None)
            ),
        )
        self.replay_captures[token] = capture
        self.active_replay_tokens[id(player)] = token

    @staticmethod
    def _decode_replay_setting_hex(value: str) -> bytes:
        if value == "-":
            return b""
        if len(value) > 2_000_000 or len(value) % 2:
            raise ValueError("invalid replay setting field length")
        return bytes.fromhex(value)

    def _handle_replay_settings(self, payload: str) -> None:
        parts = payload.split()
        if not parts:
            return
        kind = parts[0]
        assemblies = getattr(self, "replay_settings_assemblies", None)
        if assemblies is None:
            assemblies = self.replay_settings_assemblies = {}
        if kind == "BEGIN" and len(parts) == 4:
            identifier = parts[1]
            try:
                format_version = int(parts[2])
                expected_count = int(parts[3])
            except ValueError:
                return
            if (
                format_version != REPLAY_SETTINGS_FORMAT_VERSION
                or expected_count < 0
                or expected_count > 20_000
            ):
                LOG.warning("unsupported replay settings snapshot: %s", payload)
                return
            assemblies[identifier] = ReplaySettingsAssembly(
                format_version,
                expected_count,
            )
            return
        if kind == "ITEM" and len(parts) == 4:
            assembly = assemblies.get(parts[1])
            if assembly is None or len(assembly.items) >= assembly.expected_count:
                return
            try:
                item = (
                    self._decode_replay_setting_hex(parts[2]),
                    self._decode_replay_setting_hex(parts[3]),
                )
            except ValueError:
                LOG.warning("invalid replay settings item for %s", parts[1])
                assemblies.pop(parts[1], None)
                return
            assembly.items.append(item)
            return
        if kind == "END" and len(parts) == 3:
            identifier = parts[1]
            assembly = assemblies.pop(identifier, None)
            try:
                reported_count = int(parts[2])
            except ValueError:
                return
            if (
                assembly is None
                or reported_count != assembly.expected_count
                or len(assembly.items) != assembly.expected_count
            ):
                LOG.warning("incomplete replay settings snapshot %s", identifier)
                return
            try:
                self.store.add_replay_settings(
                    identifier,
                    assembly.format_version,
                    assembly.items,
                )
            except Exception:
                LOG.exception("unable to save replay settings %s", identifier)
            return
        if kind == "ACTIVE" and len(parts) == 3:
            identifier = parts[1]
            try:
                game_time = float(parts[2])
            except ValueError:
                return
            if not math.isfinite(game_time):
                return
            self.active_replay_settings_identifier = identifier
            for capture in self.replay_captures.values():
                capture.add_settings_transition(game_time, identifier)

    def _handle_replay_state(self, payload: str) -> None:
        parts = payload.split()
        if len(parts) < 9:
            return
        token, state_kind = parts[:2]
        capture = self.replay_captures.get(token)
        if capture is None:
            return
        try:
            game_time, x, y, xdir, ydir, speed = map(float, parts[2:8])
            turns = int(parts[8])
        except (TypeError, ValueError):
            return
        capture.update_state(
            game_time,
            x,
            y,
            xdir,
            ydir,
            speed,
            turns,
            released=state_kind == "release",
        )

    def _handle_replay_input(self, payload: str) -> None:
        parts = payload.split()
        if len(parts) < 3:
            return
        capture = self.replay_captures.get(parts[0])
        if capture is None:
            return
        try:
            game_time = float(parts[1])
        except ValueError:
            return
        capture.add_input(game_time, parts[2])

    def _persist_replay_capture(
        self,
        capture: ReplayCapture,
        ended_at: float | None = None,
    ) -> None:
        try:
            self.store.add_replay(capture, ended_at or time.time())
            availability_changed = (
                capture.outcome == "finish"
                and capture.authenticated
                and self.store.mark_replay_available(
                    capture.resource_key, capture.identity_key
                )
            )
            if (
                availability_changed
                and getattr(self, "federation_role", "off") != "off"
                and self.store.queue_federation_record(
                    getattr(self, "federation_local_server_id", ""),
                    capture.resource_key,
                    capture.identity_key,
                )
            ):
                wakeup = getattr(self, "_federation_record_wakeup", None)
                if wakeup is not None:
                    wakeup.set()
        except Exception:
            LOG.exception("unable to save replay capture %s", capture.token)

    def _handle_replay_end(self, payload: str) -> None:
        parts = payload.split()
        if len(parts) < 3:
            return
        token, player_name = parts[:2]
        capture = self.replay_captures.pop(token, None)
        if capture is None:
            return
        try:
            end_game_time = float(parts[2])
        except ValueError:
            end_game_time = capture.spawn_game_time
        capture.death_reason = " ".join(parts[3:]).strip()
        player = self.player_for(player_name)
        if player is not None:
            capture.update_identity(player)
            if self.active_replay_tokens.get(id(player)) == token:
                self.active_replay_tokens.pop(id(player), None)
        if capture.outcome == "death" and capture.death_reason == "KILL_ALL":
            capture.outcome = "round_end"
        ended_at = capture.started_at + max(
            0.0, end_game_time - capture.spawn_game_time
        )
        self._persist_replay_capture(capture, ended_at)

    def _mark_replay_finish(
        self,
        player: Player,
        seconds: float,
        turns: int | None,
        personal_best: bool,
    ) -> None:
        token = getattr(self, "active_replay_tokens", {}).get(id(player))
        capture = getattr(self, "replay_captures", {}).get(token or "")
        if capture is None:
            return
        capture.update_identity(player)
        capture.outcome = "finish"
        capture.finish_seconds = seconds
        capture.finish_turns = turns
        capture.personal_best = personal_best

    def _handle_cycle_created(self, payload: str) -> None:
        parts = payload.split()
        if len(parts) < 6:
            return
        player = self.player_for(parts[0], create=True)
        assert player
        was_active = player.active
        player.alive = True
        if player.is_ai:
            player.active = True
            return
        if not was_active:
            # A RESPAWN_PLAYER already in the console pipe can race with native
            # spectator selection. Remove that late cycle without restarting
            # its countdown or reactivating the player.
            player.pending_respawn = False
            asyncio.create_task(self.sink.send(f"KILL_SILENT {player.target}"))
            return
        player.active = True
        if (
            getattr(self, "respawns_paused", False)
            or not player.respawn_enabled
            or (
                self.final_countdown_active
                and id(player) not in self.finalists
            )
        ):
            # A late join or a respawn command already in the console pipe must
            # not introduce a new racer after final-chance timing has begun.
            # The same guard keeps /spec effective across normal round starts.
            player.pending_respawn = False
            asyncio.create_task(self.sink.send(f"KILL_SILENT {player.target}"))
            return
        try:
            event_time = float(parts[-1])
        except ValueError:
            return
        if (
            player.checkpoint_respawn_requested
            and player.checkpoint_snapshot is not None
            and player.pending_respawn_kind != "checkpoint"
        ):
            # /cp can overtake an ordinary respawn already waiting in the
            # server's console pipe. Remove that stale spawn; its destruction
            # will schedule the requested checkpoint cycle.
            player.pending_respawn = False
            player.respawn_created_game = None
            asyncio.create_task(self.sink.send(f"KILL_SILENT {player.target}"))
            return
        respawn_kind = player.pending_respawn_kind
        starts_without_hold = player.pending_start_mode in {
            "immediate",
            "respawn",
        }
        if player.pending_respawn and starts_without_hold:
            if (
                respawn_kind != "checkpoint"
                or not self._resume_checkpoint_attempt(player, event_time)
            ):
                self._begin_new_attempt(player, event_time)
            player.pending_respawn = False
            player.respawn_created_game = None
            player.pending_respawn_kind = ""
        elif player.pending_respawn:
            player.respawn_created_game = event_time
        else:
            self._begin_new_attempt(player, event_time)
        if (
            respawn_kind != "checkpoint"
            and self.current
            and self.current.spawns
        ):
            try:
                x, y = float(parts[1]), float(parts[2])
                nearest = min(
                    range(len(self.current.spawns)),
                    key=lambda index: (self.current.spawns[index].x - x) ** 2
                    + (self.current.spawns[index].y - y) ** 2,
                )
                player.last_spawn_index = nearest
                if not player.pending_respawn:
                    preferred = self._preferred_spawn_index(player)
                    player.spawn_cursor = (
                        preferred
                        if preferred is not None
                        else (nearest + 1) % len(self.current.spawns)
                    )
            except ValueError:
                pass

    async def _handle_cycle_released(self, payload: str) -> None:
        parts = payload.split()
        if len(parts) < 2:
            return
        player = self.player_for(parts[0])
        if (
            not player
            or not player.pending_respawn
            or not player.connected
            or not player.active
            or not player.respawn_enabled
            or getattr(self, "respawns_paused", False)
            or (
                self.final_countdown_active
                and id(player) not in self.finalists
            )
            or self.transitioning
        ):
            return
        try:
            event_time = float(parts[-1])
        except ValueError:
            return
        await self._record_player_activity(player)
        respawn_kind = player.pending_respawn_kind
        if (
            respawn_kind != "checkpoint"
            or not self._resume_checkpoint_attempt(player, event_time)
        ):
            self._begin_new_attempt(player, event_time)
        player.pending_respawn = False
        player.respawn_created_game = None
        player.pending_respawn_kind = ""
        # Brake-start uses a persistent targeted center message. Explicitly
        # replace it with an empty one as soon as the held cycle is released.
        await self.center_private(player, "")
        if player.pending_start_mode == "countdown":
            await self._show_go(player)

    async def _handle_cycle_destroyed(self, payload: str) -> None:
        parts = payload.split()
        if not parts:
            return
        player = self.player_for(parts[0])
        if not player:
            return
        player.alive = False
        if player.is_ai:
            return
        held_cycle_destroyed = (
            player.pending_respawn and player.respawn_created_game is not None
        )
        if player.pending_respawn and not held_cycle_destroyed:
            # This is the old cycle disappearing after its replacement command
            # was queued; CYCLE_CREATED has not confirmed the held cycle yet.
            return
        player.checkpoints_collected.clear()
        player.checkpoint_notice_monotonic = None
        if held_cycle_destroyed:
            player.generation += 1
            player.pending_respawn = False
            player.respawn_created_game = None
            player.pending_respawn_kind = ""
            freeze_task = self.freeze_tasks.pop(id(player), None)
            if freeze_task:
                freeze_task.cancel()
        if (
            not self.round_active
            or self.transitioning
            or self.final_countdown_active
            or getattr(self, "respawns_paused", False)
            or not player.respawn_enabled
            or id(player) in self.respawn_tasks
        ):
            return
        if (
            self._start_mode_for(player) == "respawn"
            and not player.manual_restart_pending
        ):
            await self.center_private(player, "Type /restart to respawn")
            return
        self._schedule_respawn_after_death(
            player,
            empty_arena=not any(
                candidate.connected
                and candidate.active
                and candidate.alive
                for candidate in {id(item): item for item in self.players.values()}.values()
                if candidate is not player
            ),
        )

    def _schedule_respawn_after_death(
        self,
        player: Player,
        empty_arena: bool = False,
    ) -> None:
        if player.manual_restart_pending:
            self._schedule_respawn(player, delay_seconds=0.0)
            return
        explicitly_requested = player.checkpoint_respawn_requested
        if player.checkpoint_snapshot is not None:
            player.checkpoint_respawn_requested = True
            if player.checkpoint_respawn_speed is None:
                player.checkpoint_respawn_speed = player.checkpoint_snapshot.speed
        if explicitly_requested:
            delay_seconds = float(
                self.config.get("checkpoint_respawn_delay_seconds", 0.1)
            )
        else:
            delay_seconds = (
                float(self.config.get("empty_arena_respawn_delay_seconds", 0.1))
                if empty_arena
                else None
            )
        self._schedule_respawn(player, delay_seconds=delay_seconds)

    def _schedule_respawn(
        self, player: Player, delay_seconds: float | None = None
    ) -> None:
        if getattr(self, "respawns_paused", False):
            return
        generation = player.generation
        old_task = self.respawn_tasks.pop(id(player), None)
        if old_task:
            old_task.cancel()
        self.respawn_tasks[id(player)] = asyncio.create_task(
            self._respawn_after_delay(player, generation, delay_seconds)
        )

    async def _respawn_after_delay(
        self,
        player: Player,
        generation: int,
        delay_seconds: float | None = None,
    ) -> None:
        try:
            await asyncio.sleep(
                max(
                    0.0,
                    float(
                        self.config.get("respawn_delay_seconds", 2.0)
                        if delay_seconds is None
                        else delay_seconds
                    ),
                )
            )
            if (
                generation != player.generation
                or not self.round_active
                or not player.connected
                or not player.active
                or player.alive
                or not player.respawn_enabled
                or self.final_countdown_active
                or self.transitioning
                or getattr(self, "respawns_paused", False)
            ):
                return
            await self._respawn_player(player)
        except asyncio.CancelledError:
            raise
        finally:
            current = self.respawn_tasks.get(id(player))
            if current is asyncio.current_task():
                self.respawn_tasks.pop(id(player), None)

    async def _respawn_player(self, player: Player) -> None:
        if (
            not self.current
            or not self.current.spawns
            or not player.connected
            or not player.active
            or not player.respawn_enabled
            or self.final_countdown_active
            or self.transitioning
            or getattr(self, "respawns_paused", False)
        ):
            return
        start_mode = self._start_mode_for(player)
        manual_restart = player.manual_restart_pending
        if start_mode == "respawn" and not manual_restart:
            await self.center_private(player, "Type /restart to respawn")
            return
        player.manual_restart_pending = False
        if player.checkpoint_respawn_requested:
            if player.checkpoint_snapshot is not None:
                await self._respawn_from_checkpoint(player)
                return
            player.checkpoint_respawn_requested = False
            player.checkpoint_respawn_speed = None
        preferred = self._preferred_spawn_index(player)
        spawn_index = (
            preferred
            if preferred is not None
            else player.spawn_cursor % len(self.current.spawns)
        )
        spawn = self.current.spawns[spawn_index]
        if preferred is None:
            player.spawn_cursor = (spawn_index + 1) % len(self.current.spawns)
        else:
            player.spawn_cursor = preferred
        player.generation += 1
        generation = player.generation
        player.pending_respawn = True
        player.respawn_created_game = None
        player.pending_respawn_kind = "spawn"
        player.attempt_started_game = None
        player.pending_start_mode = start_mode
        spawn_arguments = (
            f"{player.target} false {spawn.x:.9g} {spawn.y:.9g} "
            f"{spawn.xdir:.9g} {spawn.ydir:.9g}"
        )
        if start_mode in {"immediate", "respawn"}:
            await self.sink.send(
                *self._checkpoint_color_reset_commands(player),
                f"RESPAWN_PLAYER {spawn_arguments}",
            )
            return
        if start_mode == "countdown":
            await self.sink.send(
                *self._checkpoint_color_reset_commands(player),
                f"RESPAWN_PLAYER_HELD {spawn_arguments}",
                f"FREEZE_PLAYER {player.target} 3",
            )
        else:
            await self.sink.send(
                *self._checkpoint_color_reset_commands(player),
                f"RESPAWN_PLAYER_HELD {spawn_arguments}",
            )
        old_task = self.freeze_tasks.pop(id(player), None)
        if old_task:
            old_task.cancel()
        wait_for_start = (
            self._wait_for_countdown_start
            if start_mode == "countdown"
            else self._wait_for_brake_start
        )
        self.freeze_tasks[id(player)] = asyncio.create_task(
            wait_for_start(player, generation)
        )

    async def _respawn_from_checkpoint(self, player: Player) -> None:
        snapshot = player.checkpoint_snapshot
        if snapshot is None:
            player.checkpoint_respawn_requested = False
            player.checkpoint_respawn_speed = None
            return
        player.generation += 1
        generation = player.generation
        player.pending_respawn = True
        player.respawn_created_game = None
        player.pending_respawn_kind = "checkpoint"
        player.attempt_started_game = None
        start_mode = self._start_mode_for(player)
        player.pending_start_mode = start_mode
        speed = (
            snapshot.speed
            if player.checkpoint_respawn_speed is None
            else player.checkpoint_respawn_speed
        )
        spawn_arguments = (
            f"{player.target} false {snapshot.x:.9g} {snapshot.y:.9g} "
            f"{snapshot.xdir:.9g} {snapshot.ydir:.9g} "
            f"{speed:.9g} {snapshot.turns}"
        )
        if start_mode == "immediate":
            await self.sink.send(
                f"RESPAWN_PLAYER_CHECKPOINT {spawn_arguments}",
            )
            return
        if start_mode == "countdown":
            await self.sink.send(
                f"RESPAWN_PLAYER_CHECKPOINT_HELD {spawn_arguments}",
                f"FREEZE_PLAYER {player.target} 3",
            )
        else:
            await self.sink.send(
                f"RESPAWN_PLAYER_CHECKPOINT_HELD {spawn_arguments}"
            )
        old_task = self.freeze_tasks.pop(id(player), None)
        if old_task:
            old_task.cancel()
        wait_for_start = (
            self._wait_for_countdown_start
            if start_mode == "countdown"
            else self._wait_for_brake_start
        )
        self.freeze_tasks[id(player)] = asyncio.create_task(
            wait_for_start(player, generation)
        )

    async def _wait_for_brake_start(self, player: Player, generation: int) -> None:
        try:
            await self.center_private(
                player,
                "Press brake to start",
            )
            while (
                generation == player.generation
                and player.connected
                and player.active
                and player.respawn_enabled
                and not self.final_countdown_active
                and not self.transitioning
                and not getattr(self, "respawns_paused", False)
                and player.pending_respawn
            ):
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        finally:
            current = self.freeze_tasks.get(id(player))
            if current is asyncio.current_task():
                self.freeze_tasks.pop(id(player), None)

    async def _wait_for_countdown_start(
        self,
        player: Player,
        generation: int,
    ) -> None:
        try:
            for number in (3, 2, 1):
                if not (
                    generation == player.generation
                    and player.connected
                    and player.active
                    and player.respawn_enabled
                    and not self.final_countdown_active
                    and not self.transitioning
                    and not getattr(self, "respawns_paused", False)
                    and player.pending_respawn
                    and player.pending_start_mode == "countdown"
                ):
                    return
                if not await self._show_checkpoint_progress(player, str(number)):
                    await self.center_private(player, str(number))
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        finally:
            current = self.freeze_tasks.get(id(player))
            if current is asyncio.current_task():
                self.freeze_tasks.pop(id(player), None)

    def _missing_checkpoints(self, player: Player) -> tuple[int, ...]:
        required = tuple(getattr(self.current, "checkpoint_ids", ()) or ())
        return tuple(
            checkpoint_id
            for checkpoint_id in required
            if checkpoint_id not in player.checkpoints_collected
        )

    async def _checkpoint_notice(
        self,
        player: Player,
        message: str,
        *,
        throttle: bool = False,
    ) -> None:
        now = time.monotonic()
        if (
            throttle
            and player.checkpoint_notice_monotonic is not None
            and now - player.checkpoint_notice_monotonic < 1.5
        ):
            return
        player.checkpoint_notice_monotonic = now
        await self.private(player, message)

    async def _handle_checkpoint(self, payload: str) -> None:
        parsed = parse_checkpoint_entry(payload)
        required = tuple(getattr(self.current, "checkpoint_ids", ()) or ())
        if not parsed or not required:
            return
        checkpoint_id = parsed.checkpoint_id
        player = self.player_for(parsed.player_name)
        if (
            not player
            or player.is_ai
            or not player.alive
            or player.pending_respawn
            or player.attempt_started_game is None
        ):
            return
        if checkpoint_id not in required or checkpoint_id in player.checkpoints_collected:
            return
        mode = getattr(self.current, "checkpoint_mode", "ordered") or "ordered"
        if mode == "ordered":
            expected = next(
                item
                for item in required
                if item not in player.checkpoints_collected
            )
            if checkpoint_id != expected:
                await self._checkpoint_notice(
                    player,
                    f"Checkpoint {expected} must be collected next.",
                    throttle=True,
                )
                return
        player.checkpoints_collected.add(checkpoint_id)
        player.last_checkpoint_game = parsed.game_time
        segment_started = (
            player.no_cp_segment_started_game
            if player.no_cp_segment_started_game is not None
            else player.attempt_started_game
        )
        segment_seconds = parsed.game_time - segment_started
        if segment_seconds >= 0 and math.isfinite(segment_seconds):
            player.no_cp_elapsed += segment_seconds
            player.no_cp_segment_started_game = parsed.game_time
            if parsed.has_respawn_state:
                assert parsed.x is not None
                assert parsed.y is not None
                assert parsed.xdir is not None
                assert parsed.ydir is not None
                assert parsed.speed is not None
                assert parsed.turns is not None
                player.checkpoint_snapshot = CheckpointSnapshot(
                    checkpoint_id=checkpoint_id,
                    x=parsed.x,
                    y=parsed.y,
                    xdir=parsed.xdir,
                    ydir=parsed.ydir,
                    speed=parsed.speed,
                    turns=parsed.turns,
                    event_game=parsed.game_time,
                    attempt_started_game=player.attempt_started_game,
                    checkpoints_collected=frozenset(
                        player.checkpoints_collected
                    ),
                    no_cp_elapsed=player.no_cp_elapsed,
                )
        await self._record_player_activity(player)
        await self.sink.send(
            f"SET_CHECKPOINT_PLAYER_COLOR {player.target} {checkpoint_id}"
        )
        await self._show_checkpoint_progress(player)

    async def _handle_winzone(self, payload: str) -> None:
        parsed = parse_winzone_finish(payload)
        if not parsed or not self.current:
            return
        map_key = map_records_key(self.current)
        time_decimals = race_time_decimals(self.current)
        player_name, finish_game, turns = parsed
        player = self.player_for(player_name)
        if not player or player.is_ai or not player.alive:
            return
        if player.attempt_started_game is None:
            # Held brake/countdown cycles exist in the arena before takeoff.
            # A spawn inside the win zone therefore emits events before there
            # is a valid attempt to finish; ignore them instead of creating a
            # kill/respawn loop. Completed finishes mark the player dead below,
            # so their repeated zone ticks are already rejected above.
            return
        await self._record_player_activity(player)
        missing_checkpoints = self._missing_checkpoints(player)
        if missing_checkpoints:
            mode = getattr(self.current, "checkpoint_mode", "ordered") or "ordered"
            required_count = len(getattr(self.current, "checkpoint_ids", ()))
            if mode == "ordered":
                message = (
                    f"Finish blocked: collect checkpoint {missing_checkpoints[0]} next "
                    f"({len(player.checkpoints_collected)}/{required_count})."
                )
            else:
                message = (
                    f"Finish blocked: {len(missing_checkpoints)} checkpoint"
                    f"{'s' if len(missing_checkpoints) != 1 else ''} remaining."
                )
            await self._checkpoint_notice(player, message, throttle=True)
            return
        attempt_started_game = player.attempt_started_game
        seconds = finish_game - attempt_started_game
        if seconds < 0 or seconds > float(self.config.get("maximum_record_seconds", 7200)):
            LOG.warning("discarding invalid finish %.6f for %s", seconds, player.record_name)
            token = getattr(self, "active_replay_tokens", {}).get(id(player))
            capture = getattr(self, "replay_captures", {}).get(token or "")
            if capture is not None:
                capture.outcome = "invalid_finish"
            player.attempt_started_game = None
            self._clear_checkpoint_run(player)
            player.alive = False
            await self.sink.send(
                *self._checkpoint_color_reset_commands(player),
                f"KILL_SILENT {player.target}",
            )
            return
        no_cp_seconds: float | None = None
        if (
            player.checkpoint_respawn_used
            and player.no_cp_segment_started_game is not None
        ):
            candidate = (
                player.no_cp_elapsed
                + finish_game
                - player.no_cp_segment_started_game
            )
            if 0 <= candidate <= float(
                self.config.get("maximum_record_seconds", 7200)
            ) and math.isfinite(candidate):
                no_cp_seconds = candidate
        self._clear_checkpoint_run(player)
        # Keep the final-countdown loop from advancing the map after this cycle
        # is marked dead but before its score, record, and message are complete.
        if not hasattr(self, "finishes_in_progress"):
            self.finishes_in_progress = set()
        self.finishes_in_progress.add(id(player))
        player.attempt_started_game = None
        player.alive = False
        try:
            # Native race scoring only recognizes a player's first finish in a
            # round. This controller permits repeated attempts, so score every
            # validated winzone entry here instead.
            await self.sink.send(
                *self._checkpoint_color_reset_commands(player),
                f"ADD_SCORE_PLAYER {player.target} 1",
            )
            record, improved, previous_best, previous_best_turns = (
                self.store.add_finish(map_key, player, seconds, turns)
            )
            if improved and getattr(self, "federation_role", "off") != "off":
                queued_for_federation = self.store.queue_federation_record(
                    self.federation_local_server_id,
                    map_key,
                    player.identity_key,
                )
                if queued_for_federation:
                    wakeup = getattr(self, "_federation_record_wakeup", None)
                    if wakeup is not None:
                        wakeup.set()
            self._mark_replay_finish(player, seconds, turns, improved)
            records = self.store.records(map_key)
            finish_key = (seconds, math.inf if turns is None else turns)
            finish_rank = 1 + sum(
                (
                    item.best_seconds,
                    math.inf if item.best_turns is None else item.best_turns,
                )
                < finish_key
                for item in records
            )
            best_rank = next(
                index
                for index, item in enumerate(records, 1)
                if item.identity_key == record.identity_key
            )
            previous_best_rank: int | None = None
            if improved and previous_best is not None:
                previous_key = (
                    previous_best,
                    math.inf
                    if previous_best_turns is None
                    else previous_best_turns,
                )
                previous_best_rank = 1 + sum(
                    (
                        item.best_seconds,
                        math.inf
                        if item.best_turns is None
                        else item.best_turns,
                    )
                    < previous_key
                    for item in records
                    if item.identity_key != record.identity_key
                )
            no_cp_rank: int | None = None
            if no_cp_seconds is not None:
                no_cp_key = (
                    no_cp_seconds,
                    math.inf if turns is None else turns,
                )
                no_cp_rank = 1 + sum(
                    (
                        item.best_seconds,
                        math.inf
                        if item.best_turns is None
                        else item.best_turns,
                    )
                    < no_cp_key
                    for item in records
                    if item.identity_key != record.identity_key
                )
            LOG.info(
                "finish map=%s username=%s time=%.*f personal_best=%s",
                map_key,
                player.record_name,
                time_decimals,
                seconds,
                improved,
            )
            self._publish_dashboard_finish_activity(
                player,
                seconds=seconds,
                rank=finish_rank,
                turns=turns,
                improved=improved,
                best_seconds=record.best_seconds,
                best_turns=record.best_turns,
                previous_best=previous_best,
                previous_best_turns=previous_best_turns,
                pb_rank=(previous_best_rank if improved else best_rank),
                no_cp_seconds=no_cp_seconds,
                no_cp_rank=no_cp_rank,
            )
            await self.broadcast(
                format_finish_message(
                    player.colored_display_name,
                    seconds,
                    finish_rank,
                    record.best_seconds,
                    best_rank,
                    previous_best,
                    turns,
                    record.best_turns,
                    previous_best_turns,
                    no_cp_seconds,
                    no_cp_rank,
                    turns if no_cp_seconds is not None else None,
                    improved,
                    previous_best_rank,
                    time_decimals,
                ),
            )
        finally:
            self.finalists.discard(id(player))
            self.finishes_in_progress.discard(id(player))
            await self.sink.send(f"KILL_SILENT {player.target}")

    async def _delayed_round_display(
        self,
        delay_seconds: float | None = None,
        allow_intermission: bool = False,
        expected_map_key: str | None = None,
    ) -> bool:
        if delay_seconds is None:
            delay_seconds = float(
                self.config.get("round_display_delay_seconds", 0.35)
            )
        await asyncio.sleep(max(0.0, delay_seconds))
        if allow_intermission and delay_seconds <= 0:
            # NEW_ROUND arrives just before CURRENT_MAP during a map change.
            # A zero-delay table should appear at the earliest safe point: as
            # soon as the target map is confirmed, still before ROUND_STARTED.
            while (
                getattr(self, "transitioning", False)
                and not getattr(self, "transition_map_confirmed", False)
            ):
                await asyncio.sleep(0.01)
        if not self.current:
            return False
        if expected_map_key is not None and self.current.key != expected_map_key:
            return False
        if not self.round_active and not allow_intermission:
            return False
        if getattr(self, "transitioning", False) and not getattr(
            self, "transition_map_confirmed", False
        ):
            return False
        records = self.store.records(map_records_key(self.current))
        ranks = {record.identity_key: (index + 1, record) for index, record in enumerate(records)}
        delivered: set[str] = set()
        recipients: list[Player] = []
        personal_rows: list[
            tuple[str, int | str, str, float | None, int | None]
        ] = []
        for player in list(self.players.values()):
            if (
                player.is_ai
                or not player.connected
                or not player.active
                or player.identity_key in delivered
            ):
                continue
            delivered.add(player.identity_key)
            recipients.append(player)
            row = ranks.get(player.identity_key)
            if row:
                rank, record = row
                personal_rows.append(
                    (
                        player.identity_key,
                        rank,
                        record.username,
                        record.best_seconds,
                        record.best_turns,
                    )
                )
            else:
                personal_rows.append(
                    (player.identity_key, "--", player.record_name, None, None)
                )
        common_lines, private_lines = build_leaderboard_table(
            self._display_map_name(self.current),
            self.current.author,
            records,
            personal_rows,
            axes=self.current.axes,
            rating=self.store.rating_average(self.current.rating_key),
            time_decimals=race_time_decimals(self.current),
        )
        # Send each viewer one complete table so its rows cannot be reordered
        # with that viewer's personal-best row or the status footer.
        footer_lines = common_lines[-2:]
        map_minutes = self._map_play_seconds(self.current) / 60.0
        map_time_text = f"{map_minutes:.2f}".rstrip("0").rstrip(".")
        map_time_line = f"Map time: {map_time_text} minutes"
        for player in recipients:
            await self.private_block(
                player,
                [
                    *common_lines[:-2],
                    *private_lines.get(player.identity_key, []),
                    *footer_lines,
                    map_time_line,
                ],
            )

        # Normally there is at least one active recipient. Preserve the old
        # server-console output when a delayed display outlives every player.
        if not recipients:
            await self.broadcast_block([*common_lines, map_time_line])
        return True

    def active_players(self) -> list[Player]:
        unique: dict[int, Player] = {}
        for player in self.players.values():
            if (
                player.connected
                and player.active
                and player.respawn_enabled
                and not player.is_ai
            ):
                unique[id(player)] = player
        return list(unique.values())

    def eligible_voters(self) -> list[Player]:
        """Return active human racers who currently count toward votes."""
        voters = [player for player in self.active_players() if not player.afk]
        if self.federation_leader:
            for key, item in self.federation_remote_players.items():
                player = self.federation_command_players.get(key)
                if player is None:
                    player = self._federation_command_player(
                        str(item.get("player_id", "")).casefold(),
                        item,
                        str(item.get("_server_id", self.federation_remote_server_id)),
                    )
                if (
                    player.connected
                    and player.active
                    and player.respawn_enabled
                    and not player.afk
                ):
                    voters.append(player)
        return voters

    def _vote_generation(self, vote_name: str) -> int:
        return int(getattr(self, f"{vote_name}_vote_generation", 0))

    def _clear_vote(self, vote_name: str) -> None:
        votes_attribute = f"{vote_name}_votes"
        votes = getattr(self, votes_attribute, None)
        if votes is None:
            votes = set()
            setattr(self, votes_attribute, votes)
        votes.clear()
        setattr(
            self,
            f"{vote_name}_vote_generation",
            self._vote_generation(vote_name) + 1,
        )
        for player in {
            id(item): item for item in getattr(self, "players", {}).values()
        }.values():
            player.suspended_votes.pop(vote_name, None)

    def _clear_all_votes(self) -> None:
        self._clear_vote("extend")
        self._clear_vote("skip")

    def _suspend_player_votes(self, player: Player) -> bool:
        suspended = False
        identity_key = player.identity_key
        for vote_name in ("extend", "skip"):
            votes = getattr(self, f"{vote_name}_votes")
            if identity_key not in votes:
                continue
            votes.remove(identity_key)
            player.suspended_votes[vote_name] = self._vote_generation(vote_name)
            suspended = True
        return suspended

    def _restore_player_votes(self, player: Player) -> bool:
        if (
            not player.connected
            or not player.active
            or not player.respawn_enabled
            or player.is_ai
            or player.afk
            or not getattr(self, "current", None)
            or not self._round_is_active()
            or getattr(self, "transitioning", False)
            or getattr(self, "final_countdown_active", False)
        ):
            return False
        restored = False
        for vote_name in ("extend", "skip"):
            generation = player.suspended_votes.pop(vote_name, None)
            if generation != self._vote_generation(vote_name):
                continue
            getattr(self, f"{vote_name}_votes").add(player.identity_key)
            restored = True
        return restored

    async def _resolve_extend_vote(self) -> bool:
        if not hasattr(self, "extend_votes"):
            self.extend_votes = set()
        if (
            not getattr(self, "current", None)
            or not self._round_is_active()
            or getattr(self, "transitioning", False)
            or getattr(self, "final_countdown_active", False)
        ):
            return False
        voters = self.eligible_voters()
        active_keys = {voter.identity_key for voter in voters}
        self.extend_votes.intersection_update(active_keys)
        required = (
            1
            if len(voters) <= 1
            else extend_votes_required(len(voters))
        )
        if not self.extend_votes or len(self.extend_votes) < required:
            return False
        extension = float(self.config.get("extend_seconds", 300))
        if self.deadline_epoch is None:
            self.deadline_epoch = time.time() + extension
        else:
            self.deadline_epoch += extension
        self.store.set_json("deadline_epoch", self.deadline_epoch)
        self._clear_vote("extend")
        await self.broadcast("Map extended by 5 minutes.")
        return True

    async def _resolve_skip_vote(self) -> bool:
        if not hasattr(self, "skip_votes"):
            self.skip_votes = set()
        if (
            not getattr(self, "current", None)
            or not self._round_is_active()
            or getattr(self, "transitioning", False)
            or getattr(self, "final_countdown_active", False)
            or getattr(self, "final_countdown_announcement", None)
        ):
            return False
        voters = self.eligible_voters()
        active_keys = {voter.identity_key for voter in voters}
        self.skip_votes.intersection_update(active_keys)
        required = 1 if len(voters) <= 1 else skip_votes_required(len(voters))
        if not self.skip_votes or len(self.skip_votes) < required:
            return False
        self._clear_vote("skip")
        # Mark the countdown active immediately so no respawn can slip in
        # before the map timer starts its countdown loop on the next tick.
        self.final_countdown_active = True
        self.final_countdown_end_epoch = None
        self.final_countdown_map_key = self.current.key
        self.final_countdown_announcement = "Skip vote passed."
        self.store.set_json("final_countdown_active", True)
        self.store.set_json("final_countdown_end_epoch", None)
        self.store.set_json("final_countdown_map_key", self.current.key)
        idle_seconds = float(
            getattr(self, "config", {}).get("final_countdown_idle_seconds", 10)
        )
        if idle_seconds > 0:
            await self.sink.send(f"KILL_IDLE_PLAYERS {idle_seconds:.9g}")
        return True

    async def _resolve_votes_after_eligibility_change(self) -> None:
        # Resolve skip first: once its final countdown starts, extend is no
        # longer a valid live vote.
        if await self._resolve_skip_vote():
            self._clear_vote("extend")
            return
        await self._resolve_extend_vote()

    async def _record_player_activity(
        self,
        player: Player,
        activity_time: float | None = None,
    ) -> None:
        player.last_activity_monotonic = (
            time.monotonic() if activity_time is None else activity_time
        )
        if not player.afk:
            return
        player.afk = False
        restored = self._restore_player_votes(player)
        if not player.active or not player.respawn_enabled:
            return
        await self.broadcast(
            f"{player.record_name} is no longer AFK."
            + (" Their active vote was restored." if restored else "")
        )
        await self._resolve_votes_after_eligibility_change()

    async def _set_player_afk(self, player: Player) -> None:
        if (
            player.afk
            or not player.connected
            or not player.active
            or not player.alive
            or not player.respawn_enabled
        ):
            return
        player.afk = True
        suspended = self._suspend_player_votes(player)
        await self.broadcast(
            f"{player.record_name} is now AFK and does not count toward votes."
            + (" Their vote is suspended." if suspended else "")
        )
        await self._resolve_votes_after_eligibility_change()

    async def _check_afk_players(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        timeout = max(1.0, float(self.config.get("afk_timeout_seconds", 60)))
        players = {
            id(item): item for item in getattr(self, "players", {}).values()
        }.values()
        for player in players:
            last_activity = player.last_activity_monotonic
            if (
                player.connected
                and player.active
                and player.alive
                and player.respawn_enabled
                and not player.is_ai
                and not player.afk
                and last_activity is not None
                and now - last_activity >= timeout
            ):
                await self._set_player_afk(player)

    async def _handle_player_activity_snapshot(self, payload: str) -> None:
        parts = payload.split()
        if len(parts) < 5:
            return
        player = self.player_for(parts[0])
        if not player or not player.connected or player.is_ai:
            return
        try:
            native_idle_seconds = max(0.0, float(parts[1]))
            cycle_alive = bool(int(parts[2]))
            position = (float(parts[3]), float(parts[4]))
        except ValueError:
            return
        now = time.monotonic()
        candidate_activity = now - native_idle_seconds
        previous_position = player.last_activity_position
        was_alive = player.activity_cycle_alive
        first_snapshot = not player.activity_snapshot_seen
        player.activity_snapshot_seen = True
        player.activity_cycle_alive = cycle_alive
        player.last_activity_position = position if cycle_alive else None

        moved = False
        if cycle_alive and previous_position is not None and was_alive:
            epsilon = max(
                0.0,
                float(self.config.get("afk_position_epsilon", 0.01)),
            )
            moved = (
                (position[0] - previous_position[0]) ** 2
                + (position[1] - previous_position[1]) ** 2
                > epsilon**2
            )
        elif cycle_alive and previous_position is None and first_snapshot:
            # Establishing a live position after controller startup or a new
            # cycle receives one grace window; subsequent snapshots must move
            # or contain genuine native input.
            moved = True

        last_activity = player.last_activity_monotonic
        native_input = (
            last_activity is None
            or candidate_activity > last_activity + 0.5
        )
        if moved:
            await self._record_player_activity(player, now)
        elif native_input:
            await self._record_player_activity(player, candidate_activity)

    async def player_activity_monitor(self) -> None:
        interval = max(
            0.25,
            float(self.config.get("afk_poll_interval_seconds", 1.0)),
        )
        while True:
            now = time.monotonic()
            players = {
                id(item): item
                for item in getattr(self, "players", {}).values()
                if item.connected
                and item.active
                and item.alive
                and item.respawn_enabled
                and not item.is_ai
            }.values()
            timeout = max(
                1.0,
                float(self.config.get("afk_timeout_seconds", 60)),
            )
            probe_lead = min(
                timeout,
                max(
                    1.0,
                    float(self.config.get("afk_probe_lead_seconds", 10)),
                ),
            )
            needs_baseline = any(
                not player.activity_snapshot_seen for player in players
            )
            at_risk = any(
                not player.afk
                and (
                    player.last_activity_monotonic is None
                    or now - player.last_activity_monotonic >= timeout - probe_lead
                )
                for player in players
            )
            recovering_afk = any(player.afk for player in players)
            should_probe = (
                now >= self.next_activity_probe_monotonic
                and (needs_baseline or at_risk or recovering_afk)
            )
            if should_probe:
                await self.sink.send("GET_PLAYER_ACTIVITY")
                probe_interval = max(
                    1.0,
                    float(self.config.get("afk_probe_interval_seconds", 5)),
                )
                self.next_activity_probe_monotonic = now + probe_interval
                # Let the ladderlog consumer apply the requested snapshot
                # before evaluating the AFK threshold.
                await asyncio.sleep(min(0.2, interval / 2))
            await self._check_afk_players()
            await asyncio.sleep(interval)

    def _alive_finalists(self) -> list[Player]:
        unique: dict[int, Player] = {}
        for player in self.players.values():
            if (
                id(player) in self.finalists
                and (
                    id(player) in getattr(self, "finishes_in_progress", set())
                    or (player.connected and player.active and player.alive)
                )
            ):
                unique[id(player)] = player
        if self.federation_leader:
            for key in self.federation_finalists:
                item = self.federation_remote_players.get(key)
                if not item:
                    continue
                player = self.federation_command_players.get(key)
                if player is None:
                    player = self._federation_command_player(
                        str(item.get("player_id", "")).casefold(),
                        item,
                        str(item.get("_server_id", self.federation_remote_server_id)),
                    )
                if player.connected and player.active and player.alive:
                    unique[id(player)] = player
        return list(unique.values())

    def _clock_runout_players(self, records: Sequence[Record]) -> list[Player]:
        if not self.config.get("clock_runout_prevention_enabled", True):
            return []
        if self.last_game_time is None:
            return []
        by_identity = {record.identity_key: record for record in records}
        minimum = max(
            0.0,
            float(self.config.get("clock_runout_minimum_seconds", 60)),
        )
        multiplier = max(
            1.0,
            float(self.config.get("clock_runout_personal_best_multiplier", 3)),
        )
        checkpoint_grace = max(
            0.0,
            float(self.config.get("clock_runout_checkpoint_grace_seconds", 20)),
        )
        candidates: list[Player] = []
        for player in self.active_players():
            record = by_identity.get(player.identity_key)
            if (
                record is None
                or not player.alive
                or player.pending_respawn
                or player.attempt_started_game is None
            ):
                continue
            attempt_age = self.last_game_time - player.attempt_started_game
            allowed = max(minimum, record.best_seconds * multiplier)
            if not math.isfinite(attempt_age) or attempt_age < allowed:
                continue
            if (
                player.last_checkpoint_game is not None
                and self.last_game_time - player.last_checkpoint_game
                <= checkpoint_grace
            ):
                continue
            candidates.append(player)
        return candidates

    async def _run_final_countdown(
        self,
        resume: bool = False,
        enforce_clock_runout: bool = False,
    ) -> None:
        if (
            not self.current
            or self.transitioning
            or getattr(self, "controller_reload_draining", False)
        ):
            return
        map_key = self.current.key
        now = time.time()
        records = self.store.records(map_records_key(self.current))
        duration = final_countdown_seconds(records)
        if enforce_clock_runout:
            # The last-run window follows the full respawn-enabled map window.
            # Cap that additional window independently at the configured map
            # maximum (five minutes by default).
            duration = min(duration, self._map_play_seconds(self.current))

        if not resume or not self.final_countdown_end_epoch:
            self.final_countdown_active = True
            self._clear_all_votes()
            self.final_countdown_end_epoch = now + duration
            self.final_countdown_map_key = map_key
            self.store.set_json("final_countdown_active", True)
            self.store.set_json(
                "final_countdown_end_epoch", self.final_countdown_end_epoch
            )
            self.store.set_json("final_countdown_map_key", map_key)
            # Establish the eligible racers before the first await so a held
            # cycle released concurrently with the announcement is retained.
            self.finalists = {
                id(player)
                for player in self.active_players()
                if player.alive and player.respawn_enabled
            }
            self.federation_finalists = {
                key
                for key, item in self.federation_remote_players.items()
                if bool(item.get("connected", True))
                and bool(item.get("active", False))
                and bool(item.get("alive", False))
            }
            runout_players = (
                self._clock_runout_players(records)
                if enforce_clock_runout
                else []
            )
            if runout_players:
                self.finalists.difference_update(map(id, runout_players))
                for player in runout_players:
                    await self.private(
                        player,
                        "Your run exceeded the clock-runout limit for this map.",
                    )
                await self.sink.send(
                    *(f"KILL_SILENT {player.target}" for player in runout_players)
                )
                LOG.info(
                    "clock runout prevention map=%s players=%s",
                    map_key,
                    ",".join(player.identity_key for player in runout_players),
                )
            for task in self.respawn_tasks.values():
                task.cancel()
            self.respawn_tasks.clear()
            for task in self.freeze_tasks.values():
                task.cancel()
            self.freeze_tasks.clear()
            for player in self.players.values():
                if (
                    player.pending_respawn
                    and player.respawn_created_game is None
                ):
                    player.generation += 1
                    player.pending_respawn = False
            announcement = (
                getattr(self, "final_countdown_announcement", None)
                or "Map time expired."
            )
            self.final_countdown_announcement = None
            await self._publish_federation_control(
                "countdown_state",
                {
                    "active": True,
                    "map_key": map_key,
                    "end_epoch": self.final_countdown_end_epoch,
                },
            )
            await self.broadcast(
                f"{announcement} Respawning is disabled. "
                f"Final countdown: {math.ceil(duration)} seconds."
            )
            LOG.info(
                "final countdown map=%s duration=%.3f record=%s",
                map_key,
                duration,
                f"{records[0].best_seconds:.3f}" if records else "none",
            )
        else:
            self.finalists = {
                id(player)
                for player in self.active_players()
                if player.alive and player.respawn_enabled
            }
            self.federation_finalists = {
                key
                for key, item in self.federation_remote_players.items()
                if bool(item.get("connected", True))
                and bool(item.get("active", False))
                and bool(item.get("alive", False))
            }
        last_number: int | None = None
        last_idle_check = 0.0
        while (
            self.final_countdown_active
            and self.current
            and self.current.key == map_key
            and not self.transitioning
            and not getattr(self, "controller_reload_draining", False)
        ):
            if not self._alive_finalists():
                await self.broadcast("All remaining racers are finished.")
                await self.activate_next_map("all racers finished final countdown")
                return
            if getattr(self, "finishes_in_progress", set()):
                await asyncio.sleep(0.01)
                continue
            remaining = float(self.final_countdown_end_epoch or now) - time.time()
            if remaining <= 0:
                await self.center_broadcast("0")
                await self.activate_next_map("final countdown expired")
                return
            number = max(1, math.ceil(remaining))
            if number != last_number:
                await self.center_broadcast(str(number))
                last_number = number
            idle_seconds = float(
                self.config.get("final_countdown_idle_seconds", 10)
            )
            monotonic_now = time.monotonic()
            if idle_seconds > 0 and monotonic_now - last_idle_check >= 1:
                await self.sink.send(
                    f"KILL_IDLE_PLAYERS {idle_seconds:.9g}"
                )
                last_idle_check = monotonic_now
            await asyncio.sleep(0.1)

    async def _handle_command(self, payload: str) -> None:
        parsed = parse_intercepted_command(payload)
        if not parsed:
            return
        command, player_name, access_level, arguments = parsed
        player = self.player_for(player_name)
        if not player:
            # Unknown command senders may be spectators missed during a
            # controller restart.  Let them queue/query maps, but do not count
            # them as active voters until a grid/online event confirms it.
            player = Player(player_name, player_name, connected=True, active=False)
            player.start_mode = str(
                getattr(self, "start_preferences", {}).get(
                    player.identity_key,
                    "immediate",
                )
            ).casefold()
            player.display_server_tags = bool(
                getattr(self, "display_server_tag_preferences", {}).get(
                    player.identity_key,
                    False,
                )
            )
            self.players[player_name.casefold()] = player
            self.register_alias(player, player_name)
        if self.federation_follower and command not in FEDERATION_LOCAL_COMMANDS:
            # The signed sidecar sends this same engine-authenticated COMMAND
            # event to the leader, where it is executed once against shared state.
            return
        await self._dispatch_command(command, player, access_level, arguments)

    async def _dispatch_command(
        self,
        command: str,
        player: Player,
        access_level: int,
        arguments: str,
    ) -> None:
        await self._record_player_activity(player)
        if not await self._command_rate_allowed(player):
            return
        hot_commands = getattr(self, "hot_commands", None)
        if hot_commands and await hot_commands.dispatch(
            self, command, player, access_level, arguments
        ):
            return
        if command == "/q":
            await self._command_queue(player, arguments)
        elif command == "/rate":
            await self._command_rate(player, arguments)
        elif command == "/help":
            await self._command_help(player, access_level)
        elif command == "/report":
            await self._command_report(player, arguments, access_level)
        elif command == "/leaderboard":
            await self._command_leaderboard(player)
        elif command == "/setspawn":
            await self._command_setspawn(player, arguments)
        elif command == "/start":
            await self._command_start(player, arguments)
        elif command == "/display_server_tags":
            await self._command_display_server_tags(player)
        elif command == "/link":
            await self._command_link(player, arguments)
        elif command == "/cp":
            await self._command_checkpoint_respawn(player)
        elif command == "/restart":
            await self._command_restart(player)
        elif command == "/extend":
            await self._command_extend(player)
        elif command == "/skip":
            await self._command_skip(player)
        elif command == "/forceskip":
            await self._command_forceskip(player, access_level)
        elif command == "/end":
            await self._command_end(player, access_level)
        elif command == "/nextmap":
            await self._command_nextmap(player)
        elif command == "/rotation":
            await self._command_rotation(player)
        elif command == "/exclusion_list":
            await self._command_exclusion_list(player)
        elif command in {"/respawn", "/sui"}:
            await self._command_respawn(player, kill_first=True)
        elif command == "/join":
            await self._command_respawn(player, kill_first=False)
        elif command in {"/spec", "/spectate"}:
            await self._command_spectate(player)
        elif command == "/size":
            await self._command_size(player, access_level, arguments)
        elif command == "/reloadmaps":
            await self._command_reloadmaps(player, access_level)
        elif command == "/resetalltimes":
            await self._command_reset_all_times(player, access_level)
        elif command == "/reset":
            await self._command_reset_time(player, access_level, arguments)
        elif command == "/review":
            await self._command_review(player, access_level, arguments)
        elif command == "/exclude":
            await self._command_exclude(player, access_level, arguments)
        elif command == "/remove_exclusion":
            await self._command_remove_exclusion(player, access_level, arguments)

    async def _command_queue(self, player: Player, query: str) -> None:
        query = query.strip()
        if not query:
            await self.private(
                player,
                "Usage: /q [map], /q remove [map], or /q clear",
            )
            return

        if query.casefold() == "clear":
            removed_count = len(self.queue)
            if not removed_count:
                await self.private(player, "The map queue is already empty.")
                return
            self.queue.clear()
            self._save_rotation()
            await self.broadcast(
                f"{player.record_name} cleared {removed_count} "
                f"{'map' if removed_count == 1 else 'maps'} from the queue."
            )
            return

        action, separator, action_query = query.partition(" ")
        removing = action.casefold() == "remove"
        if removing:
            query = action_query.strip() if separator else ""
            if not query:
                await self.private(player, "Usage: /q remove [map name]")
                return

        matches = self.repository.search(query)
        if not matches:
            await self.private(player, f"No map found matching: {query}")
            return

        if removing:
            queued_keys = set(self.queue)
            queued_matches = [entry for entry in matches if entry.key in queued_keys]
            if not queued_matches:
                await self.private(player, f"That map is not in the queue: {query}")
                return
            matches = queued_matches

        if len(matches) > 1:
            preview = ", ".join(
                f"{self._display_map_name(entry)} ({entry.author})"
                for entry in matches[:5]
            )
            await self.private(player, f"Map name is ambiguous: {preview}")
            return

        entry = matches[0]
        display_name = self._display_map_name(entry)
        if removing:
            position = list(self.queue).index(entry.key) + 1
            self.queue.remove(entry.key)
            self._save_rotation()
            await self.broadcast(
                f"{player.record_name} removed {display_name} by {entry.author} "
                f"from the queue (was position {position})."
            )
            return

        if self.current and entry.key == self.current.key:
            await self.private(
                player,
                f"{display_name} is already active and cannot be queued next.",
            )
            return

        self.queue.append(entry.key)
        self._save_rotation()
        await self.broadcast(
            f"{player.record_name} queued {display_name} by {entry.author} "
            f"(position {len(self.queue)})."
        )

    async def _command_rate(self, player: Player, argument: str) -> None:
        if not self.current:
            await self.private(player, "No current map is available to rate.")
            return
        requested = argument.strip().casefold()
        map_key = self.current.rating_key
        map_name = self._display_map_name(self.current)

        if requested == "undo":
            result = self.store.undo_rating(map_key, player.identity_key)
            if result is None:
                await self.private(
                    player,
                    f"There is no rating change to undo for {map_name}.",
                )
                return
            _, restored = result
            if restored is None:
                await self.private(
                    player,
                    f"Your rating for {map_name} was undone and removed.",
                )
            else:
                await self.private(
                    player,
                    f"Your rating for {map_name} was restored to {restored}/5.",
                )
            return

        if requested == "revoke":
            revoked = self.store.revoke_rating(map_key, player.identity_key)
            if revoked is None:
                await self.private(
                    player,
                    f"You have no rating to revoke for {map_name}.",
                )
            else:
                await self.private(
                    player,
                    f"Your {revoked}/5 rating for {map_name} was revoked.",
                )
            return

        if not re.fullmatch(r"[1-5]", requested):
            await self.private(
                player,
                "Usage: /rate [1-5], /rate undo, or /rate revoke",
            )
            return
        rating = int(requested)
        _, changed = self.store.set_rating(map_key, player, rating)
        if not changed:
            await self.private(
                player,
                f"You already rated {map_name} {rating}/5.",
            )
            return
        await self.broadcast(
            f"{player.record_name} rated {map_name} {rating}/5. "
            "Use /rate to submit your own rating."
        )

    async def _command_help(self, player: Player, access_level: int) -> None:
        entries = list(USER_COMMAND_HELP)
        for setting, command, description in ADMIN_COMMAND_HELP:
            if access_level <= int(self.config.get(setting, 1)):
                entries.append((command, description))
        hot_commands = getattr(self, "hot_commands", None)
        if hot_commands:
            entries.extend(hot_commands.help_entries(self.config, access_level))
        await self.private_block(
            player,
            ["TronnerRacing commands:", *build_help_lines(entries)],
        )

    def _report_api_key(self) -> str:
        api_key = os.environ.get("RESEND_API_KEY", "").strip()
        if api_key:
            return api_key
        configured_path = self.config.get("resend_api_key_file")
        if not configured_path:
            return ""
        try:
            return Path(configured_path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    async def _command_report(
        self,
        player: Player,
        argument: str,
        access_level: int,
    ) -> None:
        maximum_characters = max(
            1, int(self.config.get("report_maximum_characters", 1000))
        )
        message = plain_console_text(argument).strip()
        if not message:
            await self.private(player, "Usage: /report [message]")
            return
        if len(message) > maximum_characters:
            await self.private(
                player,
                f"Reports may be at most {maximum_characters} characters.",
            )
            return

        api_key = self._report_api_key()
        recipient = str(self.config.get("report_recipient", "")).strip()
        sender = str(self.config.get("report_sender", "")).strip()
        if not api_key or not recipient or not sender:
            LOG.error("report service configuration is unavailable")
            await self.private(
                player,
                "Reports are temporarily unavailable. Please try again later.",
            )
            return

        now_monotonic = time.monotonic()
        maximum_admin_access = int(
            self.config.get("report_admin_access_level", 1)
        )
        if access_level <= maximum_admin_access:
            cooldown_seconds = max(
                0.0,
                float(self.config.get("report_admin_cooldown_seconds", 30)),
            )
        else:
            cooldown_seconds = max(
                0.0,
                float(self.config.get("report_cooldown_seconds", 300)),
            )
        last_sent = self.report_last_sent.get(player.identity_key)
        if last_sent is not None and now_monotonic - last_sent < cooldown_seconds:
            remaining = max(
                1,
                math.ceil(cooldown_seconds - (now_monotonic - last_sent)),
            )
            await self.private(
                player,
                f"Please wait {remaining} seconds before sending another report.",
            )
            return

        now_epoch = time.time()
        quota_window_seconds = max(
            1.0,
            float(self.config.get("report_quota_window_seconds", 31 * 86400)),
        )
        quota_maximum = max(
            1, int(self.config.get("report_quota_maximum", 240))
        )
        while (
            self.report_success_epochs
            and now_epoch - self.report_success_epochs[0] >= quota_window_seconds
        ):
            self.report_success_epochs.popleft()
        if len(self.report_success_epochs) >= quota_maximum:
            LOG.warning("report service quota guard reached")
            await self.private(
                player,
                "The report limit has been reached. Please contact an admin directly.",
            )
            return

        username = clean_console_text(
            plain_console_text(player.display_name or player.log_name)
        )[:80] or "Unknown"
        authenticated_username = clean_console_text(
            plain_console_text(player.auth_name or "Not authenticated")
        )[:120] or "Not authenticated"
        timezone_name = str(
            self.config.get("report_timezone", "America/Phoenix")
        )
        try:
            report_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            LOG.warning("unknown report timezone %r; using UTC", timezone_name)
            report_timezone = datetime.timezone.utc
        reported_at = datetime.datetime.now(report_timezone)
        timestamp = reported_at.strftime("%Y-%m-%d %H:%M:%S %Z")
        subject = (
            f"[{timestamp}] Report from {username} "
            f"(auth: {authenticated_username})"
        )
        map_name = (
            self._display_map_name(self.current) if self.current else "Unknown"
        )
        body = (
            "Tronner Racing Player Report\n"
            "\n"
            f"Reported: {timestamp}\n"
            f"Display username: {username}\n"
            f"Authenticated username: {authenticated_username}\n"
            f"Current map: {map_name}\n"
            "\n"
            "Report:\n"
            f"{message}\n"
        )
        try:
            await asyncio.to_thread(
                send_resend_report,
                api_key,
                recipient,
                sender,
                subject,
                body,
                str(self.config.get("resend_endpoint", RESEND_ENDPOINT)),
                max(1.0, float(self.config.get("report_timeout_seconds", 10))),
            )
        except Exception as error:
            LOG.warning(
                "report submission failed for username=%r auth=%r: %s",
                username,
                authenticated_username,
                error,
            )
            await self.private(
                player,
                "Unable to send your report right now. Please try again later.",
            )
            return

        self.report_last_sent[player.identity_key] = now_monotonic
        self.report_success_epochs.append(now_epoch)
        self.store.set_json(
            "report_success_epochs", list(self.report_success_epochs)
        )
        LOG.info(
            "report submitted for username=%r auth=%r",
            username,
            authenticated_username,
        )
        await self.private(player, "Your report was sent. Thank you.")

    async def _command_leaderboard(self, player: Player) -> None:
        if not self.current:
            await self.private(player, "No current map is available.")
            return
        records = self.store.records(map_records_key(self.current))
        lines, _ = build_leaderboard_table(
            self._display_map_name(self.current),
            self.current.author,
            records,
            top_limit=10,
            axes=self.current.axes,
            rating=self.store.rating_average(self.current.rating_key),
            time_decimals=race_time_decimals(self.current),
        )
        await self.private_block(player, lines)

    async def _command_setspawn(self, player: Player, argument: str) -> None:
        if not self.current or not self.current.spawns:
            await self.private(player, "The current map has no selectable spawns.")
            return
        requested = argument.strip()
        if requested:
            try:
                number = int(requested)
            except ValueError:
                await self.private(
                    player,
                    f"Usage: /setspawn [0-{len(self.current.spawns)}] (0 clears it)",
                )
                return
            if number == 0:
                stable_key = map_spawn_preferences_key(self.current)
                map_preferences = self._spawn_preferences_for(self.current)
                removed = map_preferences.pop(player.identity_key, None)
                if not map_preferences:
                    self.spawn_preferences.pop(stable_key, None)
                if removed is not None:
                    preference_key = self._set_local_federation_preference(
                        "spawn",
                        player.identity_key,
                        None,
                        stable_key,
                    )
                    await self._publish_federation_preference_update(
                        preference_key
                    )
                if (
                    player.last_spawn_index is not None
                    and 0 <= player.last_spawn_index < len(self.current.spawns)
                ):
                    player.spawn_cursor = (
                        player.last_spawn_index + 1
                    ) % len(self.current.spawns)
                else:
                    player.spawn_cursor = 0
                if removed is None:
                    await self.private(
                        player,
                        f"You do not have a saved spawn for "
                        f"{self._display_map_name(self.current)}.",
                    )
                else:
                    await self.private(
                        player,
                        f"Saved spawn removed for "
                        f"{self._display_map_name(self.current)}.",
                    )
                return
        elif (
            player.last_spawn_index is not None
            and 0 <= player.last_spawn_index < len(self.current.spawns)
        ):
            number = player.last_spawn_index + 1
        else:
            await self.private(
                player,
                "No recent spawn is available. Use /setspawn followed by a spawn number.",
            )
            return
        if not 1 <= number <= len(self.current.spawns):
            await self.private(
                player,
                f"Spawn must be between 1 and {len(self.current.spawns)}.",
            )
            return
        map_preferences = self._spawn_preferences_for(self.current, create=True)
        preference_key = self._set_local_federation_preference(
            "spawn",
            player.identity_key,
            number,
            map_spawn_preferences_key(self.current),
        )
        await self._publish_federation_preference_update(preference_key)
        player.spawn_cursor = number - 1
        await self.private(
            player,
            f"Spawn #{number} saved for "
            f"{self._display_map_name(self.current)}. It will be used "
            "for every respawn on this map.",
        )

    async def _command_start(self, player: Player, argument: str) -> None:
        requested = argument.strip().casefold()
        if not requested:
            await self.private(
                player,
                f"Start mode: {self._start_mode_for(player)}. "
                "Usage: /start brake, /start immediate, /start countdown, "
                "or /start respawn.",
            )
            return
        if requested not in {"brake", "immediate", "countdown", "respawn"}:
            await self.private(
                player,
                "Usage: /start brake, /start immediate, /start countdown, "
                "or /start respawn.",
            )
            return
        player.start_mode = requested
        if not hasattr(self, "start_preferences"):
            self.start_preferences = {}
        preference_key = self._set_local_federation_preference(
            "start", player.identity_key, requested
        )
        await self._publish_federation_preference_update(preference_key)
        descriptions = {
            "brake": "Press brake to begin moving after each respawn.",
            "immediate": "Begin moving immediately after each automatic respawn.",
            "countdown": "Wait for 3, 2, 1, Go! after each respawn.",
            "respawn": "Wait after a crash, then begin immediately when you use /restart.",
        }
        pending = (
            " Your current start is unchanged; this applies on your next respawn."
            if player.pending_respawn
            else ""
        )
        await self.private(
            player,
            f"Start mode set to {requested}. {descriptions[requested]}{pending}",
        )

    async def _command_display_server_tags(self, player: Player) -> None:
        enabled = not self._display_server_tags_for(player)
        if not hasattr(self, "display_server_tag_preferences"):
            self.display_server_tag_preferences = {}
        preference_key = self._set_local_federation_preference(
            "tags", player.identity_key, enabled
        )
        player.display_server_tags = enabled
        await self._publish_federation_preference_update(preference_key)
        await self.sink.send(
            f"FEDERATION_DISPLAY_SERVER_TAGS {player.target} "
            f"{1 if enabled else 0}"
        )
        await self.private(
            player,
            "Server tags on other players' names are now "
            f"{'enabled' if enabled else 'disabled'}.",
        )

    def _game_link_secret(self) -> str:
        secret = os.environ.get("GAME_LINK_SERVER_SECRET", "").strip()
        if secret:
            return secret
        game_link = self.config.get("game_link", {})
        if not isinstance(game_link, dict):
            return ""
        configured_path = str(
            game_link.get(
                "secret_file",
                "/etc/tronner-racing/game-link-secret",
            )
        ).strip()
        if not configured_path:
            return ""
        try:
            return Path(configured_path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    async def _command_link(self, player: Player, argument: str) -> None:
        code = plain_console_text(argument).strip()
        if not re.fullmatch(r"\d{6}", code):
            await self.private(
                player,
                "Usage: /link [6-digit code]. Generate one under Game logins "
                "in your tronner.io settings.",
            )
            return
        game_username = plain_console_text(player.auth_name or "").strip()
        if not game_username:
            await self.private(
                player,
                "Sign in to your in-game account first, then use /link again.",
            )
            return
        game_link = self.config.get("game_link", {})
        if not isinstance(game_link, dict):
            game_link = {}
        endpoint = str(
            game_link.get("endpoint", DEFAULT_GAME_LINK_ENDPOINT)
        ).strip()
        secret = self._game_link_secret()
        server_id = str(
            game_link.get("server_id", self.federation_local_server_id)
        ).strip().casefold()
        if not endpoint or not secret or not server_id:
            LOG.error("game account linking is not configured")
            await self.private(
                player,
                "Account linking is temporarily unavailable. Please try again later.",
            )
            return
        try:
            result = await asyncio.to_thread(
                redeem_game_account_link,
                endpoint,
                secret,
                code,
                game_username,
                server_id,
                max(1.0, float(game_link.get("timeout_seconds", 10))),
            )
        except GameLinkServiceError as error:
            LOG.info(
                "game account link refused for auth=%r server=%s reason=%s",
                game_username,
                server_id,
                error.code,
            )
            await self.private(player, error.public_message)
            return
        except Exception as error:
            LOG.warning(
                "game account link failed for auth=%r server=%s: %s",
                game_username,
                server_id,
                error,
            )
            await self.private(
                player,
                "Unable to link your account right now. Please try again later.",
            )
            return
        website_name = clean_console_text(
            result.get("websiteDisplayName", "your website account")
        ).strip()[:80] or "your website account"
        await self.private(
            player,
            f"Linked {game_username} to {website_name} on tronner.io.",
        )

    async def _command_extend(self, player: Player) -> None:
        if self.final_countdown_active:
            await self.private(player, "The final countdown has started; this map cannot be extended.")
            return
        voters = self.eligible_voters()
        voter_ids = {id(voter) for voter in voters}
        if id(player) not in voter_ids:
            await self.private(player, "Only active players may vote to extend.")
            return
        active_keys = {voter.identity_key for voter in voters}
        self.extend_votes.intersection_update(active_keys)
        count = max(1, len(voters))
        required = 1 if count == 1 else extend_votes_required(count)
        self.extend_votes.add(player.identity_key)
        if not await self._resolve_extend_vote():
            await self.broadcast(
                f"Extend vote: {len(self.extend_votes)}/{required} required."
            )

    async def _command_skip(self, player: Player) -> None:
        if not self.current or not self._round_is_active():
            await self.private(player, "No active map is available to skip.")
            return
        if self.transitioning:
            await self.private(player, "A map change is already in progress.")
            return
        if self.final_countdown_active or self.final_countdown_announcement:
            await self.private(player, "The end-of-map timer is already active.")
            return
        voters = self.eligible_voters()
        voter_ids = {id(voter) for voter in voters}
        if id(player) not in voter_ids:
            await self.private(player, "Only active players may vote to skip.")
            return
        active_keys = {voter.identity_key for voter in voters}
        self.skip_votes.intersection_update(active_keys)
        count = max(1, len(voters))
        required = 1 if count == 1 else skip_votes_required(count)
        self.skip_votes.add(player.identity_key)
        if not await self._resolve_skip_vote():
            await self.broadcast(
                f"Skip vote: {len(self.skip_votes)}/{required} required. "
                "Type /skip to go to the next map"
            )

    async def _command_end(self, player: Player, access_level: int) -> None:
        maximum_access = int(self.config.get("map_admin_access_level", 1))
        if access_level > maximum_access:
            await self.private(
                player, "Only an Owner or Admin may start the end-of-map timer."
            )
            return
        if not self.current or not self._round_is_active():
            await self.private(player, "No active map is available to end.")
            return
        if self.transitioning:
            await self.private(player, "A map change is already in progress.")
            return
        if self.final_countdown_active or self.final_countdown_announcement:
            await self.private(player, "The end-of-map timer is already active.")
            return

        idle_seconds = float(
            self.config.get("final_countdown_idle_seconds", 10)
        )
        if idle_seconds > 0:
            # Clear racers who were already idle at the instant /end was
            # submitted. The final-countdown loop keeps rechecking afterward.
            await self.sink.send(
                f"KILL_IDLE_PLAYERS {idle_seconds:.9g}"
            )

        # Map timer owns the countdown loop. Expiring its ordinary deadline
        # keeps /end indistinguishable from a natural timer expiration.
        self.final_countdown_announcement = None
        self.deadline_epoch = time.time()
        self.store.set_json("deadline_epoch", self.deadline_epoch)

    async def _command_forceskip(self, player: Player, access_level: int) -> None:
        maximum_access = int(self.config.get("records_admin_access_level", 1))
        if access_level > maximum_access:
            await self.private(player, "Only an Owner or Admin may force-skip maps.")
            return
        if self.transitioning:
            await self.private(player, "A map change is already in progress.")
            return
        self._clear_vote("skip")
        await self.broadcast(f"{player.record_name} force-skipped the map.")
        await self.activate_next_map("admin force skip")

    async def _command_exclusion_list(self, player: Player) -> None:
        rows = self._excluded_map_rows()
        if not rows:
            await self.private(player, "The exclusion list is empty.")
            return
        reasons = getattr(self, "excluded_map_reasons", {})
        items = [
            f"{selector} by {author} [{version}]"
            + (f" — {reasons[key]}" if reasons.get(key) else "")
            for key, _, author, version, selector in rows
        ]
        await self.private_block(
            player,
            [
                f"Excluded maps ({len(rows)}):",
                *build_compact_columns(items),
            ],
        )

    async def _map_reviews(self) -> list[dict]:
        return await asyncio.to_thread(self.repository.list_map_reviews)

    async def _command_review_list(self, player: Player) -> None:
        try:
            rows = self._review_map_rows(await self._map_reviews())
        except Exception as exc:
            LOG.exception("loading the Vectron map review list failed")
            await self.private(player, f"Review list failed: {exc}")
            return
        if not rows:
            await self.private(player, "The map review list is empty.")
            return
        items = [
            f"{selector} by {author} [{version}; {status}]"
            for _, _, _, author, version, status, selector in rows
        ]
        await self.private_block(
            player,
            [
                f"Maps awaiting or needing Vectron review ({len(rows)}):",
                *build_compact_columns(items),
            ],
        )

    async def _command_review_remove(
        self,
        player: Player,
        query: str,
    ) -> None:
        if not query.strip():
            await self.private(player, "Usage: /review remove [map name]")
            return
        try:
            reviews = await self._map_reviews()
        except Exception as exc:
            LOG.exception("loading the Vectron map review list failed")
            await self.private(player, f"Review removal failed: {exc}")
            return
        matches = self._search_map_reviews(reviews, query)
        if not matches:
            await self.private(player, f"No reviewed map found matching: {query.strip()}")
            return
        if len(matches) > 1:
            preview = ", ".join(
                f"{selector} ({author}, {version})"
                for _, _, _, author, version, _, selector in matches[:5]
            )
            await self.private(player, f"Reviewed map name is ambiguous: {preview}")
            return
        review_id, key, name, author, version, _, selector = matches[0]
        if (
            self.current
            and key == self.current.key
            and (self.final_countdown_active or self.final_countdown_announcement)
        ):
            await self.private(
                player,
                "That map is already ending; remove it from review after the next map starts.",
            )
            return
        async with self.map_lock:
            try:
                await asyncio.to_thread(
                    self.repository.cancel_map_review,
                    review_id,
                    f"Review cancelled by server admin {player.record_name}",
                )
                self._reconcile_rotation()
            except Exception as exc:
                LOG.exception("cancelling map review failed for %s", review_id)
                await self.private(player, f"Review removal failed: {exc}")
                return
        await self.broadcast(
            f"{player.record_name} removed {selector} by {author} [{version}] "
            "from review and returned it to the map pool."
        )

    async def _command_review_submit(
        self,
        player: Player,
        query: str,
        reason: str = "",
    ) -> None:
        query = query.strip()
        if query:
            matches = self.repository.search(query)
            if not matches:
                await self.private(player, f"No active map found matching: {query}")
                return
            if len(matches) > 1:
                preview = ", ".join(
                    f"{self._display_map_name(entry)} ({entry.author}, {entry.version})"
                    for entry in matches[:5]
                )
                await self.private(player, f"Map name is ambiguous: {preview}")
                return
            entry = matches[0]
        else:
            entry = self.current
            if not entry:
                await self.private(player, "No current map is available for review.")
                return

        reviewing_current = bool(self.current and entry.key == self.current.key)
        if reviewing_current:
            if not self._round_is_active():
                await self.private(player, "The current map has not started yet.")
                return
            if self.transitioning:
                await self.private(player, "A map change is already in progress.")
                return
            if self.final_countdown_active or self.final_countdown_announcement:
                await self.private(player, "The end-of-map timer is already active.")
                return
        if len(set(self.repository.catalog) - {entry.key}) < 1:
            await self.private(player, "The final available map cannot be reviewed.")
            return

        name = self._display_map_name(entry)
        submission_reason = (
            reason.strip()
            or f"Submitted by server admin {player.record_name}"
        )
        async with self.map_lock:
            try:
                await asyncio.to_thread(
                    self.repository.submit_map_review,
                    entry.key,
                    submission_reason,
                )
                self._reconcile_rotation()
            except Exception as exc:
                LOG.exception("submitting map review failed for %s", entry.key)
                await self.private(player, f"Review submission failed: {exc}")
                return

        await self.broadcast(
            f"{player.record_name} submitted {name} by {entry.author} "
            "for Vectron review and removed it from rotation."
        )
        if reviewing_current:
            # Arm the countdown before yielding again so no respawn can slip
            # in after a map has been removed from the catalog.
            self.final_countdown_active = True
            self.final_countdown_end_epoch = None
            self.final_countdown_map_key = entry.key
            self.final_countdown_announcement = "Map submitted for Vectron review."
            self.store.set_json("final_countdown_active", True)
            self.store.set_json("final_countdown_end_epoch", None)
            self.store.set_json("final_countdown_map_key", entry.key)
            idle_seconds = float(
                self.config.get("final_countdown_idle_seconds", 10)
            )
            if idle_seconds > 0:
                await self.sink.send(f"KILL_IDLE_PLAYERS {idle_seconds:.9g}")

    async def _command_review(
        self,
        player: Player,
        access_level: int,
        arguments: str,
    ) -> None:
        maximum_access = int(self.config.get("map_admin_access_level", 1))
        if access_level > maximum_access:
            await self.private(player, "Only an Owner or Admin may review maps.")
            return
        action, separator, remainder = arguments.strip().partition(" ")
        if action.casefold() == "list" and not remainder.strip():
            await self._command_review_list(player)
        elif action.casefold() == "remove":
            await self._command_review_remove(
                player,
                remainder.strip() if separator else "",
            )
        else:
            query, reason, has_reason = split_admin_reason(arguments)
            if has_reason and not reason:
                await self.private(
                    player,
                    "Enter a reason after --, or omit -- entirely.",
                )
                return
            if len(reason) > 1000:
                await self.private(
                    player,
                    "Keep the review reason to 1,000 characters or fewer.",
                )
                return
            await self._command_review_submit(player, query, reason)

    async def _command_remove_exclusion(
        self,
        player: Player,
        access_level: int,
        query: str,
    ) -> None:
        maximum_access = int(self.config.get("map_admin_access_level", 1))
        if access_level > maximum_access:
            await self.private(
                player,
                "Only an Owner or Admin may remove map exclusions.",
            )
            return
        query = query.strip()
        if not query:
            await self.private(player, "Usage: /remove_exclusion [map name]")
            return
        matches = self._search_excluded_maps(query)
        if not matches:
            await self.private(player, f"No excluded map found matching: {query}")
            return
        if len(matches) > 1:
            preview = ", ".join(
                f"{selector} ({author}, {version})"
                for _, _, author, version, selector in matches[:5]
            )
            await self.private(player, f"Excluded map name is ambiguous: {preview}")
            return

        key, _, author, version, selector = matches[0]
        exclusion_reason = getattr(self, "excluded_map_reasons", {}).get(key, "")
        async with self.map_lock:
            try:
                await asyncio.to_thread(
                    publish_repository_map_status,
                    self.repository,
                    key,
                    "active",
                    f"Reactivated by server admin {player.record_name}",
                )
            except Exception as exc:
                LOG.exception("publishing map reactivation failed for %s", key)
                await self.private(player, f"Removing exclusion failed: {exc}")
                return
            self.excluded_map_keys.remove(key)
            if hasattr(self, "excluded_map_reasons"):
                self.excluded_map_reasons.pop(key, None)
            self.repository.excluded_keys = self.excluded_map_keys
            self.store.set_json(
                "excluded_map_keys",
                sorted(self.excluded_map_keys),
            )
            self.store.set_json(
                "excluded_map_reasons",
                getattr(self, "excluded_map_reasons", {}),
            )
            try:
                await asyncio.to_thread(self.repository.scan)
                self._reconcile_rotation()
            except Exception as exc:
                self.excluded_map_keys.add(key)
                if exclusion_reason:
                    self.excluded_map_reasons[key] = exclusion_reason
                self.repository.excluded_keys = self.excluded_map_keys
                self.store.set_json(
                    "excluded_map_keys",
                    sorted(self.excluded_map_keys),
                )
                self.store.set_json(
                    "excluded_map_reasons",
                    getattr(self, "excluded_map_reasons", {}),
                )
                LOG.exception("removing map exclusion failed for %s", key)
                await self.private(player, f"Removing exclusion failed: {exc}")
                return
        await self._publish_federation_catalog_exclusions()
        await self.broadcast(
            f"{player.record_name} returned {selector} by {author} "
            f"[{version}] to the map pool."
        )

    async def _command_exclude(
        self,
        player: Player,
        access_level: int,
        arguments: str = "",
    ) -> None:
        maximum_access = int(self.config.get("map_admin_access_level", 1))
        if access_level > maximum_access:
            await self.private(player, "Only an Owner or Admin may exclude maps.")
            return
        query, reason, has_reason = split_admin_reason(arguments)
        if has_reason and not reason:
            await self.private(
                player,
                "Enter a reason after --, or omit -- entirely.",
            )
            return
        if len(reason) > 1000:
            await self.private(
                player,
                "Keep the exclusion reason to 1,000 characters or fewer.",
            )
            return
        if query:
            matches = self.repository.search(query)
            if not matches:
                await self.private(player, f"No active map found matching: {query}")
                return
            if len(matches) > 1:
                preview = ", ".join(
                    f"{self._display_map_name(entry)} ({entry.author}, {entry.version})"
                    for entry in matches[:5]
                )
                await self.private(player, f"Map name is ambiguous: {preview}")
                return
            entry = matches[0]
        else:
            entry = self.current
            if not entry:
                await self.private(player, "No current map is available.")
                return
        excluding_current = bool(self.current and entry.key == self.current.key)
        if excluding_current and self.transitioning:
            await self.private(player, "A map change is already in progress.")
            return
        key = entry.key
        if len(set(self.repository.catalog) - {key}) < 1:
            await self.private(player, "The final available map cannot be excluded.")
            return
        name = self._display_map_name(entry)
        status_reason = (
            reason.strip()
            or f"Excluded by server admin {player.record_name}"
        )
        try:
            await asyncio.to_thread(
                publish_repository_map_status,
                self.repository,
                key,
                "inactive",
                status_reason,
            )
        except Exception as exc:
            LOG.exception("publishing map exclusion failed for %s", key)
            await self.private(player, f"Excluding map failed: {exc}")
            return
        await self._exclude_map_key(key, status_reason)
        await self.broadcast(
            f"{player.record_name} excluded {name} from the map pool."
            + (f" Reason: {reason}" if reason else "")
        )
        if excluding_current:
            await self.activate_next_map("admin excluded current map")

    async def _command_reset_all_times(
        self, player: Player, access_level: int
    ) -> None:
        maximum_access = int(self.config.get("records_admin_access_level", 1))
        if access_level > maximum_access:
            await self.private(player, "Only an Owner or Admin may reset times.")
            return
        if not self.current:
            await self.private(player, "No current map is available.")
            return
        record_count, finish_count = self.store.reset_map(map_records_key(self.current))
        await self.broadcast(
            f"{player.record_name} reset all times on "
            f"{self._display_map_name(self.current)}: "
            f"{record_count} records and {finish_count} finish entries removed."
        )

    async def _command_reset_time(
        self, player: Player, access_level: int, arguments: str
    ) -> None:
        maximum_access = int(self.config.get("records_admin_access_level", 1))
        if access_level > maximum_access:
            await self.private(player, "Only an Owner or Admin may reset times.")
            return
        username, separator, map_query = arguments.strip().partition(" ")
        if not username:
            await self.private(player, "Usage: /reset [user] [map (optional)]")
            return
        if separator:
            matches = self.repository.search(map_query)
            if not matches:
                await self.private(player, f"No map found matching: {map_query}")
                return
            if len(matches) > 1:
                preview = ", ".join(
                    f"{self._display_map_name(entry)} ({entry.author})"
                    for entry in matches[:5]
                )
                await self.private(player, f"Map name is ambiguous: {preview}")
                return
            entry = matches[0]
        else:
            entry = self.current
        if not entry:
            await self.private(player, "No current map is available.")
            return
        names, record_count, finish_count = self.store.reset_user(
            map_records_key(entry), username
        )
        if not names:
            await self.private(
                player,
                f"No record for {username} was found on "
                f"{self._display_map_name(entry)}.",
            )
            return
        matched_names = ", ".join(names)
        await self.broadcast(
            f"{player.record_name} reset {matched_names} on "
            f"{self._display_map_name(entry)}: "
            f"{record_count} record and {finish_count} finish entries removed."
        )

    async def _command_nextmap(self, player: Player) -> None:
        entry = self._peek_next()
        if entry:
            prefix = "Queued next" if self.queue else "Next map"
            await self.private(
                player,
                f"{prefix}: {self._display_map_name(entry)} by {entry.author}",
            )
        else:
            await self.private(player, "No map is currently available.")

    async def _command_rotation(self, player: Player) -> None:
        entries_by_key = dict(self.repository.catalog)
        if self.current:
            entries_by_key.setdefault(self.current.key, self.current)
        entries = sorted(
            entries_by_key.values(),
            key=lambda entry: (
                self._display_map_name(entry).casefold(),
                entry.author.casefold(),
                entry.version.casefold(),
                entry.key.casefold(),
            ),
        )
        if not entries:
            await self.private(player, "The map rotation is empty.")
            return
        current_key = self.current.key if self.current else None
        items = [
            (
                self._display_map_name(entry),
                entry.author,
                entry.version,
                entry.key == current_key,
            )
            for entry in entries
        ]
        await self.private_block(
            player,
            [
                f"Map rotation ({len(entries)}):",
                *build_rotation_columns(items),
            ],
        )

    async def _command_reloadmaps(
        self, player: Player, access_level: int
    ) -> None:
        maximum_access = int(self.config.get("map_admin_access_level", 1))
        if access_level > maximum_access:
            await self.private(player, "Only an Owner or Admin may reload maps.")
            return
        async with self.map_lock:
            before = set(self.repository.catalog)
            try:
                await asyncio.to_thread(self.repository.sync, True)
                self._reconcile_rotation()
            except Exception as exc:
                LOG.exception("manual map reload failed")
                await self.private(player, f"Map reload failed: {exc}")
                return
            after = set(self.repository.catalog)
        added = sorted(after - before)
        removed = sorted(before - after)
        await self.private(
            player,
            f"Maps reloaded: {len(after)} available, {len(added)} added, "
            f"{len(removed)} removed.",
        )
        if added:
            labels = ", ".join(
                self._display_map_name(self.repository.catalog[key])
                for key in added
            )
            await self.private(player, f"Added: {labels}")

    async def _command_size(
        self, player: Player, access_level: int, argument: str
    ) -> None:
        maximum_access = int(self.config.get("size_admin_access_level", 1))
        if access_level > maximum_access:
            await self.private(player, "Only an Owner or Admin may change map size.")
            return
        match = re.fullmatch(r"([+-])(\d+(?:\.\d+)?)", argument.strip())
        if not match:
            await self.private(player, "Usage: /size +x or /size -x")
            return
        if not self.current:
            await self.private(player, "No active map is available to revise.")
            return
        delta = float(match.group(2)) * (1 if match.group(1) == "+" else -1)
        current_factor = self.current_size_factor
        if current_factor is None:
            with contextlib.suppress(Exception):
                current_factor = await asyncio.to_thread(
                    self.repository.map_size_factor, self.current
                )
        if current_factor is None:
            current_factor = float(self.config.get("default_size_factor", 0))
        revised_factor = current_factor + delta
        if not math.isfinite(revised_factor) or not -10 <= revised_factor <= 10:
            await self.private(player, "SIZE_FACTOR must remain between -10 and 10.")
            return

        await self.private(
            player,
            f"Publishing map size {format_size_factor(current_factor)} -> "
            f"{format_size_factor(revised_factor)}...",
        )

        async with self.map_lock:
            old_entry = self.current
            try:
                revision = await asyncio.to_thread(
                    self.repository.create_size_revision, old_entry, revised_factor
                )
                # Firebase advances the logical map document to a new immutable
                # resource path, so its superseded path is already absent from
                # rotation. Only the legacy Git backend needs a local exclusion.
                if getattr(self.repository, "firebase", None) is None:
                    self.excluded_map_keys.add(old_entry.key)
                    self.repository.excluded_keys = self.excluded_map_keys
                    self.store.set_json(
                        "excluded_map_keys", sorted(self.excluded_map_keys)
                    )
                if getattr(self.repository, "firebase", None) is None:
                    await asyncio.to_thread(self.repository.scan)
                revision = self.repository.catalog[revision.key]
                self._reconcile_rotation()
                with contextlib.suppress(ValueError):
                    self.rotation.remove(revision.key)
                self.queue = collections.deque(
                    key
                    for key in self.queue
                    if key not in {old_entry.key, revision.key}
                )
                self.cycle_played.add(revision.key)
                self._save_rotation()
                await asyncio.to_thread(self.repository.cache_for_server, revision)
                await self._prepare_federated_leader_map(
                    revision,
                    revised_factor,
                )
            except Exception as exc:
                LOG.exception("unable to create SIZE_FACTOR map revision")
                await self.private(player, f"Unable to revise this map: {exc}")
                return

            old_record_count, old_finish_count = self.store.reset_map(
                map_records_key(old_entry)
            )
            new_record_count, new_finish_count = self.store.reset_map(
                map_records_key(revision)
            )
            reset_record_count = old_record_count + new_record_count
            reset_finish_count = old_finish_count + new_finish_count

            self._clear_final_countdown_state()
            self.current = revision
            self.current_spec = revision.key
            self.current_size_factor = revised_factor
            self.round_started_epoch = None
            self.deadline_epoch = time.time() + self._map_open_play_seconds(revision)
            self.store.set_json("current_key", revision.key)
            self.store.set_json("deadline_epoch", self.deadline_epoch)
            self.store.set_json("round_started_epoch", None)
            self._clear_all_votes()
            self._begin_map_transition(revision.key)
            self.round_active = False
            self._reset_attempts()
            await self.sink.send(
                f"SIZE_FACTOR {format_size_factor(float(self.config.get('default_size_factor', 0)))}",
                f"MAP_FILE {quote_console(revision.key)}",
                "START_NEW_MATCH",
                "KILL_ALL",
                "GET_CURRENT_MAP",
            )
            await self.broadcast(
                f"Map size changed from {format_size_factor(current_factor)} to "
                f"{format_size_factor(revised_factor)}. Reloading "
                f"{self._display_map_name(revision)} "
                f"version {revision.version}. Reset {reset_record_count} records "
                f"and {reset_finish_count} finish entries."
            )

    async def _command_checkpoint_respawn(self, player: Player) -> None:
        if getattr(self, "respawns_paused", False):
            await self.private(
                player,
                "Respawns are paused for a controller reload; your run will resume shortly.",
            )
            return
        if self.final_countdown_active:
            await self.private(
                player,
                "Checkpoint respawns are disabled during the final countdown.",
            )
            return
        snapshot = player.checkpoint_snapshot
        if snapshot is None:
            await self.private(player, "No checkpoint is available for this run yet.")
            return
        if not player.connected or not player.active or not player.respawn_enabled:
            await self.private(player, "Join the grid before using /cp.")
            return

        now = time.monotonic()
        repeat_window = max(
            0.0,
            float(self.config.get("checkpoint_double_respawn_seconds", 1.5)),
        )
        double_respawn = player.pending_respawn_kind == "checkpoint" or (
            player.last_checkpoint_respawn_monotonic is not None
            and now - player.last_checkpoint_respawn_monotonic <= repeat_window
        )
        player.last_checkpoint_respawn_monotonic = now
        player.checkpoint_respawn_requested = True
        player.checkpoint_respawn_speed = 0.0 if double_respawn else snapshot.speed
        self._cancel_player_freeze(player, clear_attempt=False)
        player.pending_respawn_kind = ""

        if double_respawn:
            await self.private(
                player,
                "Checkpoint respawn reset: takeoff speed is now 0.",
            )
        if player.alive:
            await self.sink.send(f"KILL_SILENT {player.target}")
            return
        self._schedule_respawn(
            player,
            delay_seconds=float(
                self.config.get("checkpoint_respawn_delay_seconds", 0.1)
            ),
        )

    async def _command_spectate(self, player: Player) -> None:
        player.respawn_enabled = False
        player.forced_racing = False
        self.finalists.discard(id(player))
        self._cancel_player_freeze(player)
        await self.private(player, "Respawning disabled. Use /join or /respawn to return.")
        if player.alive:
            await self.sink.send(f"KILL_SILENT {player.target}")

    async def _command_restart(self, player: Player) -> None:
        if getattr(self, "respawns_paused", False):
            await self.private(
                player,
                "Respawns are paused for a controller reload; your run will resume shortly.",
            )
            return
        if self.final_countdown_active:
            await self.private(
                player,
                "Respawning is disabled during the final countdown.",
            )
            return
        player.respawn_enabled = True
        if not player.connected:
            await self.private(player, "Join the grid before restarting.")
            return
        if not player.active:
            player.forced_racing = True
            player.active = True
        self._cancel_player_freeze(player)
        if self._start_mode_for(player) == "respawn":
            player.manual_restart_pending = True
            await self.center_private(player, "")
        if player.alive:
            await self.sink.send(f"KILL_SILENT {player.target}")
            return
        await self._respawn_player(player)

    async def _command_respawn(self, player: Player, kill_first: bool) -> None:
        if getattr(self, "respawns_paused", False):
            await self.private(
                player,
                "Respawns are paused for a controller reload; your run will resume shortly.",
            )
            return
        if self.final_countdown_active:
            await self.private(player, "Respawning is disabled during the final countdown.")
            if kill_first and player.alive:
                self.finalists.discard(id(player))
                await self.sink.send(f"KILL_SILENT {player.target}")
            return
        player.respawn_enabled = True
        if not player.connected:
            await self.private(player, "Join the grid before enabling respawns.")
            return
        if not player.active:
            # RESPAWN_STRICT is disabled for this controller-managed path, so a
            # client retaining spectator mode can explicitly return to racing.
            player.forced_racing = True
            player.active = True
        if kill_first and player.alive:
            self._cancel_player_freeze(player, clear_attempt=False)
            await self.sink.send(f"KILL_SILENT {player.target}")
            return
        if not player.alive and id(player) not in self.respawn_tasks:
            await self._respawn_player(player)
        elif not player.alive:
            await self.private(player, "Respawn already scheduled.")
        else:
            await self.private(player, "Respawning enabled.")

    async def _announce_time_left(self, now: float) -> None:
        if (
            not self._round_is_active()
            or self.transitioning
            or self.final_countdown_active
            or self.deadline_epoch is None
        ):
            return
        minute = max(0, int(math.ceil((self.deadline_epoch - now) / 60.0)))
        if minute == self.last_time_left_minute:
            return
        self.last_time_left_minute = minute
        if minute > 0 and minute % 2 == 0:
            await self.broadcast(f"Time left: {minute} minutes.")

    async def map_timer(self) -> None:
        if self.federation_follower:
            await self.stop_event.wait()
            return
        while not self.stop_event.is_set():
            if getattr(self, "controller_reload_draining", False):
                await asyncio.sleep(0.25)
                continue
            if self.final_countdown_active:
                await self._run_final_countdown(resume=True)
            elif self._round_is_active() and not self.transitioning:
                now = time.time()
                await self._announce_time_left(now)
                if self.deadline_epoch is not None and now >= self.deadline_epoch:
                    await self._run_final_countdown(enforce_clock_runout=True)
            await asyncio.sleep(0.25)

    async def repository_refresher(self) -> None:
        # Firebase invalidation is handled by catalog_state_monitor. Retain the
        # periodic refresher only for legacy Git-backed repositories.
        if (
            self.repository.firebase is not None
            or not self.config.get("repository_auto_sync", True)
        ):
            await self.stop_event.wait()
            return
        interval = max(60, int(self.config.get("repository_refresh_seconds", 300)))
        while not self.stop_event.is_set():
            await asyncio.sleep(interval)
            try:
                async with self.map_lock:
                    await asyncio.to_thread(self.repository.sync)
                    self._reconcile_rotation()
            except Exception:
                LOG.exception("repository refresh failed; retaining the current catalog")

    async def catalog_state_monitor(self) -> None:
        """Watch one version document instead of polling full collections."""
        if self.repository.firebase is None or self.federation_follower:
            await self.stop_event.wait()
            return
        interval = max(
            5.0,
            float(self.config.get(
                "catalog_state_poll_seconds",
                self.config.get("review_status_poll_seconds", 15),
            )),
        )
        while not self.stop_event.is_set():
            try:
                firebase = self.repository.firebase
                state = await asyncio.to_thread(firebase.get_catalog_state)
                signature = (
                    int(state.get("catalogVersion") or 0),
                    str(state.get("generation") or ""),
                    str(state.get("serverManifestSha256") or ""),
                )
                if not all(signature):
                    raise FirebaseCatalogError(
                        "catalog state is incomplete; retaining the current catalog"
                    )
                if signature != self.catalog_state_signature:
                    async with self.map_lock:
                        manifest = await asyncio.to_thread(
                            self.repository.sync,
                            catalog_state=state,
                        )
                        self._reconcile_rotation()
                    self.catalog_state_signature = signature
                    LOG.info(
                        "Firebase catalog version %d applied (generation %s)",
                        signature[0],
                        signature[1],
                    )
                else:
                    manifest = None
                if signature != self.catalog_ack_signature:
                    if manifest is None:
                        manifest = json.loads(
                            (self.repository.checkout / ".catalog.json").read_text("utf-8")
                        )
                    await asyncio.to_thread(
                        firebase.publish_server_catalog_state,
                        catalog_state=state,
                        generation=str(manifest.get("generation") or ""),
                        map_count=len(manifest.get("maps") or []),
                    )
                    self.catalog_ack_signature = signature
            except Exception:
                LOG.exception("Firebase catalog state refresh failed")
            await asyncio.sleep(interval)

    def _next_helpful_message(
        self, messages: Sequence[str]
    ) -> tuple[str, dict] | None:
        current_messages = list(dict.fromkeys(messages))
        if not current_messages:
            return None
        current_set = set(current_messages)
        state = getattr(self, "helpful_message_cycle", {})
        raw_order = state.get("order", []) if isinstance(state, dict) else []
        if not isinstance(raw_order, list):
            raw_order = []
        try:
            raw_index = max(0, min(int(state.get("index", 0)), len(raw_order)))
        except (TypeError, ValueError):
            raw_index = 0

        shown: list[str] = []
        remaining: list[str] = []
        known: set[str] = set()
        for position, value in enumerate(raw_order):
            if not isinstance(value, str) or value not in current_set or value in known:
                continue
            known.add(value)
            if position < raw_index:
                shown.append(value)
            else:
                remaining.append(value)
        additions = [message for message in current_messages if message not in known]
        random.shuffle(additions)
        remaining.extend(additions)
        order = [*shown, *remaining]
        index = len(shown)
        last_shown = state.get("last_shown") if isinstance(state, dict) else None

        if index >= len(order):
            order = current_messages.copy()
            random.shuffle(order)
            if len(order) > 1 and order[0] == last_shown:
                order[0], order[1] = order[1], order[0]
            index = 0
        message = order[index]
        return message, {
            "version": 1,
            "order": order,
            "index": index + 1,
            "last_shown": message,
        }

    async def _announce_helpful_message_once(self) -> bool:
        activity_window = max(
            1.0,
            float(
                self.config.get(
                    "helpful_message_activity_window_seconds",
                    180,
                )
            ),
        )
        now = time.monotonic()
        active_human = any(
            player.connected
            and player.active
            and not player.is_ai
            and player.last_activity_monotonic is not None
            and now - player.last_activity_monotonic <= activity_window
            for player in self.players.values()
        )
        if not active_human:
            return False
        path = Path(
            self.config.get(
                "helpful_messages_file",
                "/etc/tronner-racing/helpful_messages.txt",
            )
        )
        messages = await asyncio.to_thread(load_helpful_messages, path)
        store = getattr(self, "store", None)
        if store is not None:
            messages.extend(load_custom_helpful_messages(store))
        selected = self._next_helpful_message(messages)
        if selected is None:
            return False
        message, next_cycle = selected
        await self.broadcast(style_tip_message(message))
        next_cycle["announced_round_token"] = getattr(
            self, "helpful_message_round_token", None
        )
        self.helpful_message_cycle = next_cycle
        if store is not None:
            store.set_json("helpful_message_cycle", next_cycle)
        return True

    def _cancel_helpful_message(self) -> None:
        self.helpful_message_round_generation = (
            getattr(self, "helpful_message_round_generation", 0) + 1
        )
        self.helpful_message_announced = False
        task = getattr(self, "_helpful_message_task", None)
        if task:
            task.cancel()
        self._helpful_message_task = None

    def _begin_helpful_message_round(self) -> None:
        # The leader is the cluster authority for timed round messages. The follower
        # receives the resulting broadcast through the federation control
        # channel, avoiding two independent tip timers.
        if self.federation_follower:
            self._cancel_helpful_message()
            return
        map_key = self.current.key if self.current else "unknown"
        self.helpful_message_round_token = f"{map_key}:{time.time_ns()}"
        self.store.set_json(
            "helpful_message_round_token", self.helpful_message_round_token
        )
        self._schedule_helpful_message()

    def _schedule_helpful_message(self) -> None:
        self._cancel_helpful_message()
        if not self._round_is_active() or self.federation_follower:
            return
        if not getattr(self, "helpful_message_round_token", None):
            map_key = self.current.key if getattr(self, "current", None) else "unknown"
            self.helpful_message_round_token = f"{map_key}:{time.time_ns()}"
            store = getattr(self, "store", None)
            if store is not None:
                store.set_json(
                    "helpful_message_round_token",
                    self.helpful_message_round_token,
                )
        if self.helpful_message_cycle.get("announced_round_token") == (
            self.helpful_message_round_token
        ):
            self.helpful_message_announced = True
            return
        generation = self.helpful_message_round_generation
        map_duration = max(
            0.0, float(self.config.get("map_duration_seconds", 300))
        )
        minimum = max(
            0.0,
            float(self.config.get("helpful_message_random_min_seconds", 30)),
        )
        maximum = max(
            0.0,
            float(
                self.config.get(
                    "helpful_message_random_max_seconds",
                    map_duration * 0.8,
                )
            ),
        )
        if self.deadline_epoch is not None:
            maximum = min(maximum, max(0.0, self.deadline_epoch - time.time() - 1))
        minimum = min(minimum, maximum)
        delay = random.uniform(minimum, maximum)
        self._helpful_message_task = asyncio.create_task(
            self._delayed_helpful_message(generation, delay),
            name="helpful-message",
        )

    async def _delayed_helpful_message(
        self, generation: int, delay: float
    ) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            if (
                generation != self.helpful_message_round_generation
                or self.helpful_message_announced
                or not self._round_is_active()
                or self.transitioning
                or self.final_countdown_active
            ):
                return
            self.helpful_message_announced = True
            await self._announce_helpful_message_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("helpful console message announcement failed")
        finally:
            if self._helpful_message_task is asyncio.current_task():
                self._helpful_message_task = None

    async def helpful_message_announcer(self) -> None:
        if self._round_is_active() and self._helpful_message_task is None:
            self._schedule_helpful_message()
        await self.stop_event.wait()

    def _bootstrap_players_from_lines(
        self, lines: Sequence[str], authoritative: bool = False
    ) -> None:
        seen_players: set[int] = set()
        for line in lines:
            parts = line.split(maxsplit=5)
            if len(parts) < 6:
                continue
            player = self.player_for(parts[0])
            is_new = player is None
            was_connected = bool(player and player.connected)
            player = player or self.player_for(parts[0], create=True)
            assert player
            player.log_name = parts[0]
            # CYCLE_CREATED/CYCLE_DESTROYED and the ladderlog online-status
            # events are authoritative for players already being tracked. The
            # periodically rewritten online_players file can briefly lag those
            # events; allowing it to overwrite alive here can make a scheduled
            # respawn see a stale True value and silently abort. Only use the
            # file's alive bit when discovering or recovering a player.
            if is_new or not was_connected:
                player.alive = parts[1] == "1"
            player.connected = True
            if is_new:
                player.active = False
            player.display_name = parts[5]
            if "@" in parts[0]:
                player.auth_name = parts[0]
            self.register_alias(player, parts[0])
            seen_players.add(id(player))
            self.online_snapshot_misses.pop(id(player), None)

        if authoritative:
            for player in {id(item): item for item in self.players.values()}.values():
                if player.is_ai or id(player) in seen_players:
                    continue
                if not player.connected:
                    continue
                misses = self.online_snapshot_misses.get(id(player), 0) + 1
                self.online_snapshot_misses[id(player)] = misses
                if misses < 2:
                    continue
                player.connected = False
                player.active = False
                player.alive = False
                self.finalists.discard(id(player))
                self._cancel_player_freeze(player)
                self.command_windows.pop(id(player), None)
                self.command_warning_times.pop(id(player), None)

    async def bootstrap_players(self) -> None:
        path = Path(self.config.get("online_players_file", ""))
        while not self.stop_event.is_set():
            try:
                lines = self._decode_game_bytes(
                    path.read_bytes(),
                    "online player snapshot",
                ).splitlines()
                self._bootstrap_players_from_lines(lines[1:], authoritative=True)
            except (OSError, IndexError):
                pass
            await asyncio.sleep(2)

    async def follow_ladderlog(self) -> None:
        path = Path(self.config["ladderlog"])
        handle = None
        inode = None
        while not self.stop_event.is_set():
            try:
                stat = path.stat()
                if handle is None or inode != stat.st_ino or handle.tell() > stat.st_size:
                    if handle:
                        handle.close()
                    handle = path.open("rb")
                    handle.seek(0, os.SEEK_END)
                    inode = stat.st_ino
                    # Ask the native server to retransmit the current snapshot
                    # after this follower is positioned. This makes controller
                    # crashes/reloads safe even if the original snapshot event
                    # was only partially consumed.
                    await self.sink.send("CYCLE_REPLAY_SETTINGS_SNAPSHOT")
                raw_line = handle.readline()
                if raw_line:
                    if raw_line.startswith(b"ENCODING "):
                        fields = raw_line.strip().split()
                        if len(fields) >= 2:
                            with contextlib.suppress(UnicodeDecodeError):
                                self._apply_advertised_game_encoding(
                                    fields[1].decode("ascii")
                                )
                    await self.handle_line(
                        self._decode_game_bytes(raw_line, "ladderlog event")
                    )
                    continue
            except FileNotFoundError:
                pass
            await asyncio.sleep(0.05)
        if handle:
            handle.close()

    async def run(self) -> None:
        await self.initialize(start_http=True)
        loop = asyncio.get_running_loop()
        reload_signal_registered = False
        reload_ready_path: Path | None = None
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(
                signal.SIGUSR1,
                self.request_controller_reload,
                "systemctl reload",
            )
            reload_signal_registered = True
        if reload_signal_registered:
            reload_ready_path = Path(
                self.config.get(
                    "controller_reload_ready_file",
                    "/run/tronner-racing/graceful-reload.pid",
                )
            )
            try:
                reload_ready_path.write_text(
                    f"{os.getpid()}\n", encoding="ascii"
                )
            except OSError:
                LOG.exception(
                    "unable to publish graceful reload readiness at %s",
                    reload_ready_path,
                )
        tasks = [
            asyncio.create_task(self.follow_ladderlog(), name="ladderlog"),
            asyncio.create_task(self.map_timer(), name="map-timer"),
            asyncio.create_task(self.repository_refresher(), name="repository-refresh"),
            asyncio.create_task(self.catalog_state_monitor(), name="catalog-state"),
            asyncio.create_task(self.bootstrap_players(), name="player-bootstrap"),
            asyncio.create_task(
                self.federation_record_sync(),
                name="federation-record-sync",
            ),
            asyncio.create_task(
                self._federation_record_snapshot_sync(),
                name="federation-record-snapshot-sync",
            ),
            asyncio.create_task(
                self.federation_preference_sync(),
                name="federation-preference-sync",
            ),
            asyncio.create_task(
                self.federation_catalog_exclusion_sync(),
                name="federation-catalog-exclusion-sync",
            ),
            asyncio.create_task(
                self.helpful_message_announcer(),
                name="helpful-message-announcer",
            ),
            asyncio.create_task(
                self.server_options_refresher(),
                name="server-options-refresher",
            ),
            asyncio.create_task(
                self.player_activity_monitor(),
                name="player-activity-monitor",
            ),
            asyncio.create_task(
                self.live_dashboard_publisher(),
                name="live-dashboard",
            ),
            asyncio.create_task(
                self.live_dashboard_follower_replays(),
                name="live-dashboard-follower-replays",
            ),
            asyncio.create_task(
                self.server_management_worker(),
                name="server-management",
            ),
            asyncio.create_task(
                self.follow_server_console(),
                name="server-console",
            ),
        ]
        try:
            await self.stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if reload_signal_registered:
                loop.remove_signal_handler(signal.SIGUSR1)
            if reload_ready_path is not None:
                with contextlib.suppress(OSError):
                    if reload_ready_path.read_text(encoding="ascii").strip() == str(
                        os.getpid()
                    ):
                        reload_ready_path.unlink()
            self.close()


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tronner Racing server controller")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="sync and validate maps, then exit")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    config = load_config(args.config)
    controller = TronnerRacing(config)
    if args.check:
        try:
            await controller.initialize(start_http=False)
            print(
                json.dumps(
                    {
                        "maps": len(controller.repository.catalog),
                        "skipped": controller.repository.issues,
                        "spawn_points": sum(
                            len(entry.spawns) for entry in controller.repository.catalog.values()
                        ),
                    },
                    indent=2,
                )
            )
            return 0
        finally:
            controller.close()
    await controller.run()
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("TRONNER_RACING_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
