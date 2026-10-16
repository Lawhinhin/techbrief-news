"""Offline collector boundaries: receipt clocks, cache floors and safe diagnostics."""
import json
import sqlite3
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from newsradar.collector import Collector, FeedError, fetch_feed
from newsradar.storage import Storage
from tests.test_collector import SOURCE, CATALOG, feed


class SourceTimingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(":memory:")
        self.config = {"sources": [SOURCE], "tickers": CATALOG, "watchlist": ["AAPL"], "telegram_enabled": False}
        self.collector = Collector(self.storage, self.config)

    def tearDown(self):
        self.storage.close()

    def collect(self, payload, *, source=SOURCE, start="2027-01-01T00:01:00.000000Z",
                receipt="2027-01-01T00:01:02.250000Z", headers=None, validators=None):
        meta = {**(validators or {}), "_response": {"received_at": receipt, "headers": headers or {}}}
        with patch("newsradar.collector._receipt_now", return_value=start), \
             patch("newsradar.storage.utc_now", return_value="2027-01-01T00:01:10Z"), \
             patch("newsradar.collector.fetch_feed", return_value=(payload, source["url"], meta)):
            return self.collector._collect_source(source, self.config, {})

    def test_response_clock_precedes_storage_and_seed_has_no_fresh_metrics(self):
        self.collect(feed(date="Fri, 01 Jan 2027 00:00:30 GMT"))
        summary = self.collector.latency.summary()
        source = summary["sources"][0]
        self.assertEqual(source["last_response_at"], "2027-01-01T00:01:02.250000Z")
        self.assertEqual(source["response_elapsed_seconds"]["median"], 2.25)
        self.assertEqual(source["prospective_receipt_count"], 0)
        self.assertIsNone(source["publication_to_receipt_seconds"]["median"])
        self.assertEqual(source["exclusion_counts"], {"initial_snapshot": 1})

    def test_duplicate_archive_link_keeps_both_source_receipts(self):
        other = {**SOURCE, "id": "second", "name": "Second"}
        self.collector.update_config({**self.config, "sources": [SOURCE, other]})
        empty = b"<rss><channel/></rss>"
        self.collect(empty, start="2027-01-01T00:00:00Z", receipt="2027-01-01T00:00:02Z")
        self.collect(empty, source=other, start="2027-01-01T00:00:00Z", receipt="2027-01-01T00:00:03Z")
        payload = feed(date="Fri, 01 Jan 2027 00:01:00 GMT")
        self.collect(payload)
        self.collect(payload, source=other, start="2027-01-01T00:02:00Z", receipt="2027-01-01T00:02:03Z")
        self.assertEqual(self.storage.total_count(), 1)
        result = self.collector.latency.summary()
        self.assertEqual(result["prospective_receipt_count"], 2)
        comparison = result["same_link_comparisons"][0]
        self.assertEqual(comparison["source_count"], 2)
        self.assertEqual(comparison["receipts"][1]["seconds_after_first_local_receipt"], 60.75)

    def test_cache_lifetime_and_ttl_defer_without_false_observations(self):
        payload = feed().replace(b"<channel>", b"<channel><ttl>20</ttl>")
        self.collect(payload, headers={"Cache-Control": "public, max-age=600"}, validators={"etag": '"v1"'})
        self.assertEqual(self.storage.get_metadata("source_refresh_minutes:example"), 20)
        self.assertEqual(self.storage.get_metadata("source_next_fetch:example"), "2027-01-01T00:21:02Z")
        self.assertEqual(self.storage.get_metadata("source_http:example"), {"etag": '"v1"'})
        with patch("newsradar.collector.fetch_feed") as fetch:
            report = self.collector._collect_source(SOURCE, self.config, {})
        self.assertEqual(report["status"], "deferred")
        fetch.assert_not_called()
        self.assertEqual(self.collector.latency.summary()["fetch_count"], 1)

    def test_304_preserves_feed_ttl_and_counts_receipt_without_items(self):
        self.storage.set_metadata("source_feed_ttl_minutes:example", 15)
        report = self.collect(None, headers={"Cache-Control": "max-age=120"})
        self.assertTrue(report["not_modified"])
        self.assertEqual(self.storage.get_metadata("source_next_fetch:example"), "2027-01-01T00:16:02Z")
        source = self.collector.latency.summary()["sources"][0]
        self.assertEqual(source["not_modified_count"], 1)
        self.assertEqual(source["prospective_receipt_count"], 0)

    def test_diagnostic_failure_does_not_lose_successful_article(self):
        with patch.object(self.collector.latency, "record_fetch", side_effect=sqlite3.OperationalError("offline diagnostic failure")), \
             self.assertLogs("techbrief", level="WARNING"):
            report = self.collect(feed())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(self.storage.total_count(), 1)
        self.assertEqual(self.storage.source_status()[0]["status"], "ok")

    def test_updated_only_time_is_not_original_publication_measurement(self):
        self.collect(b"<rss><channel/></rss>", start="2027-01-01T00:00:00Z", receipt="2027-01-01T00:00:02Z")
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Apple news</title><link href="https://example.com/updated"/><updated>2027-01-01T00:01:00Z</updated></entry></feed>'
        self.collect(atom)
        source = self.collector.latency.summary()["sources"][0]
        self.assertEqual(source["exclusion_counts"], {"updated_timestamp_only": 1})
        self.assertEqual(source["prospective_receipt_count"], 0)

    def test_retry_after_is_a_persistent_floor_and_error_is_redacted(self):
        with patch("newsradar.collector.fetch_feed", side_effect=FeedError("temporary token-secret failure", retry_after_seconds=1800)), \
             patch("newsradar.collector._receipt_now", return_value="2027-01-01T00:00:00.000000Z"):
            report = self.collector._collect_source(SOURCE, self.config, {"telegram_bot_token": "token-secret"})
        self.assertEqual(report["status"], "error")
        self.assertEqual(self.storage.get_metadata("source_next_fetch:example"), "2027-01-01T00:30:00Z")
        source = self.collector.latency.summary()["sources"][0]
        self.assertEqual(source["response_elapsed_seconds"]["sample_count"], 0)
        self.assertNotIn("token-secret", json.dumps(source))

    def test_http_retry_after_does_not_immediately_repeat_request(self):
        error = urllib.error.HTTPError(SOURCE["url"], 429, "throttled", {"Retry-After": "1200"}, None)
        opener = MagicMock()
        opener.open.side_effect = error
        with patch("newsradar.collector.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(FeedError) as result:
                fetch_feed(SOURCE, retries=2)
        self.assertEqual(result.exception.retry_after_seconds, 1200)
        opener.open.assert_called_once()

    def test_cache_age_uses_remaining_freshness(self):
        self.collect(feed(), headers={"Cache-Control": "max-age=600", "Age": "540"})
        self.assertEqual(self.storage.get_metadata("source_refresh_minutes:example"), 1)
        self.assertEqual(self.storage.get_metadata("source_next_fetch:example"), "2027-01-01T00:02:02Z")

    def test_304_recomputes_cache_age_instead_of_reapplying_previous_floor(self):
        self.storage.set_metadata("source_cache_control:example", "max-age=600")
        self.storage.set_metadata("source_refresh_minutes:example", 10)
        self.collect(None, headers={"Age": "540"})
        self.assertEqual(self.storage.get_metadata("source_refresh_minutes:example"), 1)

    def test_http_response_metadata_records_receipt_and_only_permitted_headers(self):
        response = MagicMock()
        response.geturl.return_value = SOURCE["url"]
        response.headers = {"Cache-Control": "max-age=600", "ETag": '"v1"', "Set-Cookie": "private-cookie"}
        response.read.return_value = b"<rss><channel/></rss>"
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("newsradar.collector.urllib.request.build_opener", return_value=opener), \
             patch("newsradar.collector._receipt_now", return_value="2027-01-01T00:00:02.125000Z"):
            payload, final, metadata = fetch_feed(SOURCE)
        self.assertEqual(payload, response.read.return_value)
        self.assertEqual(metadata["etag"], '"v1"')
        self.assertEqual(metadata["_response"]["received_at"], "2027-01-01T00:00:02.125000Z")
        self.assertNotIn("Set-Cookie", metadata["_response"]["headers"])

    def test_http_304_metadata_preserves_validators_and_captures_headers(self):
        error = urllib.error.HTTPError(SOURCE["url"], 304, "unchanged", {"Cache-Control": "max-age=120"}, None)
        opener = MagicMock()
        opener.open.side_effect = error
        with patch("newsradar.collector.urllib.request.build_opener", return_value=opener):
            payload, final, metadata = fetch_feed(SOURCE, validators={"etag": '"old"'})
        self.assertIsNone(payload)
        self.assertEqual(metadata["etag"], '"old"')
        self.assertEqual(metadata["_response"]["headers"]["Cache-Control"], "max-age=120")

    def test_retry_after_longer_than_a_day_is_honored(self):
        error = urllib.error.HTTPError(SOURCE["url"], 503, "busy", {"Retry-After": "172800"}, None)
        opener = MagicMock()
        opener.open.side_effect = error
        with patch("newsradar.collector.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(FeedError) as result:
                fetch_feed(SOURCE)
        self.assertEqual(result.exception.retry_after_seconds, 172800)

    def test_failed_parsing_records_handling_time_and_no_response_distribution(self):
        with patch("newsradar.collector._receipt_now", side_effect=["2027-01-01T00:00:00Z", "2027-01-01T00:00:12Z"]), \
             patch("newsradar.collector.fetch_feed", return_value=(b"bad xml", SOURCE["url"], {
                 "_response": {"received_at": "2027-01-01T00:00:02Z", "headers": {}}})):
            report = self.collector._collect_source(SOURCE, self.config, {})
        self.assertEqual(report["status"], "error")
        source = self.collector.latency.summary()["sources"][0]
        self.assertEqual(source["failed_attempt_elapsed_seconds"]["median"], 12)
        self.assertIsNone(source["response_elapsed_seconds"]["median"])


if __name__ == "__main__":
    unittest.main()
