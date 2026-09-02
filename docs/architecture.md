# Architecture

Tronner Racing has one production game server in New York.

```text
Armagetron dedicated server
  ↕ ladderlog + console input
Python racing controller
  ↕ Firebase catalog, live status, replays, and admin command queue
tronner.io / Vectron
```

The dedicated server owns cycle simulation and networking. The controller owns
map rotation, repeated attempts, checkpoints, records, ratings, AFK decisions,
countdowns, replay capture, and bounded dashboard publishing. Firebase is the
catalog and website bridge, not a second race authority.

Server and controller run as the unprivileged `armagetron` account under
systemd. Runtime data lives under `/var/lib`; credentials live under `/etc`
with root ownership and group-readable permissions only where required.
