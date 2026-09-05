# Private replay ghosts

`/ghost` selects a recorded run to compare against on the current map:

```text
/ghost
/ghost pb
/ghost wr
/ghost rank 5
/ghost player Alice
/ghost off
```

`/ghost` defaults to PB. PB uses the requester's best time, WR uses rank 1, and
rank/name selectors use the current map's leaderboard. A selection starts with
the requester's next attempt and remains active until it is disabled, the map
changes, or the requester disconnects. The controller prefers the exact replay
for the selected leaderboard time. If that time predates input capture, it uses
the player's fastest available full-run replay and states both the replay and
ranked times in the confirmation.

Historical resource names and revisions remain eligible when a strict XML
comparison proves that their physical track geometry is identical to the active
map. The comparison ignores map-local settings, checks those settings against
the captured server physics separately, and normalizes spatial coordinates by
`SIZE_FACTOR`. This also converts a legacy replay's start position when an old
size-scaled map was migrated to baked coordinates. A replay remains unavailable
when its inputs are missing or invalid, its geometry genuinely changed, or any
movement/zone physics setting differs.

Physics compatibility compares the captured setting values, not only the raw
snapshot identifier. Runtime-only `SERVER_OPTIONS` text and
`PING_CHARITY_SERVER` latency allowance are ignored because they cannot affect
the route of a server-driven, non-colliding ghost. `SIZE_FACTOR` is compared via
the physical-map normalization above; movement and zone settings must still
match exactly, including every captured mid-run settings transition.

The controller writes a bounded, mode-0600 one-shot plan under
`/var/lib/armagetronad/ghosts`. The patched server creates an invulnerable,
server-driven cycle, replays accepted turn and brake inputs at their recorded
microsecond offsets, and starts from the human cycle's authoritative release
time. Teleport, speed, and rubber zones still affect playback so the recorded
route remains faithful. Ghosts do not create walls, affect other cycles,
trigger scoring/death/checkpoint zones, or enter racing ladder logs.

Newly recorded replays also store the authoritative position, direction, speed,
and turn count after every accepted turn. The server applies these turn
keyframes after the recorded input and clears the ordinary delayed-input queue,
preventing simulation drift near walls and preventing a queued turn from firing
later as an apparent extra input. Version-1 plans and older runs without turn
keyframes remain supported as best-effort input-only replays.

Replay plans store positions in map coordinates. The server converts those
positions through the arena size multiplier when it creates the ghost, matching
the normal player spawn path on both size-zero and resized maps.

Administrators can recover start fields written by older controller versions
from retained authoritative ladder logs. The repair defaults to a dry run,
requires a new integrity-checked database backup for `--apply`, and skips every
ambiguous match:

```sh
python3 tools/repair_replay_starts.py \
  --database /var/lib/tronner-racing/TronnerRacing.sqlite3 \
  --ladderlog /var/lib/armagetronad/ladderlog.txt
```

## Legacy client compatibility

No new network descriptor or client feature is required. The ghost is sent as
the existing player descriptor 201 and cycle descriptor 320 used by unmodified
0.2.8 clients. A wire-only negative cycle distance prevents legacy clients from
predicting a wall for the ghost. Because 0.2.8 has no translucent-cycle protocol,
the compatibility rendering is an ordinary cyan cycle named for the selected
slot.

Visibility is filtered per network connection. Other clients receive neither
the ghost player nor its cycle. Multiple local players using one split-screen
connection necessarily share that connection's view, which is a limitation of
the legacy protocol.
