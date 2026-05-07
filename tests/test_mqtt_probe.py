"""Tests for the MQTTPrinterClient pre-connect probe diagnostics."""

from __future__ import annotations

import socket
import ssl
import unittest
from unittest.mock import MagicMock, patch

from app.services.mqtt_printer import MQTTPrinterClient


def _make_client() -> MQTTPrinterClient:
    return MQTTPrinterClient(
        ip="10.0.1.157",
        access_code="abcdefgh",
        serial="030xxxxxxxxx403",
    )


class TestProbeBrokerReachability(unittest.TestCase):
    @patch("app.services.mqtt_printer.ssl.SSLContext")
    @patch("app.services.mqtt_printer.socket.create_connection")
    def test_returns_none_when_tcp_and_tls_succeed(self, create_conn, ssl_ctx):
        create_conn.return_value = MagicMock()
        ssl_ctx.return_value.wrap_socket.return_value = MagicMock()

        result = _make_client()._probe_broker_reachability()

        self.assertIsNone(result)
        create_conn.assert_called_once()
        args, kwargs = create_conn.call_args
        self.assertEqual(args[0], ("10.0.1.157", 8883))
        self.assertIn("timeout", kwargs)

    @patch("app.services.mqtt_printer.socket.create_connection")
    def test_connection_refused_hints_at_other_client_or_lan_mode(self, create_conn):
        create_conn.side_effect = ConnectionRefusedError(61, "Connection refused")

        msg = _make_client()._probe_broker_reachability()

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertIn("refused", msg.lower())
        self.assertIn("10.0.1.157", msg)
        self.assertIn("MQTT client", msg)
        self.assertIn("LAN", msg)

    @patch("app.services.mqtt_printer.socket.create_connection")
    def test_connection_reset_hints_at_other_client(self, create_conn):
        create_conn.side_effect = ConnectionResetError(54, "Connection reset by peer")

        msg = _make_client()._probe_broker_reachability()

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertIn("reset", msg.lower())
        self.assertIn("MQTT client", msg)

    @patch("app.services.mqtt_printer.socket.create_connection")
    def test_timeout_mentions_ip_and_reachability(self, create_conn):
        create_conn.side_effect = TimeoutError("timed out")

        msg = _make_client()._probe_broker_reachability()

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertIn("imed out", msg)
        self.assertIn("10.0.1.157", msg)

    @patch("app.services.mqtt_printer.socket.create_connection")
    def test_dns_failure_mentions_resolution(self, create_conn):
        create_conn.side_effect = socket.gaierror(-2, "Name or service not known")

        msg = _make_client()._probe_broker_reachability()

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertIn("resolve", msg.lower())

    @patch("app.services.mqtt_printer.ssl.SSLContext")
    @patch("app.services.mqtt_printer.socket.create_connection")
    def test_tls_error_mentions_tls(self, create_conn, ssl_ctx):
        create_conn.return_value = MagicMock()
        ssl_ctx.return_value.wrap_socket.side_effect = ssl.SSLError("handshake failure")

        msg = _make_client()._probe_broker_reachability()

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertIn("TLS", msg)

    @patch("app.services.mqtt_printer.socket.create_connection")
    def test_generic_oserror_mentions_network(self, create_conn):
        create_conn.side_effect = OSError(101, "Network is unreachable")

        msg = _make_client()._probe_broker_reachability()

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertIn("etwork", msg)


class TestConnectShortCircuitsOnProbeFailure(unittest.TestCase):
    def test_connect_does_not_create_paho_client_when_probe_fails(self):
        client = _make_client()
        probe_msg = "Connection refused by 10.0.1.157:8883. ..."
        with patch.object(client, "_probe_broker_reachability", return_value=probe_msg):
            with patch("app.services.mqtt_printer.mqtt.Client") as mqtt_client_cls:
                client.connect()
                mqtt_client_cls.assert_not_called()
        self.assertFalse(client._connected)
        self.assertEqual(client._last_error, probe_msg)
        self.assertIsNone(client._client)

    def test_connect_creates_paho_client_when_probe_succeeds(self):
        client = _make_client()
        with patch.object(client, "_probe_broker_reachability", return_value=None):
            with patch("app.services.mqtt_printer.mqtt.Client") as mqtt_client_cls:
                fake_client = MagicMock()
                mqtt_client_cls.return_value = fake_client
                client.connect()
                mqtt_client_cls.assert_called_once()
                fake_client.connect_async.assert_called_once()
                fake_client.loop_start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
