# Adding an approved region

Protocol v2 supports adding a follower to the leader hub. Each new region gets
its own identity, overlay address, and directional key pair. Never reuse an
existing identity, keys, or overlay address.

## 1. Prepare the private network

Create a WireGuard peer for the candidate host in the private operator
inventory. The candidate keeps its private key locally and shares only its
public key. Confirm that the candidate and leader can reach each other's
overlay address and that the application federation port is unavailable on
every public interface. Provider-private addresses are not necessarily routed
between regions; use an explicit cross-region overlay rather than assuming
they are.

## 2. Create a credential-free request

Run on the candidate or an operator workstation:

```sh
python3 deploy/enrollment.py request \
  --server-id region-b \
  --region-label B \
  --overlay-address 10.77.0.2 \
  --wireguard-public-key PUBLIC_KEY \
  --output /tmp/region-b-request.json
```

The request contains no secret and cannot authenticate a packet. Transfer it
to an administrator for manual identity, ownership, address, and capacity
review.

## 3. Approve and create pair credentials

On a trusted administrative machine:

```sh
python3 deploy/enrollment.py approve \
  --request /tmp/region-b-request.json \
  --leader-node /secure/operator/leader-node.json \
  --leader-overlay-address 10.77.0.1 \
  --output /secure/operator/region-b-enrollment
```

The output directory is mode `0700`. It contains separate leader and follower
bundles, two 32-byte directional keys, configuration fragments, and a record of
the non-secret fingerprints. Copy bundles with SSH or another authenticated
administrative channel. Never commit the directory.

Append the leader bundle's `peer` object to the leader's `federation.peers`.
Use the follower bundle's `federation` object on the candidate. Add the new
server ID and region label to the cluster `members` registry distributed to
every node. Store each bundle's `secrets` files only in that node's private
installer secrets directory.

Set the follower node's `federation.leader_resource_base_url` to the leader's
private overlay map mirror, for example `http://10.77.0.1:8080/`. The renderer
rejects a URL whose host is not the configured leader overlay peer.

## 4. Commission unlisted

1. Install with `master_list` false.
2. Validate sidecar configuration and key permissions.
3. Confirm signed heartbeat and player snapshots in both directions and
   origin-preserving relay between the new and existing followers.
   Run `tools/check_federation_health.py` on every node; all must be healthy.
4. Run disposable identity/color/cycle and synchronized-round smoke tests.
5. Confirm the new follower completes a leader preference snapshot before
   accepting players; saved spawns and start modes must match an existing node.
6. Confirm every node receives the same map and releases the round with a
   synchronized marker rather than its safety timeout.
7. Test revocation by stopping the tunnel or removing the peer firewall rule.
8. Re-enable the peer, then deliberately opt into the public master list.

## Revocation

Stop federation on the candidate, remove its leader WireGuard peer and firewall
admission, remove the candidate from the leader peer registry, archive the
enrollment record as revoked, and remove that pair's two HMAC keys. If either
key may have been copied elsewhere, generate a new pair before reconnecting.
No unrelated node credential needs rotation.
