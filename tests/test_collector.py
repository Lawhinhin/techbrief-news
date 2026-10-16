"""Offline tests for ingestion evidence, safe links, deduplication, and alerts."""
import json
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from newsradar.collector import Collector, FeedError, fetch_feed, match_tickers, normalize_url, parse_feed, parse_timestamp
from newsradar.storage import Storage
from newsradar.telegram import TelegramClient, TelegramError, destination_key, format_article, redact


SOURCE = {"id": "example", "name": "Example", "url": "https://example.com/feed", "allowed_hosts": ["example.com"], "category": "news", "enabled": True}
CATALOG = [{"symbol": "AAPL", "aliases": ["Apple"]}, {"symbol": "ARM", "aliases": ["Arm Holdings"]}, {"symbol": "NVDA", "aliases": ["NVIDIA"]}]


def feed(title="Apple launches a product", url="https://example.com/apple", date=None):
    date = date or datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    return f"<rss><channel><item><title>{title}</title><link>{url}</link><description>New technology.</description><pubDate>{date}</pubDate></item></channel></rss>".encode()


class ParsingTests(unittest.TestCase):
    def test_atom_published_and_alternate_link(self):
        payload = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Apple &amp; NVIDIA</title><link rel="self" href="https://example.com/api"/><link href="/news?utm_source=x&amp;id=4#top"/><summary type="html">&lt;p&gt;One &lt;b&gt;two&lt;/b&gt;&lt;/p&gt;</summary><published>2026-10-01T18:00:00+08:00</published><updated>2026-10-02T00:00:00Z</updated></entry></feed>'''
        articles = parse_feed(payload, SOURCE)
        self.assertEqual(articles[0]["published_at"], "2026-10-01T10:00:00Z")
        self.assertEqual(articles[0]["url"], "https://example.com/news?id=4")
        self.assertEqual(articles[0]["summary"], "One two")

    def test_atom_xhtml_paragraphs_keep_word_boundaries(self):
        payload = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Apple update</title><link href="https://example.com/news"/><summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>One</p><p>two</p><script>hidden</script></div></summary></entry></feed>'''
        self.assertEqual(parse_feed(payload, SOURCE)[0]["summary"], "One two")

    def test_missing_publication_stays_unknown(self):
        payload = b"<rss><channel><item><title>Apple</title><link>https://example.com/a</link></item></channel></rss>"
        self.assertIsNone(parse_feed(payload, SOURCE)[0]["published_at"])
        self.assertIsNone(parse_timestamp("2026-10-02T12:00:00"))

    def test_missing_article_link_does_not_become_feed_url(self):
        self.assertEqual(parse_feed(b"<rss><channel><item><title>Apple</title></item></channel></rss>", SOURCE), [])

    def test_reject_entities_and_html_error_pages(self):
        for payload in (b"<!DOCTYPE x [<!ENTITY x 'bad'>]><rss/>", "<!DOCTYPE x [<!ENTITY x 'bad'>]><rss/>".encode("utf-16"), b"<html><body>blocked</body></html>"):
            with self.assertRaises(FeedError):
                parse_feed(payload, SOURCE)

    def test_article_link_must_match_source_allowlist(self):
        self.assertEqual(parse_feed(feed(url="https://untrusted.example/news"), SOURCE), [])

    def test_unsafe_urls_and_canonical_tracking(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "http://localhost/a", "https://127.0.0.1/a", "http://192.168.1.2/a", "https://user:secret@example.com/a", "https://example.com:123/a", "https://example.com/a\nX: secret"):
            self.assertIsNone(normalize_url(url), url)
        self.assertEqual(normalize_url("HTTPS://EXAMPLE.COM:443/a?utm_campaign=c&b=2&a=1#frag"), "https://example.com/a?a=1&b=2")

    def test_exact_company_and_symbol_mentions(self):
        self.assertEqual(match_tickers("Farm machines", "a strong arm", CATALOG), [])
        self.assertEqual(match_tickers("APPLE partners with Nvidia", "ARM earnings and $AAPL", CATALOG), ["AAPL", "ARM", "NVDA"])

    def test_official_newsroom_association_uses_only_catalog_symbol(self):
        self.assertEqual(match_tickers("GeForce games arrive", "New releases", CATALOG, company_ticker="NVDA"), ["NVDA"])
        self.assertEqual(match_tickers("Apple joins a product launch", "", CATALOG, company_ticker="NVDA"), ["AAPL", "NVDA"])
        self.assertEqual(match_tickers("A new product arrives", "", CATALOG, company_ticker="UNKNOWN"), [])

    def test_allowlist_rejects_before_network(self):
        with patch("urllib.request.build_opener") as opener:
            with self.assertRaises(FeedError):
                fetch_feed({**SOURCE, "url": "https://untrusted.example/feed"})
            opener.assert_not_called()


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "news.db"
        self.storage = Storage(self.path)

    def tearDown(self):
        self.storage.close()
        self.temporary.cleanup()

    def article(self, url="https://example.com/one"):
        return {"source_id": "example", "source_name": "Example", "title": "Apple", "url": url, "tickers": ["AAPL"], "published_at": "2026-10-01T00:00:00Z"}

    def test_dedup_preserves_timestamps_across_restart(self):
        first, is_new = self.storage.add_article(self.article())
        self.assertTrue(is_new)
        second, is_new = self.storage.add_article({**self.article(), "published_at": "2026-10-02T00:00:00Z"})
        self.assertFalse(is_new)
        self.assertEqual(first["first_seen_at"], second["first_seen_at"])
        self.assertEqual(first["published_at"], second["published_at"])
        self.storage.close()
        self.storage = Storage(self.path)
        self.assertEqual(self.storage.total_count(ticker="aapl"), 1)
        self.assertEqual(self.storage.list_articles()[0]["tickers"], ["AAPL"])

    def test_literal_search_and_parameterized_filters(self):
        self.storage.add_article({**self.article(), "title": "Apple 50% sales"})
        self.storage.add_article({**self.article("https://example.com/two"), "title": "Apple normal sales"})
        self.assertEqual(self.storage.total_count(search="50%"), 1)
        self.assertEqual(self.storage.total_count(search="' OR 1=1 --"), 0)
        self.assertEqual(len(self.storage.list_articles(limit=1, offset=1)), 1)

    def test_delivery_unique_and_durable(self):
        article, _ = self.storage.add_article(self.article())
        key = destination_key("12345")
        self.assertTrue(self.storage.enqueue_delivery(article["id"], key))
        self.assertFalse(self.storage.enqueue_delivery(article["id"], key))
        delivery = self.storage.due_deliveries(key)[0]
        self.storage.begin_delivery(delivery["id"])
        self.storage.fail_delivery(delivery["id"], "network error", 60)
        self.assertEqual(self.storage.due_deliveries(key), [])
        self.storage.mark_delivered(delivery["id"], 101)
        self.assertEqual(self.storage.list_delivery_status()[0]["status"], "delivered")
        self.assertEqual(self.storage.summary()["deliveries"], {"delivered": 1})

    def test_delivery_recipient_filter_is_applied_before_limit(self):
        article, _ = self.storage.add_article(self.article())
        first_key, second_key = destination_key("123"), destination_key("456")
        self.storage.enqueue_delivery(article["id"], first_key)
        self.storage.enqueue_delivery(article["id"], second_key)
        older = self.storage.list_delivery_status(limit=1, destination_key=first_key)
        newer = self.storage.list_delivery_status(limit=1, destination_key=second_key)
        self.assertEqual(len(older), 1)
        self.assertEqual(len(newer), 1)
        self.assertLess(older[0]["id"], newer[0]["id"])
        self.assertNotIn("destination_key", older[0])
        self.assertEqual(self.storage.list_delivery_status(destination_key="unknown"), [])

    def test_article_and_outbox_written_together(self):
        key = destination_key("12345")
        first, created = self.storage.add_article(self.article(), delivery_destination_key=key)
        self.assertTrue(created)
        self.assertEqual(self.storage.due_deliveries(key)[0]["article_id"], first["id"])
        self.storage.add_article(self.article(), delivery_destination_key=key)
        self.assertEqual(len(self.storage.due_deliveries(key)), 1)

    def test_source_health_recovers(self):
        self.storage.ensure_sources([SOURCE])
        self.storage.record_source_result(SOURCE, error="temporary")
        self.assertEqual(self.storage.source_status()[0]["consecutive_failures"], 1)
        self.storage.record_source_result(SOURCE, new_count=2, item_count=3)
        row = self.storage.source_status()[0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertEqual(row["total_new_articles"], 2)

    def test_cached_feed_keeps_previous_item_and_filter_counts(self):
        self.storage.ensure_sources([SOURCE])
        self.storage.record_source_result(SOURCE, item_count=3, accepted_count=1, filtered_count=2)
        self.storage.record_source_result(SOURCE, not_modified=True)
        row = self.storage.source_status()[0]
        self.assertEqual(row["last_item_count"], 3)
        self.assertEqual(row["last_accepted_count"], 1)
        self.assertEqual(row["last_filtered_count"], 2)
        self.assertTrue(row["last_not_modified"])

    def test_attempt_budget_and_next_due_survive_restart(self):
        key = destination_key("12345")
        for number in range(6):
            article, _ = self.storage.add_article(self.article(f"https://example.com/{number}"), delivery_destination_key=key)
        for delivery in self.storage.due_deliveries(key, limit=5):
            self.assertTrue(self.storage.begin_delivery(delivery["id"]))
            self.storage.mark_delivered(delivery["id"], 1)
        self.assertEqual(self.storage.delivery_window(key)[0], 0)
        self.storage.close()
        self.storage = Storage(self.path)
        self.assertEqual(self.storage.delivery_window(key)[0], 0)
        self.assertGreater(self.storage.next_delivery_due(key), datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))

    def test_next_retry_due_does_not_select_never_attempted_messages(self):
        key = destination_key("12345")
        self.storage.add_article(self.article(), delivery_destination_key=key)
        self.assertIsNone(self.storage.next_delivery_due(key, retries_only=True))
        delivery = self.storage.due_deliveries(key)[0]
        self.storage.begin_delivery(delivery["id"])
        self.storage.fail_delivery(delivery["id"], "network failure", 120)
        self.assertIsNotNone(self.storage.next_delivery_due(key, retries_only=True))

    def test_old_database_health_columns_migrate_without_article_loss(self):
        self.storage.add_article(self.article())
        self.storage.ensure_sources([SOURCE])
        self.storage.record_source_result(SOURCE, item_count=20)
        with self.storage.lock, self.storage.connection:
            # Simulate the schema created before filtered-feed health existed.
            for column in ("last_accepted_count", "last_filtered_count", "last_not_modified"):
                self.storage.connection.execute(f"ALTER TABLE source_health DROP COLUMN {column}")
        self.storage.close()
        self.storage = Storage(self.path)
        self.storage.ensure_sources([SOURCE])
        self.assertEqual(self.storage.total_count(), 1)
        self.assertIn("last_accepted_count", self.storage.source_status()[0])
        self.storage.record_source_result(SOURCE, not_modified=True)
        self.assertIsNone(self.storage.source_status()[0]["last_accepted_count"])
        self.storage.record_source_result(SOURCE, item_count=20, accepted_count=3, filtered_count=17)
        self.assertEqual(self.storage.source_status()[0]["last_accepted_count"], 3)

    def test_retagging_preserves_evidence_and_does_not_queue_history(self):
        original, _ = self.storage.add_article({**self.article(), "title": "GeForce games arrive", "tickers": []})
        changed = self.storage.retag_articles(CATALOG, [{**SOURCE, "company_ticker": "NVDA"}])
        updated = self.storage.get_article(original["id"])
        self.assertEqual(changed, 1)
        self.assertEqual(updated["tickers"], ["NVDA"])
        for field in ("title", "summary", "url", "published_at", "first_seen_at", "last_seen_at"):
            self.assertEqual(original[field], updated[field])
        self.assertEqual(self.storage.list_delivery_status(), [])
        self.assertEqual(self.storage.retag_articles(CATALOG, [{**SOURCE, "company_ticker": "NVDA"}]), 0)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(":memory:")
        self.config = {"sources": [SOURCE], "tickers": CATALOG, "watchlist": ["AAPL"], "telegram_enabled": True, "request_retries": 0, "telegram_max_per_scan": 5}
        self.secrets = {"telegram_bot_token": "12345:fake_token_never_sent_to_network", "telegram_chat_id": "123"}
        self.collector = Collector(self.storage, self.config, self.secrets)

    def tearDown(self):
        self.storage.close()

    def test_first_scan_seeds_then_new_watchlist_article_alerts(self):
        with patch("newsradar.collector.fetch_feed", return_value=(feed(), SOURCE["url"], {})), patch("newsradar.collector.TelegramClient.send_article") as send:
            report = self.collector.run_once()
            self.assertEqual(report["new_articles"], 1)
            self.assertEqual(report["queued_alerts"], 0)
            send.assert_not_called()
        with patch("newsradar.collector.fetch_feed", return_value=(feed(url="https://example.com/new"), SOURCE["url"], {})), patch("newsradar.collector.TelegramClient.send_article", return_value={"message_id": 42}) as send:
            report = self.collector.run_once()
            self.assertEqual(report["telegram"]["delivered"], 1)
            send.assert_called_once()
        with patch("newsradar.collector.fetch_feed", return_value=(feed(url="https://example.com/new"), SOURCE["url"], {})), patch("newsradar.collector.TelegramClient.send_article") as send:
            self.assertEqual(self.collector.run_once()["new_articles"], 0)
            send.assert_not_called()

    def test_unmatched_regulator_news_excluded(self):
        self.collector.update_config({**self.config, "sources": [{**SOURCE, "requires_ticker": True}]}, self.secrets)
        with patch("newsradar.collector.fetch_feed", return_value=(feed(title="Utility company report"), SOURCE["url"], {})):
            self.collector.run_once()
        self.assertEqual(self.storage.total_count(), 0)
        row = self.storage.source_status()[0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["last_item_count"], 1)
        self.assertEqual(row["last_accepted_count"], 0)
        self.assertEqual(row["last_filtered_count"], 1)

    def test_official_product_story_gets_source_company_association(self):
        self.collector.update_config({**self.config, "sources": [{**SOURCE, "company_ticker": "NVDA"}]})
        with patch("newsradar.collector.fetch_feed", return_value=(feed(title="GeForce games arrive"), SOURCE["url"], {})):
            self.collector.run_once()
        self.assertEqual(self.storage.list_articles()[0]["tickers"], ["NVDA"])

    def test_stale_and_undated_articles_do_not_alert(self):
        self.storage.set_metadata("source_seeded:example", True)
        old = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        with patch("newsradar.collector.fetch_feed", return_value=(feed(date=old), SOURCE["url"], {})), patch("newsradar.collector.TelegramClient.send_article") as send:
            self.assertEqual(self.collector.run_once()["queued_alerts"], 0)
            send.assert_not_called()
        self.assertFalse(self.collector._should_alert({"tickers": ["AAPL"], "published_at": None}, self.config, self.secrets))
        self.assertFalse(self.collector._should_alert({"tickers": ["AAPL"], "published_at": "2026-10-03T12:00:00"}, self.config, self.secrets))

    def test_source_minimum_interval_and_conditional_headers(self):
        self.collector.update_config({**self.config, "sources": [{**SOURCE, "min_interval_minutes": 60}]})
        with patch("newsradar.collector.fetch_feed", return_value=(feed(), SOURCE["url"], {"etag": '"v1"'})) as fetch:
            self.collector.run_once()
            self.assertEqual(self.collector.run_once()["sources_deferred"], 1)
            fetch.assert_called_once()
        self.assertEqual(self.storage.get_metadata("source_http:example"), {"etag": '"v1"'})

    def test_transient_telegram_failure_is_durable_retry(self):
        self.storage.set_metadata("source_seeded:example", True)
        with patch("newsradar.collector.fetch_feed", return_value=(feed(), SOURCE["url"], {})), patch("newsradar.collector.TelegramClient.send_article", side_effect=TelegramError("network request failed")):
            report = self.collector.run_once()
        self.assertEqual(report["telegram"]["retrying"], 1)
        status = self.storage.list_delivery_status()[0]
        self.assertEqual(status["status"], "pending")
        self.assertEqual(status["attempts"], 1)

    def test_error_does_not_expose_token(self):
        token = self.secrets["telegram_bot_token"]
        with patch("newsradar.collector.fetch_feed", side_effect=FeedError("Failed: " + token)):
            report = self.collector.run_once()
        self.assertNotIn(token, json.dumps(report))
        self.assertNotIn(token, json.dumps(self.storage.source_status()))

    def pending_articles(self, count=2):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        key = destination_key(self.secrets["telegram_chat_id"])
        for number in range(1, count + 1):
            self.storage.add_article({"source_id": "example", "source_name": "Example", "title": "Apple news", "url": f"https://example.com/{number}", "tickers": ["AAPL"], "published_at": now}, delivery_destination_key=key)

    def test_disabling_during_send_stops_remaining_alerts(self):
        self.pending_articles()
        def send(article):
            self.collector.update_config({**self.config, "telegram_enabled": False})
            return {"message_id": 1}
        with patch("newsradar.collector.TelegramClient.send_article", side_effect=send) as sender:
            report = self.collector._send_pending(self.config, self.secrets)
        sender.assert_called_once()
        self.assertEqual(report["delivered"], 1)
        self.assertEqual(self.storage.summary()["deliveries"], {"delivered": 1, "pending": 1})

    def test_watchlist_change_during_pacing_prevents_send(self):
        self.pending_articles()
        def change_watchlist(seconds):
            self.collector.update_config({**self.config, "watchlist": ["NVDA"]})
            return False
        with patch("newsradar.collector.TelegramClient.send_article", return_value={"message_id": 1}) as sender, patch.object(self.collector.stop_event, "wait", side_effect=change_watchlist):
            report = self.collector._send_pending(self.config, self.secrets)
        sender.assert_called_once()
        self.assertEqual(report["skipped"], 1)

    def test_invalid_xml_does_not_cache_new_http_validator(self):
        with patch("newsradar.collector.fetch_feed", return_value=(b"not xml", SOURCE["url"], {"etag": '"bad"'})):
            self.assertEqual(self.collector.run_once()["sources_error"], 1)
        self.assertIsNone(self.storage.get_metadata("source_http:example"))

    def test_delivery_worker_sends_without_news_fetch(self):
        self.pending_articles(count=1)
        with patch("newsradar.collector.fetch_feed") as fetch, patch("newsradar.collector.TelegramClient.send_article", return_value={"message_id": 1}) as sender:
            report = self.collector.deliver_pending()
        fetch.assert_not_called()
        sender.assert_called_once()
        self.assertEqual(report["delivered"], 1)
        self.assertIsNone(report["next_due_at"])
        self.assertEqual(self.storage.get_metadata("last_delivery")["delivered"], 1)

    def test_delivery_reports_bind_current_destination_without_raw_chat_id(self):
        self.pending_articles(count=1)
        expected = destination_key(self.secrets["telegram_chat_id"])
        with patch("newsradar.collector.TelegramClient.send_article", return_value={"message_id": 1}):
            report = self.collector.deliver_pending()
        self.assertEqual(report["destination_key"], expected)
        self.assertEqual(self.storage.get_metadata("last_delivery")["destination_key"], expected)
        self.collector.update_config({**self.config, "telegram_enabled": False})
        disabled = self.collector.deliver_pending()
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(disabled["destination_key"], expected)
        self.collector.update_config(self.config, {})
        unconfigured = self.collector.deliver_pending()
        self.assertEqual(unconfigured["status"], "unconfigured")
        self.assertEqual(unconfigured["destination_key"], destination_key(""))

    def test_enable_boundary_injected_after_config_updates_and_collector_restart(self):
        boundary = "2026-10-03T00:00:00+00:00"
        self.storage.set_metadata("telegram_enabled_since", boundary)
        self.assertEqual(self.collector._snapshot()[0]["telegram_enabled_since"], boundary)
        self.collector.update_config({**self.config, "watchlist": ["NVDA"]})
        self.assertEqual(self.collector._snapshot()[0]["telegram_enabled_since"], boundary)
        restarted = Collector(self.storage, self.config, self.secrets)
        self.assertEqual(restarted._snapshot()[0]["telegram_enabled_since"], boundary)

    def test_reconfigured_boundary_skips_old_pending_but_sends_new_article(self):
        self.pending_articles()
        cutoff = datetime.now(timezone.utc)
        before = (cutoff - timedelta(minutes=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        after = (cutoff + timedelta(minutes=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.storage.lock, self.storage.connection:
            self.storage.connection.execute("UPDATE articles SET first_seen_at=? WHERE url=?", (before, "https://example.com/1"))
            self.storage.connection.execute("UPDATE articles SET first_seen_at=? WHERE url=?", (after, "https://example.com/2"))
        self.storage.set_metadata("telegram_enabled_since", cutoff.isoformat(timespec="seconds"))
        self.collector.update_config(self.config, self.secrets)
        with patch("newsradar.collector.TelegramClient.send_article", return_value={"message_id": 1}) as sender:
            report = self.collector.deliver_pending()
        sender.assert_called_once()
        self.assertEqual(sender.call_args[0][0]["url"], "https://example.com/2")
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["delivered"], 1)

    def test_five_attempt_burst_guard_is_shared_across_batches(self):
        self.pending_articles(count=7)
        self.collector.update_config({**self.config, "telegram_max_per_scan": 2})
        with patch("newsradar.collector.TelegramClient.send_article", return_value={"message_id": 1}) as sender, patch.object(self.collector.stop_event, "wait", return_value=False):
            for _ in range(4):
                report = self.collector.deliver_pending()
        self.assertEqual(sender.call_count, 5)
        self.assertEqual(self.storage.summary()["deliveries"], {"delivered": 5, "pending": 2})
        self.assertEqual(report["status"], "waiting")
        self.assertIsNotNone(report["next_due_at"])

    def test_delivery_lock_prevents_parallel_outbox_duplicates(self):
        self.pending_articles(count=1)
        entered, release = threading.Event(), threading.Event()
        errors = []
        def slow_send(article):
            entered.set()
            release.wait(5)
            return {"message_id": 1}
        def worker():
            try:
                self.collector.deliver_pending()
            except Exception as error:
                errors.append(error)
        with patch("newsradar.collector.TelegramClient.send_article", side_effect=slow_send) as sender:
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.collector.deliver_pending()["status"], "busy")
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(errors, [])
        sender.assert_called_once()
        self.assertEqual(self.storage.summary()["deliveries"], {"delivered": 1})

    def test_retry_only_worker_skips_unattempted_alerts(self):
        self.pending_articles()
        key = destination_key(self.secrets["telegram_chat_id"])
        delivery = self.storage.due_deliveries(key)[0]
        self.storage.begin_delivery(delivery["id"])
        self.storage.fail_delivery(delivery["id"], "network failure", 0)
        with patch("newsradar.collector.TelegramClient.send_article", return_value={"message_id": 1}) as sender:
            report = self.collector.deliver_pending(retries_only=True)
        sender.assert_called_once()
        self.assertEqual(report["delivered"], 1)
        self.assertEqual(self.storage.summary()["deliveries"], {"delivered": 1, "pending": 1})

    def test_telegram_retry_after_pauses_whole_recipient_queue(self):
        self.pending_articles()
        with patch("newsradar.collector.TelegramClient.send_article", side_effect=TelegramError("Telegram rate limit reached.", code=429, retry_after=180)) as sender:
            report = self.collector.deliver_pending()
            again = self.collector.deliver_pending()
        sender.assert_called_once()
        self.assertEqual(report["retrying"], 1)
        self.assertEqual(again["status"], "waiting")
        due = datetime.fromisoformat(again["next_due_at"].replace("Z", "+00:00"))
        self.assertGreater((due - datetime.now(timezone.utc)).total_seconds(), 175)

    def test_delayed_outbox_article_expiry_is_skipped(self):
        self.pending_articles(count=1)
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.storage.lock, self.storage.connection:
            self.storage.connection.execute("UPDATE articles SET published_at=?", (old,))
        with patch("newsradar.collector.TelegramClient.send_article") as sender:
            report = self.collector.deliver_pending()
        sender.assert_not_called()
        self.assertEqual(report["skipped"], 1)

    def test_concurrent_scan_returns_busy(self):
        self.collector._scan_lock.acquire()
        try:
            self.assertTrue(self.collector.run_once()["busy"])
        finally:
            self.collector._scan_lock.release()


class TelegramTests(unittest.TestCase):
    def test_credentials_not_in_repr_errors_or_redaction(self):
        token = "123456789:abcdefghijklmnopqrstuvwxyz"
        client = TelegramClient(token, "123")
        self.assertNotIn(token, repr(client))
        self.assertNotIn(token, redact("url /bot" + token + "/sendMessage"))
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = urllib.error.URLError("Failed URL /bot" + token)
            with self.assertRaises(TelegramError) as result:
                client.send_message("test")
            self.assertNotIn(token, str(result.exception))

    def test_get_me_works_without_chat_id(self):
        client = TelegramClient("token", "")
        with patch.object(client, "_call", return_value={"username": "example_bot"}):
            self.assertEqual(client.get_me()["username"], "example_bot")
        with self.assertRaises(TelegramError):
            client.send_message("test")

    def test_format_is_plain_source_link_and_bounded(self):
        message = format_article({"title": "<b>news</b>", "url": "https://example.com/a", "source_name": "Example", "tickers": ["AAPL"]})
        self.assertIn("https://example.com/a", message)
        self.assertIn("Publication time unavailable", message)
        self.assertLessEqual(len(message), 4096)


if __name__ == "__main__":
    unittest.main()
