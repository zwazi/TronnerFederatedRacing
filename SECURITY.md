# Security policy

Only the latest tagged release is supported. Pin releases and verify their
published SHA-256 checksums.

Report vulnerabilities with GitHub private security advisories. Never put a
credential, production inventory, database, log, replay, or player record in
an issue or this repository. If a credential enters Git history, revoke or
rotate it immediately; deleting the latest copy is not sufficient.

Production Firebase access uses a dedicated least-privilege service identity.
It must never be exposed to public CI or forked pull requests. Server admin
commands remain authenticated, bounded, audited, and inaccessible to regular
clients.
