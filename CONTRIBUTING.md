# Contributing

Contributions must not include production addresses, credentials, runtime
state, player information, logs, databases, recordings, or generated binaries.
Use the reserved documentation ranges and `example.invalid` in tests and
examples.

Before opening a pull request, run:

```sh
python3 -m unittest discover -s controller -p 'test_*.py'
python3 -m unittest discover -s deploy -p 'test_*.py'
python3 -m unittest discover -s tools -p 'test_*.py'
python3 tools/check_public_tree.py .
```

Changes to the wire protocol, authentication, replay handling, federation
roles, map authority, or round synchronization require focused tests and a
protocol compatibility note. Production deployments are deliberately absent
from this repository and cannot be triggered by a pull request.
