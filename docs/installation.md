# Standalone installation

The installer supports Ubuntu and creates one game process plus one racing
controller. Render examples first; production rendering rejects example
identities and hostnames.

```sh
python3 deploy/render_node.py \
  --cluster /secure/operator/service.json \
  --node /secure/operator/server.json \
  --output /tmp/tronner-rendered \
  --production

sudo deploy/install.sh \
  --cluster /secure/operator/service.json \
  --node /secure/operator/server.json \
  --secrets-dir /secure/operator/secrets
```

Add `--start` only after inspecting the rendered files, firewall, service
users, secret permissions, and public master-list setting. The installer never
ships production inventory or credentials.

Installed units are `armagetronad.service` and `tronner-racing.service`.
Configuration is under `/etc/tronner-racing` and the Armagetron config
directory; mutable state is under `/var/lib/armagetronad` and
`/var/lib/tronner-racing`.
