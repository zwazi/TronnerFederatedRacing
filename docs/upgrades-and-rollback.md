# Upgrades and rollback

Run the full tests and render the intended production configuration before an
upgrade. Back up the server-script database, map overrides, and configuration;
never copy credentials into Git.

Deploy server-script-only changes with the graceful server script reload path so
active runs finish. Engine or game-configuration changes require the explicit
server restart countdown. Verify the game process, server-script process, live
dashboard age, map, player snapshot, and one completed run afterward.

A graceful server script reload starts from the validated local Firebase
snapshot and parsed catalog cache. Firebase version reconciliation happens in
the background after the script is ready; use the explicit map reload operation
when an immediate catalog refresh is the intended change.

Rollback by reinstalling the previously pinned release and restoring only a
schema-compatible database backup. Keep immutable map/replay resources intact.
