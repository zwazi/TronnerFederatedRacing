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

Route distance, rather than straight-line distance, permits a player to move
away from the winzone temporarily when a wall or death zone makes that detour
necessary. A higher-resolution retry is used for narrow maps. If the bounded
field still cannot certify a spawn-to-winzone route, the server script disables
route judgments for that map and retains the native input-idle fallback. It
does not substitute straight-line distance.

Checkpoint maps are also conservative: until a player has collected every
required checkpoint, the winzone field is not used to judge that player.

## Enforcement

The rolling trajectory supplies ground speed, route-progress speed, and route
efficiency. The server script detects four sustained conditions:

1. movement below a map-relative minimum speed;
2. insufficient progress to finish in the remaining time;
3. increasing route distance, meaning the racer is driving away; and
4. substantial movement with little route progress, such as a fast circle.

The minimum pace, efficiency requirement, observation window, and post-warning
grace period become stricter as the countdown approaches zero. Thresholds are
calibrated with the map's median spawn-to-finish route distance and record time,
so maps with different coordinate scales do not share one arbitrary speed.

The first sustained violation sends a private warning. If the rolling window
recovers, the warning state is cleared. If the violation continues through the
time-dependent grace period, the server script sends `KILL_SILENT` once and logs
the measured ground speed, route speed, required speed, reason, map, stable
player identity, and remaining time.

The standalone server script evaluates the guard and, if necessary, kills only
the stalled local cycle.
