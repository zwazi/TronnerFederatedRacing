# Validation

Run from the repository root:

```sh
python3 -m py_compile controller/*.py deploy/*.py tools/*.py
python3 -m unittest discover -s controller -p 'test_*.py'
python3 -m unittest discover -s deploy -p 'test_*.py'
python3 -m unittest discover -s tools -p 'test_*.py'
python3 tools/check_public_tree.py .
```

For engine changes, use `deploy/build_engine.sh` in a disposable workspace and
run the standalone smoke probes. Production verification is read-only unless
an explicit restart or reload has been authorized: inspect both systemd units,
their recent journals, the current map, dashboard freshness, and live players.
