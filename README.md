# Tronner Racing

Tronner Racing is one standalone Armagetron Advanced racing server in New
York. It combines a patched dedicated server, the Python race controller, the
Firebase map catalog, live status, records, replays, and the tronner.io admin
controls.

There are no regional peers, shared rounds, or multi-server authority roles.
Do not add another server to the runtime or deployment model without a new,
explicit product decision.

## Repository contents

- `controller/` — racing, maps, respawns, records, ratings, live publishing,
  admin commands, and tests;
- `engine/` — the pinned upstream revision and Tronner gameplay patch;
- `config/` — inert standalone examples;
- `deploy/` — the standalone renderer, installer, units, and smoke probes;
- `tools/` — repository safety checks.

Production state, player records, replays, logs, reports, bans, Firebase
credentials, and server addresses do not belong in Git.

## Quick start

Copy the examples outside the checkout, replace the documentation values, and
render the configuration before installing it:

```sh
cp config/cluster.example.json /secure/operator/path/service.json
cp config/node.example.json /secure/operator/path/server.json
python3 deploy/render_node.py \
  --cluster /secure/operator/path/service.json \
  --node /secure/operator/path/server.json \
  --output /tmp/tronner-rendered
```

See [Installation](docs/installation.md) and
[Validation](docs/validation.md).

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

The engine patch is based on `engine/UPSTREAM_COMMIT`. CI verifies that it
applies cleanly and builds before a release artifact is published.

## License

GPL-2.0-or-later. Armagetron-derived engine changes retain upstream copyright
and licensing notices.
