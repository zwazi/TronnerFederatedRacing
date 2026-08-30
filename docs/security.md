# Deployment security

Open source and production federation membership are separate concerns. Source
availability reveals the packet format, but authenticated membership still
requires node-specific secrets, an approved identity, and network admission.

## Required controls

1. Put federation traffic on WireGuard or another operator-controlled private
   network. Bind `listen_host`, every peer `host`, and `expected_peer_ip` to
   overlay addresses.
2. Allow federation UDP only on that interface and only from configured peers.
   Never expose the application federation port to the general internet.
3. Generate independent directional HMAC keys for every pair. Never use the
   same key in both directions or across pairs.
4. Keep `/etc/tronner-federation/keys` owned by `root:armagetron`, mode `0750`,
   and key files mode `0640` or stricter.
5. Keep clocks synchronized. Packets outside the configured clock window are
   rejected.
6. Start new nodes unlisted. Enable `master_list` only after the authenticated
   smoke test and rollback test pass.
7. Revoke a compromised node by removing its network peer, closing its firewall
   rule, stopping federation, and rotating only that pair's HMAC keys.

## Firebase

Firebase is disabled in the examples. Prefer leader-only Firebase access so a
follower compromise cannot write directly to the project. Use a dedicated
service identity with the narrowest permissions supported by the enabled
features. Browser API configuration is not authorization; database/storage
rules, App Check where applicable, and authenticated administrative claims must
enforce the boundary.

Do not put project IDs, database URLs, service-account files, or administrative
credentials in this repository. Store production configuration in a private
operator inventory and deliver credentials directly to the host.

## GitHub

- Keep production deployment workflows and inventories outside this public
  repository.
- Give Actions read-only permissions unless a release job needs scoped content
  write access.
- Never expose environment secrets to workflows triggered from forks.
- Require CI and review on the default branch.
- Enable GitHub secret scanning and push protection before changing visibility.
- Rotate credentials if a scanner reports them; deleting a file in a later
  commit does not remove it from history.

## Existing protocol defenses

The application protocol uses canonical JSON, HMAC-SHA256, constant-time
signature comparison, explicit transport/origin/destination identities, strict
fields and identifiers, a 16 KiB datagram cap, clock-skew validation, and a
bounded replay window per peer. The leader rejects a follower that claims
another origin. These protect authenticity and freshness but do not replace
host firewalling or private-network isolation.
