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
changes, or the requester disconnects. A record is eligible only when its full input replay
matches the exact map revision, map size, finish time, turns, and current
physics snapshot. Historical records without a matching replay are reported as
unavailable.

The controller writes a bounded, mode-0600 one-shot plan under
`/var/lib/armagetronad/ghosts`. The patched server creates an invulnerable,
server-driven cycle, replays accepted turn and brake inputs at their recorded
microsecond offsets, and starts from the human cycle's authoritative release
time. Teleport, speed, and rubber zones still affect playback so the recorded
route remains faithful. Ghosts do not create walls, affect other cycles,
trigger scoring/death/checkpoint zones, or enter racing ladder logs.

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
