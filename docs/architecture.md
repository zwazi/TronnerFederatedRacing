# Architecture

## Data path

Each region runs three processes:

```text
Armagetron engine <-> Unix datagrams <-> federation sidecar <-> authenticated UDP
        |                                      |
        +---- ladderlog/console ---- controller+
```

The engine owns simulation and client networking. The controller owns map
rotation, timing, respawn behavior, records, and optional external publishing.
The sidecar keeps federation traffic off the game thread and rejects malformed,
stale, replayed, incorrectly identified, or incorrectly signed datagrams before
forwarding normalized local events.

Protocol v2 uses a bounded leader hub. Every follower has an independently
authenticated link to the configured leader; followers do not connect directly
to each other. The leader verifies a follower's transport identity, preserves
the event's origin while re-signing it for each destination, and fans it out to
the other followers. The leader is authoritative for map transitions and
synchronized round release. Every region remains authoritative for its local
client connections and sends personal-best deltas toward the leader.

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
