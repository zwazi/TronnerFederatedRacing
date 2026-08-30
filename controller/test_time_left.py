import unittest

from TronnerRacing import TronnerRacing, plain_console_text


class Sink:
    def __init__(self):
        self.commands = []

    async def send(self, *commands):
        self.commands.extend(commands)


class TimeLeftTests(unittest.IsolatedAsyncioTestCase):
    async def test_announces_each_even_minute_once(self):
        controller = object.__new__(TronnerRacing)
        controller.sink = Sink()
        controller.round_active = True
        controller.transitioning = False
        controller.final_countdown_active = False
        controller.deadline_epoch = 1000.0
        controller.last_time_left_minute = 5

        await controller._announce_time_left(760.01)
        await controller._announce_time_left(760.50)
        await controller._announce_time_left(880.01)
        await controller._announce_time_left(880.50)

        self.assertEqual(
            [plain_console_text(command) for command in controller.sink.commands],
            [
                "CONSOLE_MESSAGE Time left: 4 minutes.",
                "CONSOLE_MESSAGE Time left: 2 minutes.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
