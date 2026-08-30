# Upgrades and rollback

Pin production nodes to a tagged release. Before an upgrade, save checksums of
the installed engine and five controller modules, back up runtime state through
the private operations system, and run the release tests without contacting
production services.

Upgrade the follower first while it remains unlisted, verify protocol
compatibility, then upgrade the leader in an approved maintenance window. An
engine replacement disconnects players; a controller/sidecar change may still
affect live synchronization and must not be treated as a harmless file copy.

Keep the prior engine binary and controller modules in a root-only timestamped
directory. Rollback restores the complete matching set, not an individual
module from another release. Verify hashes, configuration checks, service
health, federation heartbeat, and one synchronized natural map transition
before declaring the rollout complete.
