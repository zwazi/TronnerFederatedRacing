# Installation

The installer targets a fresh Ubuntu 24.04 host. Review it before running it;
it installs packages, builds the patched engine, creates a restricted service
account, installs systemd services, and writes `/opt`, `/etc`, and `/var/lib`.

## 1. Prepare operator configuration

Keep configuration outside the repository:

```sh
install -d -m 0700 /secure/tronner-node
cp config/cluster.example.json /secure/tronner-node/cluster.json
cp config/node.example.json /secure/tronner-node/node.json
```

Replace all example domains and IDs. `public_base_url` must be reachable by old
game clients over HTTP if those clients cannot download HTTPS map resources.
Leave `master_list` false during commissioning.

Render without changing the host:

```sh
python3 deploy/render_node.py \
  --cluster /secure/tronner-node/cluster.json \
  --node /secure/tronner-node/node.json \
  --output /tmp/tronner-rendered \
  --production
```

Inspect every rendered file. The renderer rejects duplicate JSON keys,
malformed identifiers, unsafe key filenames, non-literal federation addresses,
same-direction key reuse, example hostnames in production mode, and inconsistent
leader/follower roles.

## 2. Enroll a federated node

Follow [Adding a region](adding-a-region.md). Place only the approved node's
two directional key files in a local mode-`0700` secrets directory. If Firebase
is explicitly enabled, add `firebase-service-account.json` to that directory.

## 3. Install without starting

```sh
sudo ./deploy/install.sh \
  --cluster /secure/tronner-node/cluster.json \
  --node /secure/tronner-node/node.json \
  --secrets-dir /secure/tronner-node/secrets
```

Installation does not enable the public master-list flag beyond the value in
the reviewed node configuration and does not automatically alter the firewall.

## 4. Firewall and private transport

Keep SSH access verified before changing firewall rules. Open the game and map
ports to players. Permit the federation port only over the private overlay and
only from the expected peer. Example policy, which must be adapted to the
operator's interface and addresses:

```sh
ufw allow OpenSSH
ufw allow 4534/udp
ufw allow 8080/tcp
ufw allow in on wg0 from 10.77.0.1 to 10.77.0.2 port 4540 proto udp
ufw enable
```

Do not copy this example blindly or expose UDP/4540 globally.

## 5. Validate and start

```sh
runuser -u armagetron -- python3 /opt/TronnerRacing/federation_sidecar.py \
  --config /etc/tronner-federation/config.json --check
systemctl start tronner-federation armagetronad tronner-racing
systemctl --no-pager --full status \
  tronner-federation armagetronad tronner-racing
```

Run the bidirectional and synchronized-round smoke tests while the node is
unlisted as described in [Validation](validation.md). Confirm map/resource
downloads from an external client. Only then set
`master_list` to true, rerender, reinstall, and deliberately reload the server
during an approved maintenance window.

## Standalone development install

`node.example.json` is intentionally inert. It can be rendered for local
inspection with `--allow-examples`, but the installer refuses to combine that
flag with `--start`.
