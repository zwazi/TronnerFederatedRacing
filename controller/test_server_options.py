import collections
import unittest
from types import SimpleNamespace

from TronnerRacing import TronnerRacing


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class ServerOptionsTests(unittest.IsolatedAsyncioTestCase):
    def controller(self):
        current = SimpleNamespace(
            key="current",
            name="Current_Map",
            author="Curator",
        )
        next_entry = SimpleNamespace(
            key="next",
            name="Next_Map",
            author="Navigator",
        )
        queued = SimpleNamespace(
            key="queued",
            name="Queued_Map",
            author="Queuer",
        )
        controller = object.__new__(TronnerRacing)
        controller.current = current
        controller.queue = collections.deque()
        controller.rotation = collections.deque([next_entry.key])
        controller.cycle_played = set()
        controller.repository = SimpleNamespace(
            catalog={
                current.key: current,
                next_entry.key: next_entry,
                queued.key: queued,
            },
            display_name=lambda entry: entry.name.replace("_", " "),
        )
        controller._server_options_last = None
        controller.sink = Sink()
        return controller

    def test_options_name_current_and_rotated_next_map(self):
        controller = self.controller()

        self.assertEqual(
            controller._server_options_text(),
            "Current map: Current Map by Curator | "
            "Next Map: Next Map by Navigator",
        )

    async def test_refresh_only_writes_changes_and_queue_takes_precedence(self):
        controller = self.controller()

        await controller._refresh_server_options_once()
        await controller._refresh_server_options_once()
        controller.queue.append("queued")
        await controller._refresh_server_options_once()

        self.assertEqual(
            controller.sink.commands,
            [
                "SERVER_OPTIONS Current map: Current Map by Curator | "
                "Next Map: Next Map by Navigator",
                "SERVER_OPTIONS Current map: Current Map by Curator | "
                "Next Map: Queued Map by Queuer",
            ],
        )


if __name__ == "__main__":
    unittest.main()
