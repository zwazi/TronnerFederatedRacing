# Permanent region rollout checklist

This checklist records the operational lessons needed for a permanent region
without publishing production inventory or credentials.

## Before installation

1. Assign a unique server ID, short region label, public endpoint, and overlay
   address. Keep the production inventory outside Git.
2. Confirm cross-region routing. Cloud-provider VPC addresses may be scoped to
   one region; build and test the WireGuard overlay before federation traffic.
3. Generate the candidate's WireGuard private key on that host. Approve only
   its public key and credential-free enrollment request.
4. Issue fresh directional HMAC keys for the candidate/leader pair. Do not copy
   an existing follower's identity or keys.
5. Add the member to every node's cluster registry and add exactly one new peer
   to the leader. The candidate peers only with the leader.
6. Keep Firebase service identities, endpoints, account-link secrets, and
   production URLs in the private operator configuration. Public examples are
   deliberately inert.

## Constrained hosts

A small game node does not need to compile C++ in production. Build the pinned
engine and patch on a compatible build host or in CI, record the SHA-256 digest,
copy the install prefix over an authenticated channel, verify the digest on the
candidate, run `ldd` and `--version` on the candidate, and install with
`--skip-engine-build`. A binary built on a newer distribution can require a
newer shared-library ABI even when the CPU architecture matches. If no
compatible builder is available, add bounded swap and build with one job on the
unlisted candidate. This avoids an avoidable out-of-memory failure while
preserving reproducibility.

## Commissioning order

1. Install packages, engine, controller, sidecar, private configuration, and
   keys without starting the game or advertising it.
2. Enable the overlay and restrict application UDP to the overlay interface and
   expected peer. Verify the public interface cannot reach the federation port.
3. Validate the sidecar configuration as the restricted service user.
4. Start the candidate unlisted. Verify signed heartbeats, all regional player
   namespaces, follower-to-follower relay, map parity, and shared round release.
5. Verify a local finish reaches the leader, its acknowledgement returns to the
   candidate, and the public leaderboard does not gain duplicates.
6. Verify map resources with an external client and check live/admin website
   state, account linking, console output, and command acknowledgement.
7. Stop the candidate or its tunnel and prove healthy existing regions keep
   releasing rounds. Restore it and prove it resynchronizes.
8. Only after the observation window, deliberately enable master-list
   advertising and retain the unlisted configuration as the rollback target.

## Rollback

Disable candidate advertising, stop its game/controller/sidecar, remove its
leader peer and WireGuard admission, and remove only its directional keys.
Existing followers keep their identities and credentials. Restore the leader's
previous reviewed configuration if the candidate cannot be isolated cleanly.
