# Multi-region roadmap

The current release deliberately supports one leader and one follower. It is
safe for reproducing the present topology or replacing a region, but it must
not be stretched to three nodes by reusing identities or credentials.

## Gate 1: public, reproducible two-node baseline

- Keep source, configuration schema, enrollment tooling, and smoke tests in
  this repository.
- Keep inventory, credentials, player data, recordings, logs, and deployments
  in private operator systems.
- Require independent directional keys, exact peer identities and addresses,
  private-network admission, CI, and an unlisted commissioning window.

This gate is implemented by the initial repository release.

## Gate 2: versioned multi-peer envelope

- Introduce a new protocol version with explicit origin and destination IDs.
- Give every leader/follower pair independent directional credentials and
  replay windows; never share one cluster-wide signing key.
- Configure a bounded peer registry with per-peer capabilities such as
  presence, cycles, chat, records, map control, and round readiness.
- Have the leader validate and fan out origin-preserving events. Followers do
  not send packets directly to each other.
- Reject unknown protocol versions, identities, destinations, capabilities,
  and origin/transport mismatches.

This requires implementation and compatibility tests before a third live node
is admitted.

## Gate 3: authority and failure behavior

- Keep one elected-by-configuration leader authoritative for maps, countdowns,
  record ordering, and round release. Automatic leader election is out of
  scope until split-brain handling exists.
- Track readiness per healthy region. A disconnected or commissioning region
  must not stall active regions indefinitely.
- Define idempotent record-delta IDs and leader acknowledgements so reconnects
  cannot lose or duplicate personal bests.
- Bound queues and stale presence per peer so one slow region cannot delay the
  leader or another follower.

## Gate 4: controlled regional rollout

For each region: review a credential-free enrollment request, approve network
and node identity manually, issue only that pair's credentials, install it
unlisted, run the full validation suite, observe a natural map transition and
record round-trip, test revocation, then opt into the public master list.

Rollback removes only the new peer's network admission and keys. Existing
regions continue with their own credentials. No public pull request or clone
can enroll itself or reach production Firebase resources.
