# Final-countdown progress guard

When the ordinary map timer expires, respawns stop and every living racer is
allowed to finish. A moving cycle is not necessarily an active racer: an
unattended cycle keeps driving, and a player can deliberately circle or drive
away from the finish to delay the map transition.

The server script therefore requests one-second player activity snapshots during
the final countdown and evaluates each local finalist over a rolling window.
It never treats one sample or one wrong turn as griefing.

## Route model

At countdown start, the server script reads the immutable active map and builds a
bounded distance-to-finish field. The field:

- uses the map's regular or explicitly configured axis directions;
- blocks static wall segments, death circles, and death polygons;
- accepts every circular or polygonal winzone as a goal; and
- is built in a worker thread with a configured cell limit.

Walls use a conservative raster supercover: every grid cell intersected by a
zero-width XML wall is blocked. This prevents a wall that happens to fall
between cell centers from disappearing from the navigation field.

Route distance, rather than straight-line distance, permits a player to move
away from the winzone temporarily when a wall or death zone makes that detour
necessary. A higher-resolution retry is used for narrow maps. If the bounded
field still cannot certify a spawn-to-winzone route, the server script disables
route judgments for that map and retains the native input-idle fallback. It
does not substitute straight-line distance.

Checkpoint maps are also conservative: until a player has collected every
required checkpoint, the winzone field is not used to judge that player.

## Enforcement

The rolling trajectory supplies two independent answers:

1. **Can the racer finish in time?** The server script divides the wall-aware
   distance from the racer's current position by their measured ground speed.
   The resulting projected travel time must fit inside the countdown time that
   remains.
2. **Is the racer making consistent progress?** The wall-aware distance must
   trend downward by more than the route field's small raster-noise allowance
   over the rolling observation window. Fast circles and sustained movement
   away from the valid route therefore fail even when ground speed is high.

The observation window and post-warning grace period become shorter as the
countdown approaches zero. A single sample, route-field jitter, or one brief
wrong turn does not trigger enforcement.

If either answer is false for a complete observation window, the racer receives
a private warning. If both answers recover, the warning state is cleared. If
either condition remains false through the time-dependent grace period, the
server script sends `KILL_SILENT` once and logs both decisions, projected travel
time, measured ground and route speeds, required speed, reason, map, stable
player identity, and remaining time.

The standalone server script evaluates the guard and, if necessary, kills only
the stalled local cycle.
