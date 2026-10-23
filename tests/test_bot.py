import socket
import unittest
from unittest.mock import AsyncMock, patch

from xiap.bot import wait_for_network


class WaitForNetworkTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_at_once_when_the_name_resolves(self):
        with patch("asyncio.sleep", AsyncMock()) as sleep, \
                patch("asyncio.BaseEventLoop.getaddrinfo", AsyncMock(return_value=[()])):
            await wait_for_network()
        sleep.assert_not_awaited()

    async def test_waits_with_growing_delays_until_the_name_resolves(self):
        lookups = AsyncMock(side_effect=[socket.gaierror(-3, "Temporary failure")] * 3 + [[()]])
        with patch("asyncio.sleep", AsyncMock()) as sleep, patch("asyncio.BaseEventLoop.getaddrinfo", lookups), \
                self.assertLogs("xiap.bot") as logs:
            await wait_for_network(max_delay=5)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [2.0, 4.0, 5])
        self.assertEqual(len(logs.records), 2)  # one line when it starts waiting, one when it's back


if __name__ == "__main__":
    unittest.main()
