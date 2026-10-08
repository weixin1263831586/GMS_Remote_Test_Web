"""Report downloads must connect to the addresses validated before the request."""

import json
import socket
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.auth import CurrentUser
from features.reports import source_api
from foundation.outbound import PinnedOutboundResolver, ResolvedOutboundTarget, UnsafeOutboundURL


class ReportDownloadDnsTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolver_accepts_idna_hostname_and_rejects_changed_target(self):
        target = ResolvedOutboundTarget(
            url="https://例子.example/report.zip", hostname="例子.example",
            port=443, addresses=("93.184.216.34",),
        )
        resolver = PinnedOutboundResolver(target)
        addresses = await resolver.resolve("例子.example".encode("idna").decode("ascii"), 443)
        self.assertEqual(addresses[0]["host"], "93.184.216.34")
        with self.assertRaises(UnsafeOutboundURL):
            await resolver.resolve("other.example", 443)
        with self.assertRaises(UnsafeOutboundURL):
            await resolver.resolve(target.hostname, 8443)

    async def test_rebinding_cannot_change_connection_host_or_tls_name(self):
        class Request:
            state = SimpleNamespace(current_user=CurrentUser(
                id="owner", username="owner", role="user",
            ))
            headers = {}
            cookies = {}

            async def json(self):
                return {"url": "https://download.example/report.zip"}

        config = SimpleNamespace(get_redmine_config=lambda: {})
        attempted = []

        async def stop_before_connect(connector, *args, **kwargs):
            attempted.append(kwargs)
            raise RuntimeError("test transport stopped before opening a socket")

        for rebound_ip in ("127.0.0.1", "10.0.0.1"):
            with self.subTest(rebound_ip=rebound_ip), patch(
                "foundation.outbound.socket.getaddrinfo", side_effect=[
                    [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
                    [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (rebound_ip, 443))],
                ],
            ) as dns, patch.object(
                source_api, "_redmine_config_manager_for_request", return_value=config,
            ), patch.object(source_api.aiohttp.TCPConnector, "_wrap_create_connection", stop_before_connect):
                response = await source_api.analyze_report_from_url(Request())
                self.assertFalse(json.loads(response.body)["success"])
                self.assertEqual(dns.call_count, 1)
                connection = attempted[-1]
                self.assertEqual([item[4][0] for item in connection["addr_infos"]], ["93.184.216.34"])
                self.assertEqual(connection["req"].headers["Host"], "download.example")
                self.assertEqual(connection["server_hostname"], "download.example")
                self.assertTrue(connection["ssl"].check_hostname)
                self.assertEqual(connection["ssl"].verify_mode, ssl.CERT_REQUIRED)
