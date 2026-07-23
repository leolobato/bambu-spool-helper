from __future__ import annotations

import unittest

from app.services.print_tracking.gateway import _reconnect_delay


class GatewayPrintClientTests(unittest.TestCase):
    def test_reconnect_backoff_reaches_documented_sixty_second_cap(self):
        self.assertEqual(
            [_reconnect_delay(attempt) for attempt in range(8)],
            [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0],
        )
