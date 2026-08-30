"""Hot-loadable administration commands for intermittent server tips."""

import re


COLOR_CODE_RE = re.compile(r"0[xX](?:[0-9a-fA-F]{6}|RESETT)")
STATE_KEY = "custom_helpful_messages"


def clean_message(value):
    text = COLOR_CODE_RE.sub("", str(value))
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def load_state(controller):
    raw = controller.store.get_json(STATE_KEY, {})
    raw_tips = raw.get("tips", []) if isinstance(raw, dict) else []
    tips = []
    maximum_id = 0
    if isinstance(raw_tips, list):
        for item in raw_tips:
            if not isinstance(item, dict):
                continue
            try:
                tip_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            message = clean_message(item.get("message", ""))
            if tip_id <= 0 or not message or any(
                existing["id"] == tip_id for existing in tips
            ):
                continue
            tips.append({"id": tip_id, "message": message})
            maximum_id = max(maximum_id, tip_id)
    tips.sort(key=lambda item: item["id"])
    try:
        next_id = max(maximum_id + 1, int(raw.get("next_id", 1)))
    except (AttributeError, TypeError, ValueError):
        next_id = maximum_id + 1
    return {"version": 1, "next_id": next_id, "tips": tips}


async def tip_command(controller, player, _access_level, arguments):
    action, separator, remainder = arguments.strip().partition(" ")
    action = action.casefold()
    remainder = remainder.strip() if separator else ""
    state = load_state(controller)

    if action == "add":
        message = clean_message(remainder)
        maximum_characters = max(
            1, int(controller.config.get("custom_tip_maximum_characters", 500))
        )
        maximum_tips = max(
            1, int(controller.config.get("custom_tip_maximum", 100))
        )
        if not message:
            await controller.private(player, "Usage: /tip add [message]")
            return
        if len(message) > maximum_characters:
            await controller.private(
                player,
                f"A tip may be at most {maximum_characters} characters.",
            )
            return
        if len(state["tips"]) >= maximum_tips:
            await controller.private(
                player,
                f"The custom tip limit of {maximum_tips} has been reached.",
            )
            return
        if any(
            item["message"].casefold() == message.casefold()
            for item in state["tips"]
        ):
            await controller.private(player, "That custom tip already exists.")
            return
        tip_id = state["next_id"]
        state["next_id"] = tip_id + 1
        state["tips"].append({"id": tip_id, "message": message})
        controller.store.set_json(STATE_KEY, state)
        await controller.private(player, f"Tip #{tip_id} added: {message}")
        return

    if action == "list" and not remainder:
        if not state["tips"]:
            await controller.private(player, "No custom tips have been added.")
            return
        await controller.private_block(
            player,
            [
                "Custom intermittent tips:",
                *(f'#{item["id"]} - {item["message"]}' for item in state["tips"]),
            ],
        )
        return

    if action == "remove":
        try:
            tip_id = int(remainder)
        except ValueError:
            tip_id = 0
        match = next(
            (item for item in state["tips"] if item["id"] == tip_id), None
        )
        if match is None:
            await controller.private(
                player,
                "No custom tip has that number. Use /tip list to see valid numbers.",
            )
            return
        state["tips"] = [
            item for item in state["tips"] if item["id"] != tip_id
        ]
        controller.store.set_json(STATE_KEY, state)
        await controller.private(
            player, f"Tip #{tip_id} removed: {match['message']}"
        )
        return

    await controller.private(
        player,
        "Usage: /tip add [message], /tip list, or /tip remove [#]",
    )


COMMANDS = {
    "/tip": {
        "handler": tip_command,
        "access_setting": "records_admin_access_level",
        "access_denied": "Only an Owner or Admin may manage tips.",
        "help_command": "/tip add|list|remove",
        "help_description": "Add, list, or remove intermittent server tips.",
    }
}
