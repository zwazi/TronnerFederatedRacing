# Upgrades and rollback

Run the full tests and render the intended production configuration before an
upgrade. Back up the controller database, map overrides, and configuration;
never copy credentials into Git.

Deploy controller-only changes with the graceful controller reload path so
active runs finish. Engine or game-configuration changes require the explicit
server restart countdown. Verify the game process, controller process, live
dashboard age, map, player snapshot, and one completed run afterward.

Rollback by reinstalling the previously pinned release and restoring only a
schema-compatible database backup. Keep immutable map/replay resources intact.
