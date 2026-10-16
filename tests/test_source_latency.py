"""Offline checks for receipt evidence, seeding exclusion and honest empty metrics."""
import json
import sqlite3
import unittest

from newsradar.source_latency import MAX_ARTICLES, SourceLatency
from newsradar.storage import Storage

ACTIVATION = "2026-10-05T01:00:00Z"
SOURCE = {"id": "google", "name": "Official Google Blog", "category": "newsroom",
          "url": "https://blog.google/rss", "min_interval_minutes": 5}


class SourceLatencyTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(":memory:")
        self.timing = SourceLatency(self.storage, activated_at=ACTIVATION)

    def tearDown(self):
        self.storage.close()

    def article(self, name="one", *, published="2026-10-05T01:05:00Z", first_seen="2026-10-05T01:06:00Z", is_new=True):
        return {"id": name, "url": "https://blog.google/" + name, "published_at": published,
                "first_seen_at": first_seen, "is_new": is_new}

    def record(self, *, source=SOURCE, start="2026-10-05T01:06:00Z", end="2026-10-05T01:06:02Z",
               articles=None, status="ok", not_modified=False, seeded=False, headers=None, duration=2200):
        return self.timing.record_fetch(source, {"status": status, "not_modified": not_modified,
            "seeded": seeded, "items": len(articles or []), "accepted": len(articles or []),
            "new": sum(bool(item.get("is_new")) for item in articles or []), "duration_ms": duration},
            started_at=start, finished_at=end, articles=articles, http_headers=headers)

    def baseline(self, source=SOURCE):
        return self.record(source=source, start="2026-10-05T01:01:00Z", end="2026-10-05T01:01:02Z", articles=[])

    def source_summary(self):
        return self.timing.summary()["sources"][0]

    def test_empty_summary_keeps_metrics_unknown_and_does_not_import_archive(self):
        self.storage.add_article({"source_id": "google", "source_name": "Google", "url": "https://blog.google/old",
            "title": "An archived release", "published_at": "2025-01-01T00:00:00Z"})
        before = self.storage.connection.total_changes
        result = self.timing.summary()
        self.assertEqual(result["fetch_count"], 0)
        self.assertEqual(result["valid_publication_delay_count"], 0)
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["status"], "awaiting_first_fetch")
        self.assertEqual(self.storage.connection.total_changes, before)

    def test_first_inventory_is_excluded_even_if_old_collector_was_seeded(self):
        self.storage.set_metadata("source_seeded:google", True)
        result = self.record(articles=[self.article()], seeded=False)
        self.assertTrue(result["initial_snapshot"])
        metrics = self.source_summary()
        self.assertEqual(metrics["exclusion_counts"], {"initial_snapshot": 1})
        self.assertEqual(metrics["publication_to_receipt_seconds"]["sample_count"], 0)
        self.assertIsNone(metrics["publication_to_receipt_seconds"]["median"])

    def test_fresh_second_response_has_receipt_not_storage_or_delivery_time(self):
        self.baseline()
        self.record(articles=[self.article(first_seen="2026-10-05T01:06:20Z")])
        source = self.source_summary()
        self.assertEqual(source["prospective_receipt_count"], 1)
        self.assertEqual(source["publication_to_receipt_seconds"]["median"], 62)
        self.assertEqual(source["response_elapsed_seconds"]["median"], 2)
        self.assertEqual(source["attempt_duration_seconds"]["median"], 2.2)
        self.assertEqual(source["observed_cadence_seconds"]["median"], 300)

    def test_304_is_a_fetch_but_does_not_establish_a_complete_inventory(self):
        self.record(not_modified=True)
        self.record(start="2026-10-05T01:11:00Z", end="2026-10-05T01:11:03Z", articles=[self.article()])
        result = self.source_summary()
        self.assertEqual(result["not_modified_count"], 1)
        self.assertEqual(result["prospective_receipt_count"], 0)
        self.assertEqual(result["exclusion_counts"], {"initial_snapshot": 1})

    def test_failed_fetch_does_not_establish_full_inventory(self):
        self.record(status="error", articles=[self.article()])
        self.record(start="2026-10-05T01:11:00Z", end="2026-10-05T01:11:03Z", articles=[self.article("two")])
        result = self.source_summary()
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["exclusion_counts"], {"initial_snapshot": 1, "partial_failed_fetch": 1})

    def test_old_archive_backfill_future_and_missing_dates_have_separate_exclusions(self):
        self.baseline()
        self.record(articles=[
            self.article("archive", first_seen="2026-10-04T01:00:00Z", is_new=False),
            self.article("old", published="2026-10-04T01:00:00Z"),
            self.article("future", published="2026-10-05T01:06:03Z"),
            self.article("unknown", published=None),
            self.article("date_only", published="2026-10-05"),
        ])
        result = self.source_summary()
        self.assertEqual(result["prospective_receipt_count"], 0)
        self.assertEqual(result["exclusion_counts"], {"existing_archive": 1,
            "historical_or_late_receipt": 1, "future_publication_time": 1, "missing_publication_time": 2})

    def test_more_than_24_hours_after_publication_is_not_fresh_speed_sample(self):
        self.baseline()
        self.record(start="2026-10-06T02:06:00Z", end="2026-10-06T02:06:02Z", articles=[self.article()])
        self.assertEqual(self.source_summary()["exclusion_counts"], {"historical_or_late_receipt": 1})

    def test_updated_only_timestamp_is_not_treated_as_first_publication(self):
        self.baseline()
        self.record(articles=[{**self.article(), "publication_timestamp_kind": "updated"}])
        source = self.source_summary()
        self.assertEqual(source["exclusion_counts"], {"updated_timestamp_only": 1})
        self.assertIsNone(source["publication_to_receipt_seconds"]["median"])

    def test_failed_attempt_duration_stays_separate_from_http_receipt_time(self):
        self.baseline()
        self.record(status="error", end="2026-10-05T01:06:20Z", duration=20000)
        source = self.source_summary()
        self.assertEqual(source["response_elapsed_seconds"]["sample_count"], 1)
        self.assertEqual(source["response_elapsed_seconds"]["median"], 2)
        self.assertEqual(source["failed_attempt_elapsed_seconds"]["median"], 20)
        self.assertEqual(source["failed_attempt_elapsed_seconds"]["sample_count"], 1)

    def test_timestamp_resolution_preserves_input_field_without_claiming_accuracy(self):
        self.baseline()
        self.record(end="2026-10-05T01:06:02.125Z", articles=[self.article()])
        row = self.storage.connection.execute("SELECT payload FROM source_latency_receipts").fetchone()
        payload = json.loads(row[0])
        self.assertEqual(payload["receipt_timestamp_resolution"], "subsecond_field")
        self.assertEqual(payload["publication_timestamp_resolution"], "second_field")
        self.assertEqual(self.source_summary()["last_receipt_timestamp_resolution"], "subsecond_field")

    def test_canonical_link_can_compare_feed_receipts_despite_global_deduplication(self):
        second = {**SOURCE, "id": "google_other", "name": "Other Google channel"}
        self.baseline()
        self.baseline(second)
        self.record(articles=[self.article()])
        self.record(source=second, start="2026-10-05T01:07:00Z", end="2026-10-05T01:07:01Z",
                    articles=[self.article(is_new=False)])
        result = self.timing.summary()
        comparison = result["same_link_comparisons"][0]
        self.assertEqual(comparison["source_count"], 2)
        self.assertEqual(comparison["first_receipt_status"], "observed_first")
        self.assertEqual([item["seconds_after_first_local_receipt"] for item in comparison["receipts"]], [0, 59])
        self.assertEqual(comparison["comparison_basis"], "same_canonical_link_local_receipt")
        self.assertEqual(result["ranking_status"], "unestablished_no_universal_fastest_claim")

    def test_same_second_is_reported_as_tie_and_never_invented_priority(self):
        second = {**SOURCE, "id": "google_other"}
        self.baseline()
        self.baseline(second)
        self.record(articles=[self.article()])
        self.record(source=second, articles=[self.article(is_new=False)])
        comparison = self.timing.summary()["same_link_comparisons"][0]
        self.assertEqual(comparison["first_receipt_status"], "timestamp_tie")

    def test_repeated_fetch_and_repeated_urls_never_rewrite_receipt(self):
        self.baseline()
        first = self.record(articles=[self.article(), self.article()])
        before = self.storage.connection.total_changes
        duplicate = self.record(articles=[self.article(published="2026-10-05T01:05:30Z")])
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(before, self.storage.connection.total_changes)
        later = self.record(start="2026-10-05T01:11:00Z", end="2026-10-05T01:11:02Z",
                            articles=[self.article(is_new=False)])
        self.assertEqual(first["new_receipts"], 1)
        self.assertEqual(later["new_receipts"], 0)
        self.assertEqual(self.source_summary()["publication_to_receipt_seconds"]["median"], 62)

    def test_headers_are_bounded_whitelisted_and_etag_is_only_a_fingerprint(self):
        self.record(headers={"Date": "Mon, 05 Oct 2026 01:06:00 GMT", "Cache-Control": "max-age=300",
            "Age": "120", "ETag": "opaque-cache-identifier", "Authorization": "secret",
            "Set-Cookie": "session=secret", "Last-Modified": "invalid\r\nInjected: secret"})
        result = self.source_summary()["latest_http_headers"]
        self.assertEqual(result["age_seconds"], 120)
        self.assertEqual(result["cache_control"], "max-age=300")
        self.assertEqual(len(result["etag_sha256"]), 64)
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("opaque-cache-identifier", json.dumps(result))
        self.assertNotIn("last_modified", result)

    def test_negative_or_nonfinite_measurements_do_not_become_zero(self):
        self.record(duration=float("nan"), headers={"Age": "-10"})
        source = self.source_summary()
        self.assertIsNone(source["attempt_duration_seconds"]["median"])
        self.assertEqual(source["attempt_duration_seconds"]["sample_count"], 0)
        self.assertNotIn("age_seconds", source["latest_http_headers"])

    def test_invalid_timing_or_304_inventory_cannot_write_evidence(self):
        for start, end in [("2026-10-05", "2026-10-05T01:01:00Z"),
                           ("2026-10-05T00:59:59Z", "2026-10-05T01:01:00Z"),
                           ("2026-10-05T01:02:00Z", "2026-10-05T01:01:00Z")]:
            with self.assertRaises(ValueError):
                self.record(start=start, end=end)
        with self.assertRaises(ValueError):
            self.record(not_modified=True, articles=[self.article()])
        self.assertEqual(self.timing.summary()["fetch_count"], 0)

    def test_deferred_poll_is_not_a_failed_or_successful_network_observation(self):
        result = self.record(status="deferred")
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(self.timing.summary()["fetch_count"], 0)

    def test_switching_feed_url_under_same_id_requires_new_baseline(self):
        self.baseline()
        result = self.record(source={**SOURCE, "url": "https://blog.google/different-feed"}, articles=[self.article()])
        self.assertTrue(result["initial_snapshot"])
        source = self.source_summary()
        self.assertEqual(source["prospective_receipt_count"], 0)
        self.assertEqual(source["exclusion_counts"], {"initial_snapshot": 1})

    def test_private_or_credential_links_are_omitted(self):
        self.baseline()
        articles = [{**self.article(), "url": url} for url in (
            "https://username:password@blog.google/page", "http://127.0.0.1/internal",
            "https://blog.google/page?api_key=secret", "https://blog.google/" + "x" * 8200)]
        self.record(articles=articles)
        self.assertEqual(self.timing.summary()["prospective_receipt_count"], 0)
        self.assertEqual(self.storage.connection.execute("SELECT COUNT(*) FROM source_latency_receipts").fetchone()[0], 0)

    def test_activation_and_observations_are_append_only(self):
        self.baseline()
        self.record(articles=[self.article()])
        with self.assertRaises(ValueError):
            SourceLatency(self.storage, activated_at="2026-10-05T02:00:00Z")
        for table in ("source_latency_activation", "source_latency_fetches", "source_latency_receipts"):
            for command in (f"DELETE FROM {table}", f"UPDATE {table} SET payload='tampered'" if table != "source_latency_activation" else f"UPDATE {table} SET activated_at='tampered'"):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.storage.connection:
                        self.storage.connection.execute(command)
        self.assertEqual(self.timing.summary()["prospective_receipt_count"], 1)

    def test_summary_limit_and_input_bound_are_enforced(self):
        for index in range(4):
            self.baseline({**SOURCE, "id": f"source_{index}"})
        summary = self.timing.summary(limit=2)
        self.assertEqual(summary["source_count"], 4)
        self.assertEqual(len(summary["sources"]), 2)
        self.assertTrue(summary["sources_truncated"])
        with self.assertRaises(ValueError):
            self.record(articles=[self.article()] * (MAX_ARTICLES + 1))


if __name__ == "__main__":
    unittest.main()
