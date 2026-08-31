# Architecture

## Two data paths

Each region runs three processes:

```text
Armagetron engine <-> Unix datagrams <-> federation sidecar <-> authenticated UDP
        |                                      |
        +---- ladderlog/console ---- controller+
```

The engine owns simulation and client networking. The controller owns map
rotation, timing, respawn behavior, records, and optional external publishing.
The sidecar has only two jobs: authenticate/route low-rate shared state, and
relay best-effort cycle motion without blocking control state. It rejects
malformed, stale, replayed, incorrectly identified, or incorrectly signed
datagrams before forwarding normalized local events.

Protocol v2 uses a bounded leader hub. Every follower has an independently
authenticated link to the configured leader; followers do not connect directly
to each other. The leader verifies a follower's transport identity, preserves
the event's origin while re-signing it for each destination, and fans shared
events out to the other followers. The leader is authoritative for map transitions and
synchronized round release. Every region remains authoritative for its local
client connections and sends personal-best deltas toward the leader.

Follower commands, observed follower maps, round-ready notices, and PB snapshot
requests terminate at the leader. They are not fanned out to other followers.
Presence, chat, motion, and accepted PB deltas are shared. This keeps one hub
without pretending every event is global.

## Ownership

| State | Authority | Distribution |
| --- | --- | --- |
| Map choice, timer, queue, global commands | leader controller | authenticated control packets |
| Exact selected map XML | leader immutable mirror | follower fetch over the private overlay, authenticated SHA-256 in `map_prepare` |
| Local players, finishes, respawns | each local engine/controller | normalized edges and snapshots |
| Remote cycle visuals | each local engine | best-effort coalesced UDP telemetry |
| Authenticated personal bests | node that observed the finish, merged idempotently | PB deltas through the leader |
| Public website state | leader unless explicitly documented otherwise | external publisher |

Only the leader continuously watches Firebase catalog invalidation. Followers
load their last catalog at startup, then obtain the exact selected immutable
map from the leader. Firebase remains the publishing authority, but is no
longer a timing dependency between regions during a map change.

## Trust boundaries

- Public game and resource ports accept ordinary players and map downloads.
- Controller and engine federation sockets are Unix sockets accessible only to
  the restricted service account.
- Federation UDP is an operator network, not a public enrollment API.
- Each direction of each leader/follower pair has a separate secret of at
  least 32 random bytes.
- The receiving side checks cluster ID, transport sender, semantic origin,
  destination, timestamp, per-peer sequence window, HMAC signature, and
  expected source address.
- Firebase is optional. When enabled, the service-account file is read from the
  host and is never part of an installation bundle or Git checkout.

## Runtime state

SQLite records, recordings, preferences, generated map mirrors, logs, reports,
and downloaded catalog data are runtime state beneath `/var/lib` or the system
journal. They are not required to reproduce the software and must not enter the
source repository.

## Failure behavior

The leader releases a round after every currently healthy follower is ready.
A peer that has exceeded the configured health timeout does not stall the
remaining fleet. Remote player state is namespaced by origin, so identical
engine player IDs in two regions cannot collide. Record deltas are idempotent
and acknowledgements name their destination. Protocol v1 and v2 envelopes have
strict, separate schemas; an older receiver rejects v2 instead of accepting
ambiguous authority.

The sidecar writes `/run/tronner-federation/health.json` once per second. It
contains no credentials or player addresses; it records the last authenticated
packet of each kind from every semantic origin. `tools/check_federation_health.py`
combines that evidence with local socket readiness and current-map hashes.
