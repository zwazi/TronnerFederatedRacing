"""Hot-loadable command for a graceful racing-controller reload."""


async def reload_controller(controller, player, _access_level, arguments):
    if arguments.strip():
        await controller.private(player, "Usage: /reload_controller")
        return
    if not hasattr(controller, "request_controller_reload"):
        await controller.private(
            player,
            "Graceful controller reload is not active yet; install and restart "
            "the pending controller update first.",
        )
        return
    if not controller.request_controller_reload(player.record_name):
        await controller.private(player, "A controller reload is already pending.")


COMMANDS = {
    "/reload_controller": {
        "handler": reload_controller,
        "access_setting": "records_admin_access_level",
        "access_denied": "Only an Owner or Admin may reload the controller.",
        "help_command": "/reload_controller",
        "help_description": (
            "Pause respawns, drain active runs, and reload only the controller."
        ),
    }
}
