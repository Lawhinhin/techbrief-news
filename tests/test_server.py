"""Offline local-HTTP checks using a fresh temporary news archive per test."""
import csv
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
from newsradar.collector import parse_feed
from newsradar.telegram import TelegramError


TOKEN = "123456789:" + "a" * 35
CHAT = "1234"


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.directory = tempfile.TemporaryDirectory(prefix="techbrief-news-test-")
        self.application = app.Application(Path(self.directory.name) / "data")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.server.daemon_threads = True
        self.server.application = self.application
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.application.close()
        self.directory.cleanup()
        self.environment.stop()

    def request(self, method, path, body=None, headers=None):
        request_headers = {"Host": f"127.0.0.1:{self.port}"}
        if method == "POST":
            request_headers.update({"Content-Type": "application/json", "X-CSRF-Token": self.application.csrf_token,
                                    "Origin": f"http://127.0.0.1:{self.port}"})
            body = json.dumps(body if body is not None else {}).encode() if not isinstance(body, (bytes, str)) else body
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def configure(self, client, *, text=None, chat_type="private", chat_id=CHAT, **body):
        client.get_me.return_value = {"id": 1, "is_bot": True, "username": "synthetic_bot"}
        client.get_webhook_info.return_value = {"url": ""}
        client.get_updates.return_value = [{"message": {"text": text or "/start " + self.application.pairing_code,
                                                         "chat": {"id": chat_id, "type": chat_type}}}]
        return self.request("POST", "/api/telegram/configure", {"token": TOKEN, **body})

    def article(self, title="A synthetic company headline", url="https://example.com/story"):
        return {"source_id": "synthetic", "source_name": "Example", "title": title, "url": url,
                "summary": "Synthetic fixture only.", "tickers": ["AAPL"], "published_at": "2026-01-01T00:00:00Z"}

    def test_clean_start_has_no_workers_prices_or_research_services(self):
        self.assertEqual(self.application.workers, [])
        self.assertEqual(self.application.storage.total_count(), 0)
        self.assertFalse(self.application.settings.snapshot()[0]["telegram_enabled"])
        self.assertFalse(hasattr(self.application, "market"))
        self.assertFalse(hasattr(self.application, "forward"))

    def test_status_bootstraps_local_csrf_without_returning_credentials(self):
        self.application.settings.save_telegram(TOKEN, CHAT)
        for path in ("/api/status", "/api/settings", "/api/deliveries"):
            with self.subTest(path=path):
                code, headers, raw = self.request("GET", path)
                self.assertEqual(code, 200)
                self.assertNotIn(TOKEN.encode(), raw)
                document = json.loads(raw)
                self.assertNotIn("telegram_bot_token", document)
                self.assertNotIn("telegram_chat_id", document)
                self.assertNotIn("chat_id", document.get("telegram", {}))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(json.loads(self.request("GET", "/api/status")[2])["csrf_token"], self.application.csrf_token)

    def test_status_source_categories_are_available_before_first_collection(self):
        code, _, raw = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(raw)["sources"])
        self.assertTrue(all("category" in item for item in json.loads(raw)["sources"]))

    def test_host_allowlist_blocks_dns_rebinding_and_unexpected_ports(self):
        for host in ("attacker.example", "127.0.0.1", "localhost:1", "127.0.0.1.attacker.example:" + str(self.port)):
            with self.subTest(host=host):
                code, _, raw = self.request("GET", "/api/status", headers={"Host": host})
                self.assertEqual(code, 403)
                self.assertNotIn(self.application.csrf_token.encode(), raw)
        self.assertEqual(self.request("GET", "/api/status", headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_mutations_require_matching_origin_host_and_csrf(self):
        for headers in ({"X-CSRF-Token": ""}, {"X-CSRF-Token": "incorrect"}, {"Origin": "https://attacker.example"},
                        {"Origin": "null"}, {"Host": "attacker.example"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/settings", {"interval_minutes": 30}, headers)[0], 403)
        self.assertEqual(self.application.settings.snapshot()[0]["interval_minutes"], 5)
        self.assertEqual(self.request("POST", "/api/settings", {"interval_minutes": 30})[0], 200)
        self.assertEqual(self.application.settings.snapshot()[0]["interval_minutes"], 30)

    def test_content_length_json_and_request_size_are_bounded(self):
        cases = [(b"", {"Content-Length": "-1"}), (b"x" * 16385, {}),
                 (b"", {"Content-Length": "invalid"}), (b"{}", {"Content-Type": "text/plain"}),
                 (b"{}", {"Transfer-Encoding": "chunked"}), (b"[1]", {}), (b"null", {}), (b"{broken", {}),
                 (b"", {"Content-Length": "0"})]
        for body, headers in cases:
            with self.subTest(body=body, headers=headers):
                self.assertEqual(self.request("POST", "/api/settings", body, headers)[0], 400)

    def test_unknown_and_removed_feature_routes_are_unavailable(self):
        for path in ("/missing", "/api/market/status", "/api/research/summary", "/api/forward", "/data/credentials.json", "/../app.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/api/market/sync", {})[0], 404)

    def test_invalid_query_is_generic_and_never_echoed(self):
        code, _, raw = self.request("GET", "/api/articles?hours=not-a-number")
        self.assertEqual(code, 400)
        self.assertNotIn(b"not-a-number", raw)

    def test_articles_filter_and_paginate_from_temporary_archive(self):
        for index in range(3):
            self.application.storage.add_article(self.article(title=f"Synthetic {index}", url=f"https://example.com/{index}"))
        code, _, raw = self.request("GET", "/api/articles?limit=2&offset=1&ticker=AAPL&search=Synthetic")
        document = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertEqual((document["total"], len(document["articles"]), document["offset"]), (3, 2, 1))

    def test_ingestion_rejects_unsafe_publisher_links_before_storage(self):
        source = {"id": "example", "name": "Example", "url": "https://example.com/feed", "allowed_hosts": ["example.com"]}
        for url in ("javascript:alert(1)", "http://127.0.0.1/private", "https://other.example/story"):
            payload = f"<rss><channel><item><title>Example</title><link>{url}</link></item></channel></rss>".encode()
            self.assertEqual(parse_feed(payload, source), [])
        self.assertEqual(self.application.storage.total_count(), 0)

    def test_csv_neutralizes_formulas_in_publisher_controlled_cells(self):
        titles = ["=HYPERLINK(\"https://example.com\")", "+SUM(1,2)", "-2+3", "@SUM(1)", "\t =1+1", "Ordinary title"]
        for index, title in enumerate(titles):
            self.application.storage.add_article(self.article(title=title, url=f"https://example.com/{index}"))
        code, headers, raw = self.request("GET", "/api/export.csv")
        self.assertEqual(code, 200)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("attachment", headers["Content-Disposition"])
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual({row["title"] for row in rows}, {"'" + title if title != "Ordinary title" else title for title in titles})

    def test_http_security_headers_prevent_embedding_and_external_content(self):
        _, headers, _ = self.request("GET", "/api/status")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])

    def test_request_logs_omit_query_strings_bodies_and_credentials(self):
        with self.assertLogs("techbrief", level="DEBUG") as logs:
            self.request("GET", "/missing?token=" + TOKEN)
            self.request("POST", "/api/settings", {"token": TOKEN})
        text = "\n".join(logs.output)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("?token", text)

    def test_telegram_test_is_refused_before_configuration(self):
        with patch("newsradar.telegram.TelegramClient._call") as network:
            self.assertEqual(self.request("POST", "/api/telegram/test", {})[0], 400)
            network.assert_not_called()

    def test_private_pairing_verifies_bot_and_rotates_code_without_sending(self):
        previous = self.application.pairing_code
        with patch("app.TelegramClient") as factory:
            client = factory.return_value
            code, _, raw = self.configure(client)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw), {"configured": True})
            client.get_me.assert_called_once()
            client.send_message.assert_not_called()
            self.assertNotEqual(self.application.pairing_code, previous)
            self.assertEqual(self.application.settings.snapshot()[1]["telegram_chat_id"], CHAT)
            code, _, _ = self.configure(client, text="/start " + previous)
            self.assertEqual(code, 400)

    def test_pairing_ignores_group_chats_and_wrong_commands(self):
        for options in ({"chat_type": "group"}, {"text": "/start incorrect"}):
            with self.subTest(options=options), patch("app.TelegramClient") as factory:
                self.assertEqual(self.configure(factory.return_value, **options)[0], 400)
                self.assertEqual(self.application.settings.snapshot()[1], {})

    def test_automatic_pairing_refuses_existing_webhook(self):
        with patch("app.TelegramClient") as factory:
            client = factory.return_value
            client.get_webhook_info.return_value = {"url": "https://example.com/webhook"}
            client.get_me.return_value = {"is_bot": True}
            self.assertEqual(self.request("POST", "/api/telegram/configure", {"token": TOKEN})[0], 400)
            client.get_updates.assert_not_called()
            self.assertEqual(self.application.settings.snapshot()[1], {})

    def test_automatic_pairing_refuses_multiple_matching_private_chats(self):
        with patch("app.TelegramClient") as factory:
            client = factory.return_value
            client.get_me.return_value = {"is_bot": True}
            client.get_webhook_info.return_value = {"url": ""}
            client.get_updates.return_value = [{"message": {"text": "/start " + self.application.pairing_code,
                                                           "chat": {"id": identifier, "type": "private"}}} for identifier in (1, 2)]
            self.assertEqual(self.request("POST", "/api/telegram/configure", {"token": TOKEN})[0], 400)
            self.assertEqual(self.application.settings.snapshot()[1], {})

    def test_bot_validation_failure_does_not_save_credentials(self):
        with patch("app.TelegramClient") as factory:
            factory.return_value.get_me.side_effect = TelegramError("Telegram rejected the bot token.", transient=False)
            self.assertEqual(self.request("POST", "/api/telegram/configure", {"token": TOKEN, "chat_id": CHAT})[0], 400)
            self.assertEqual(self.application.settings.snapshot()[1], {})

    def test_invalid_token_rejected_before_bot_api_calls(self):
        with patch("app.TelegramClient") as factory:
            self.assertEqual(self.request("POST", "/api/telegram/configure", {"token": "invalid"})[0], 400)
            factory.assert_not_called()

    def test_explicit_chat_configuration_and_test_have_separate_effects(self):
        with patch("app.TelegramClient") as factory:
            client = factory.return_value
            client.get_me.return_value = {"is_bot": True}
            self.assertEqual(self.request("POST", "/api/telegram/configure", {"token": TOKEN, "chat_id": CHAT, "enabled": False})[0], 200)
            client.get_updates.assert_not_called()
            client.send_message.assert_not_called()
            self.assertFalse(self.application.settings.snapshot()[0]["telegram_enabled"])
            self.assertEqual(self.request("POST", "/api/telegram/test", {})[0], 200)
            client.send_message.assert_called_once()
            self.assertEqual(self.application.status()["telegram"]["last_test_status"], "success")

    def test_scan_route_only_wakes_collector_without_running_network_in_handler(self):
        with patch.object(self.application.collector, "run_once") as collect:
            self.assertEqual(self.request("POST", "/api/scan", {})[0], 202)
            self.assertTrue(self.application.wake.is_set())
            collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
