# Contributing

Tronner Racing is a standalone New York service. Changes must preserve that
single-server model unless the product owner explicitly requests a new
topology.

Do not commit production addresses, credentials, runtime state, player data,
logs, databases, recordings, or generated binaries. Use reserved documentation
ranges and `example.invalid` in examples.

Before opening a pull request, run:

```sh
python3 -m unittest discover -s controller -p 'test_*.py'
python3 -m unittest discover -s deploy -p 'test_*.py'
python3 -m unittest discover -s tools -p 'test_*.py'
python3 tools/check_public_tree.py .
```

Production deployments are absent from CI and cannot be triggered by a pull
request.
