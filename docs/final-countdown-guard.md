# Final-countdown progress guard

When the ordinary map timer expires, respawns stop and every living racer is
allowed to finish. A moving cycle is not necessarily an active racer: an
unattended cycle keeps driving, and a player can deliberately circle or drive
away from the finish to delay the map transition.

The **Final Countdown Progress Guard** therefore requests one-second player
activity snapshots and tracks every local finalist from the beginning of the
countdown. It uses a cumulative wrong-way allowance rather than trying to judge
whether a player is fast enough to finish.

## Route model

When a map becomes active, the server script reads its immutable XML and
preloads a bounded distance-to-finish field before the countdown. The field:

- uses the map's regular or explicitly configured axis directions;
- blocks static wall segments, death circles, and death polygons;
- treats absolute, map-relative, and cycle-relative teleport zones as directed
  zero-travel-distance route edges;
- accepts every circular or polygonal winzone as a goal; and
- is built in a worker thread with a configured cell limit.

Armagetron target zones are also finish goals because `TARGET_DECLARE_WINNER`
is enabled by default. Every declared spawn must have a certified route; one
reachable spawn cannot hide another disconnected spawn.

Navigation edges are checked against the exact XML wall segments and death-zone
boundaries. Walls therefore cannot disappear between cell centers, while real
narrow wall-to-wall, wall-to-zone, and zone-to-zone passages are not erased by
artificially widening every obstacle to the raster-cell size.

The primary field is allowed up to 250,000 cells with a 0.5-unit minimum cell
size. A disconnected field retries with up to 500,000 cells and a 0.1-unit
minimum. In addition, every field—not only a fully disconnected one—merges a
sparse sub-cell graph. Exact-collision rings around wall endpoints and
centerlines through nearby wall/wall, wall/death-zone, and
death-zone/death-zone boundaries preserve alternate tight passages without
allocating a microscopic grid across the whole arena.
The supplemental graph is capped by both point count and obstacle complexity;
very large maps retain the higher-resolution raster without allowing auxiliary
collision checks to occupy the controller CPU indefinitely.

Completed fields are persisted under the server-script state directory. The cache
key includes the map XML digest, field algorithm version, size multiplier, and
all resolution and clearance inputs, so immutable revisions reuse their field
while any meaningful input change builds a separate entry. Cache retention is
bounded to 768 entries and 512 MiB by default.

Route distance, rather than straight-line distance, recognizes a necessary
detour around a wall or death zone as progress along the real route. If the
bounded field still cannot certify a spawn-to-winzone route, the server script
disables route judgments for that map and retains the native input-idle
fallback. It does not substitute straight-line distance.

Teleport destinations use the same `abs`, `rel`, and `cycle` formulas as the
patched game engine. The route field can chain multiple teleports. Teleport
displacement shortens the remaining route but is excluded from measured driving
distance, so a large jump cannot be mistaken for extreme cycle speed.

Checkpoint maps are also conservative: until a player has collected every
required checkpoint, the winzone field is not used to judge that player.

## Enforcement

Each player starts the countdown with five seconds of cumulative wrong-way
allowance. Whenever the wall-aware remaining route distance increases, elapsed
sample time is deducted from that allowance. Moving toward the winzone pauses
the deduction but never restores time already used. Small distance-field jitter
is neutral and does not consume the allowance.

A sustained wrong-way episode produces a private warning after one second. If
the player resumes progress and later starts another sustained wrong-way
episode, another warning is sent with the remaining allowance. When the
cumulative total reaches five seconds, the server script sends `KILL_SILENT`
once and records the removal in the controller log.

A continuously stationary cycle receives a private warning after one second
and is removed after five seconds. Successful replay turn events are also
tracked during the countdown; a sixteenth consecutive turn in the same
direction removes the cycle, while switching direction resets that sequence.

The server script no longer kills a player merely because an ETA projection
says the player is too slow. Cycle acceleration therefore needs no special
estimate for this rule: actual movement through the wall- and teleport-aware
route field is authoritative. The ordinary countdown expiry still ends runs
that do not finish in time.

Field construction runs in a worker thread. A first-time field that is still
building when the countdown begins retains player positions and replays them
through the completed field. Stationary and repeated-turn enforcement does not
depend on route-field readiness. A route sample that is not represented by the
field is never replaced with a straight-line guess; that map uses the existing
input-idle fallback instead.

The standalone server script evaluates the guard and, if necessary, kills only
the stalled local cycle.
