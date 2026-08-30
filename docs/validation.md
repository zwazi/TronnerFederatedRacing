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
```

These tests bind only loopback ports, never advertise to the master list, use
temporary runtime directories, and clean them on exit. They do not contact a
production controller, peer, Firebase project, or map repository.
