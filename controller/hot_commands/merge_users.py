"""Hot-loadable administration command for saved race identities."""

import shlex


async def merge_users(controller, player, _access_level, arguments):
    try:
        user_arguments = shlex.split(arguments)
    except ValueError:
        user_arguments = []
    if len(user_arguments) != 2:
        await controller.private(
            player,
            "Usage: /merge_users [old user] [new user] (quote names with spaces)",
        )
        return
    old_query, new_query = user_arguments

    old_player = controller.player_for(old_query)
    source_query = old_player.identity_key if old_player else old_query
    source_matches = controller.store.matching_user_identities(source_query)
    if not source_matches:
        await controller.private(
            player, f"No saved times were found for {old_query}."
        )
        return
    if len(source_matches) > 1:
        choices = ", ".join(match.identity_key for match in source_matches)
        await controller.private(
            player,
            f"Old user is ambiguous ({choices}); "
            "use the full auth: or guest: identity.",
        )
        return
    source = source_matches[0]

    new_player = controller.player_for(new_query)
    if new_player:
        destination = controller.store.identity_for_player(new_player)
    else:
        destination_matches = controller.store.matching_user_identities(new_query)
        if len(destination_matches) > 1:
            choices = ", ".join(
                match.identity_key for match in destination_matches
            )
            await controller.private(
                player,
                f"New user is ambiguous ({choices}); "
                "use the full auth: or guest: identity.",
            )
            return
        if destination_matches:
            destination = destination_matches[0]
        else:
            destination = controller.store.explicit_user_identity(new_query)
            if destination is None:
                await controller.private(
                    player,
                    f"New user {new_query} has no saved times and is not online. "
                    "Use auth:[name] or guest:[name] to create that identity "
                    "explicitly.",
                )
                return

    if source.identity_key.casefold() == destination.identity_key.casefold():
        await controller.private(
            player, "The old and new users resolve to the same identity."
        )
        return
    result = controller.store.merge_users(source.identity_key, destination)
    await controller.broadcast(
        f"{player.record_name} merged {source.username} into "
        f"{destination.username}: {result.records_moved} records and "
        f"{result.finishes_moved} finish entries and "
        f"{result.replay_runs_moved} replay runs moved; "
        f"{result.overlapping_records} overlapping records kept the better result."
    )


COMMANDS = {
    "/merge_users": {
        "handler": merge_users,
        "access_setting": "records_admin_access_level",
        "access_denied": "Only an Owner or Admin may merge users.",
        "help_command": "/merge_users [old] [new]",
        "help_description": (
            "Move all saved times to a user and delete the old time identity."
        ),
    }
}
