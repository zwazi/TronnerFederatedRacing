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

The current protocol release supports one leader and one follower. The leader
is authoritative for map transitions and synchronized round release. Both
regions remain authoritative for their local client connections and send
personal-best deltas to the leader.

## Trust boundaries

- Public game and resource ports accept ordinary players and map downloads.
- Controller and engine federation sockets are Unix sockets accessible only to
  the restricted service account.
- Federation UDP is an operator network, not a public enrollment API.
- Each direction has a separate secret of at least 32 random bytes.
- The receiving side checks cluster ID, server ID, timestamp, sequence window,
  HMAC signature, and expected source address.
- Firebase is optional. When enabled, the service-account file is read from the
  host and is never part of an installation bundle or Git checkout.

## Runtime state

SQLite records, recordings, preferences, generated map mirrors, logs, reports,
and downloaded catalog data are runtime state beneath `/var/lib` or the system
journal. They are not required to reproduce the software and must not enter the
source repository.

## Future multi-region protocol

More than two simultaneous regions requires an explicit protocol revision,
not duplicated sidecars sharing an identity. The intended design is a leader
hub with one credential per follower, origin-preserving fan-out, a configured
capability set per node, and a readiness set that excludes unhealthy regions.
The wire version must remain fail-closed: an older receiver must reject the new
envelope instead of silently accepting ambiguous authority.
