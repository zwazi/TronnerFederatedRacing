# Adding an approved region

The current wire release supports a two-node federation. This workflow is safe
for commissioning or replacing the follower. Do not add a second follower by
reusing the existing identity, keys, sockets, or port.

## 1. Prepare the private network

Create a WireGuard peer for the candidate host in the private operator
inventory. The candidate keeps its private key locally and shares only its
public key. Confirm that each node can reach the other's overlay address and
that the application federation port is unavailable on the public interface.

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

Merge each `federation-fragment.json` object into that node's `federation`
section. Store the matching bundle's `secrets` files in the node's private
installer secrets directory.

## 4. Commission unlisted

1. Install with `master_list` false.
2. Validate sidecar configuration and key permissions.
3. Confirm signed heartbeat and player snapshots in both directions.
4. Run disposable identity/color/cycle and synchronized-round smoke tests.
5. Confirm the node receives the same map and releases the round with a
   synchronized marker rather than its safety timeout.
6. Test revocation by stopping the tunnel or removing the peer firewall rule.
7. Re-enable the peer, then deliberately opt into the public master list.

## Revocation

Stop the federation service on both sides, remove the WireGuard peer and
firewall admission, archive the enrollment record as revoked, and remove the
pair's two HMAC keys. If either key may have been copied elsewhere, generate a
new pair before reconnecting. No unrelated node credential should need
rotation.
