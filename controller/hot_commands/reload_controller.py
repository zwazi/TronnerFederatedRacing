"""Hot-loadable command for a graceful racing server-script reload."""


async def reload_controller(controller, player, _access_level, arguments):
    if arguments.strip():
        await controller.private(player, "Usage: /reload_script")
        return
    if not hasattr(controller, "request_controller_reload"):
        await controller.private(
            player,
            "Graceful server script reload is not active yet; install and restart "
            "the pending server script update first.",
        )
        return
    if not controller.request_controller_reload(player.record_name):
        await controller.private(player, "A server script reload is already pending.")


COMMANDS = {
    "/reload_script": {
        "handler": reload_controller,
        "access_setting": "records_admin_access_level",
        "access_denied": "Only an Owner or Admin may reload the server script.",
        "help_command": "/reload_script",
        "help_description": (
            "Pause respawns, drain active runs, and reload only the server script."
        ),
    }
}
