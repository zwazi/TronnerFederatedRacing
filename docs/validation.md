# Validation

Run the fast checks before every commit:

```sh
python3 -m pip install --requirement requirements.txt
python3 -m py_compile controller/*.py controller/hot_commands/*.py deploy/*.py tools/*.py
python3 -m unittest discover -s controller -p 'test_*.py'
python3 -m unittest discover -s deploy -p 'test_*.py'
python3 -m unittest discover -s tools -p 'test_*.py'
python3 tools/check_public_tree.py .
bash -n deploy/*.sh deploy/smoke/*.sh
```

The engine build defaults to isolated temporary source and build directories:

```sh
TRONNER_ENGINE_PREFIX=/tmp/tronner-engine ./deploy/build_engine.sh
```

To retain a disposable build for the protocol smoke tests, provide all three
paths explicitly:

```sh
export TRONNER_ENGINE_SOURCE_DIR=/tmp/tronner-validation/source
export TRONNER_ENGINE_BUILD_DIR=/tmp/tronner-validation/build
export TRONNER_ENGINE_PREFIX=/tmp/tronner-validation/prefix
export TRONNER_ENGINE_DATA_DIR="$TRONNER_ENGINE_PREFIX/share/games/armagetronad-dedicated"
export TRONNER_ENGINE_CONFIG_DIR="$TRONNER_ENGINE_PREFIX/etc/games/armagetronad-dedicated"
export SMOKE_PROBE_DIR=/tmp/tronner-validation/probes

./deploy/build_engine.sh
./deploy/smoke/build_probes.sh
./deploy/smoke/run_round_sync_smoke.sh
./deploy/smoke/run_bidirectional_smoke.sh
DISPLAY_SERVER_TAGS=1 ./deploy/smoke/run_bidirectional_smoke.sh
python3 controller/test_three_region_transport.py
```

These tests bind only loopback ports, never advertise to the master list, use
temporary runtime directories, and clean them on exit. They do not contact a
production controller, peer, Firebase project, or map repository.

## Live node check

After deployment, run this on every region:

```sh
python3 /opt/TronnerRacing/check_federation_health.py
```

A healthy result proves that fresh authenticated heartbeats and presence
snapshots from every other semantic origin reached that node, all four local
Unix sockets exist, and the current map has identical bytes in the engine cache
and public mirror. Run it on all nodes to prove every required direction. The
check is read-only and does not restart or reload anything.

For a commissioned node, build `mesh_client_probe` against the installed
patched engine and run one short observer connection from every region to every
other region:

```sh
export TRONNER_ENGINE_DATA_DIR=/opt/armagetronad/share/games/armagetronad-dedicated
/path/to/mesh_client_probe HOST:PORT observer 2500
```

The `PROBE` milestones record login latency, received players, federation ghost
identity, current map settings, and clean disconnect. An optional final
argument sends one bounded chat command. `/nextmap` is a useful non-mutating
round-trip canary on a follower: its reply proves follower engine -> follower
sidecar -> leader controller -> follower sidecar -> follower engine delivery.
These are one-shot clients; do not leave probes connected or use them as a
keepalive service.
