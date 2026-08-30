# Multi-region design and rollout gates

The current release supports a bounded leader hub with multiple followers.
The gates below document what is implemented and what operators must verify
before admitting each permanent region.

## Gate 1: public, reproducible two-node baseline

- Keep source, configuration schema, enrollment tooling, and smoke tests in
  this repository.
- Keep inventory, credentials, player data, recordings, logs, and deployments
  in private operator systems.
- Require independent directional keys, exact peer identities and addresses,
  private-network admission, CI, and an unlisted commissioning window.

This gate is implemented by the initial repository release.

## Gate 2: versioned multi-peer envelope — implemented

- Introduce a new protocol version with explicit origin and destination IDs.
- Give every leader/follower pair independent directional credentials and
  replay windows; never share one cluster-wide signing key.
- Configure a bounded peer registry with a maximum of 16 members.
- Have the leader validate and fan out origin-preserving events. Followers do
  not send packets directly to each other.
- Reject unknown protocol versions, identities, destinations, capabilities,
  and origin/transport mismatches.

The transport has unit tests plus a live three-sidecar UDP test proving
follower-to-follower relay and leader control fan-out.

## Gate 3: authority and failure behavior — implemented

- Keep one elected-by-configuration leader authoritative for maps, countdowns,
  record ordering, and round release. Automatic leader election is out of
  scope until split-brain handling exists.
- Track readiness per healthy region. A disconnected or commissioning region
  must not stall active regions indefinitely.
- Define idempotent record-delta IDs and leader acknowledgements so reconnects
  cannot lose or duplicate personal bests.
- Bound queues and stale presence per peer so one slow region cannot delay the
  leader or another follower. A send failure to one peer is isolated from the
  remaining fan-out.

## Gate 4: controlled regional rollout

For each region: review a credential-free enrollment request, approve network
and node identity manually, issue only that pair's credentials, install it
unlisted, run the full validation suite, observe a natural map transition and
record round-trip, test revocation, then opt into the public master list.

Rollback removes only the new peer's network admission and keys. Existing
regions continue with their own credentials. No public pull request or clone
can enroll itself or reach production Firebase resources.
