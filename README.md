# Tronner Federated Racing

Tronner Federated Racing combines a patched Armagetron Advanced dedicated
server, an external racing controller, and an authenticated UDP sidecar.
Approved regional nodes can run the same map and round while exchanging player
presence, chat, cycle telemetry, commands, and personal-best updates.

This repository contains source code and reproducible deployment material. It
does **not** contain a live server snapshot. Player records, replays, logs,
reports, bans, map overrides, production addresses, Firebase configuration,
and credentials belong outside Git.

## Security boundary

Cloning this repository does not grant membership in an existing federation.
A fresh installation is standalone, Firebase-disabled, and absent from the
public master list. Federation requires all of the following:

1. an operator-created node configuration;
2. a unique directional secret for each direction of every approved pair;
3. the expected peer server IDs and overlay-network addresses;
4. firewall or WireGuard admission by the federation operator.

Packets use HMAC-SHA256 authentication, bounded timestamps, server and cluster
identities, replay windows, datagram limits, and source-address checks. See
[Security](docs/security.md) and [Adding a region](docs/adding-a-region.md).

## Repository contents

- `controller/` — the race controller, Firebase adapter, live publisher,
  federation protocol/sidecar, and tests;
- `engine/` — the exact upstream revision and a reviewable patch, never a
  committed binary;
- `config/` — inert examples containing documentation-only addresses;
- `deploy/` — renderer, installer, service units, and disposable smoke tests;
- `tools/` — checks that prevent runtime data and credentials from entering
  the repository.

## Quick start

Start by copying the examples outside the checkout:

```sh
cp config/cluster.example.json /secure/operator/path/cluster.json
cp config/node.example.json /secure/operator/path/node.json
```

Edit both files, then render and inspect the result without changing the host:

```sh
python3 deploy/render_node.py \
  --cluster /secure/operator/path/cluster.json \
  --node /secure/operator/path/node.json \
  --output /tmp/tronner-rendered
```

The example configuration intentionally cannot be started in production. A
federated node also needs an approved enrollment bundle. Installation and
rollout are documented in [Installation](docs/installation.md).

## Current topology

Protocol v2 supports a bounded leader-hub topology. The leader authenticates
every follower independently and relays origin-preserving events; a follower
peers only with the leader. Map changes, countdowns, record ordering, and round
release remain leader-authoritative. Never reuse an identity or a pair's keys
for another region. See [Adding a region](docs/adding-a-region.md) for the
commissioning workflow and the [multi-region design](docs/multi-region-roadmap.md)
for authority and failure behavior. The repeatable production sequence is in
the [permanent-region rollout checklist](docs/production-rollout.md).

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --requirement requirements.txt
python3 -m py_compile controller/*.py deploy/*.py tools/*.py
python3 -m unittest discover -s controller -p 'test_*.py'
python3 -m unittest discover -s deploy -p 'test_*.py'
python3 -m unittest discover -s tools -p 'test_*.py'
python3 tools/check_public_tree.py .
```

The engine patch is based on the revision in `engine/UPSTREAM_COMMIT`. CI
verifies that it applies cleanly and builds it before publishing any release
artifact. See [Validation](docs/validation.md) for the isolated engine and
multi-node smoke-test workflow. The expensive engine job runs only when engine,
build, or smoke-test paths change; routine documentation and dependency updates
use the fast test job.

## License

This project is licensed under GPL-2.0-or-later. The Armagetron-derived engine
changes retain the upstream copyright and licensing notices.
