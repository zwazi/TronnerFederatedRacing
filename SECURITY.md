# Security policy

## Supported versions

Only the latest tagged release is supported. Operators should pin a release
and verify its published SHA-256 checksums instead of installing a moving
branch on a production server.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could expose a federation,
player data, Firebase resources, or server control. Use GitHub's private
security-advisory reporting for this repository. Include the affected version,
impact, reproduction steps, and whether any credential may have been exposed.

## Credentials

No credential, production inventory, database, log, replay, or player record
belongs in this repository. If one is committed, removing the latest file is
not sufficient: revoke or rotate it immediately, keep the repository private,
and purge the affected history before publication.

Federation members must use unique directional keys. A node must be removable
without rotating unrelated node credentials. Production Firebase credentials
must use a dedicated, least-privilege service identity and must never be used
by a public CI job or forked pull request.
