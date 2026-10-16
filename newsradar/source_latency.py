"""Append-only observations of feed receipt timing, without claiming origin priority."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import statistics
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Mapping

from .storage import Storage, utc_now

METHOD_VERSION = "source-receipt-v1"
MAX_ARTICLES = 500
METRIC_WINDOW = 1000
FRESHNESS_SECONDS = 24 * 60 * 60
HEADERS = {"date", "cache-control", "age", "last-modified", "etag"}
SENSITIVE_QUERY = {"token", "api_key", "apikey", "key", "access_token", "signature", "password"}


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 100:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _iso(parsed: datetime) -> str:
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _resolution(value: Any) -> str:
    if _time(value) is None:
        return "unknown"
    clock = value.split("T", 1)[-1].split(" ", 1)[-1]
    if "." in clock:
        return "subsecond_field"
    if clock.count(":") >= 2:
        return "second_field"
    return "minute_field"


def _text(value: Any, limit: int = 500) -> str | None:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        return None
    return value[:limit]


def _url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 8192:
        return None
    value = _text(value, 8192)
    if not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme.lower() not in ("https", "http") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname.rstrip(".").lower()
        if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        port = parsed.port
        if port not in (None, 80, 443):
            return None
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=100)
        # Links with credentials are omitted, rather than exposed or silently merged.
        if any(key.lower() in SENSITIVE_QUERY for key, _ in pairs):
            return None
        netloc = f"[{host}]" if ":" in host else host
        if port and not (parsed.scheme.lower() == "https" and port == 443 or parsed.scheme.lower() == "http" and port == 80):
            netloc += f":{port}"
        return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", urllib.parse.urlencode(sorted(pairs)), ""))
    except (ValueError, UnicodeError):
        return None


def _number(value: Any, *, maximum: float = 10**9) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) and 0 <= parsed <= maximum else None
    except (ValueError, TypeError, OverflowError):
        return None


def _headers(headers: Mapping[str, Any] | None) -> dict[str, Any]:
    result = {}
    for key, value in (headers or {}).items():
        key = str(key).lower().replace("_", "-")
        if key not in HEADERS:
            continue
        text = _text(value, 1024)
        if text is None:
            continue
        if key == "etag":
            # An opaque cache identifier is useful as a fingerprint, not a public token.
            result["etag_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        elif key == "age":
            number = _number(text, maximum=366 * 86400)
            if number is not None:
                result["age_seconds"] = number
        else:
            result[key.replace("-", "_")] = text
    return result


def _statistics(values: list[float], *, count_key: str = "sample_count") -> dict[str, Any]:
    if not values:
        return {count_key: 0, "median": None, "p95": None, "min": None, "max": None}
    values = sorted(values)
    return {count_key: len(values), "median": statistics.median(values),
            "p95": values[max(0, math.ceil(.95 * len(values)) - 1)], "min": values[0], "max": values[-1]}


class SourceLatency:
    """Record actual collector observations; GET summaries never import old articles."""

    def __init__(self, storage: Storage, *, activated_at: str | None = None):
        self.storage = storage
        supplied = _time(activated_at or utc_now())
        if supplied is None:
            raise ValueError("Activation requires an offset-aware timestamp.")
        with storage.lock, storage.connection:
            storage.connection.executescript("""
                CREATE TABLE IF NOT EXISTS source_latency_activation (
                    id INTEGER PRIMARY KEY CHECK (id=1), activated_at TEXT NOT NULL,
                    method_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_latency_fetches (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL, status TEXT NOT NULL, not_modified INTEGER NOT NULL,
                    payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS source_latency_fetch_source
                    ON source_latency_fetches(source_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS source_latency_receipts (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, url TEXT NOT NULL,
                    fetch_id TEXT NOT NULL REFERENCES source_latency_fetches(id),
                    received_at TEXT NOT NULL, kind TEXT NOT NULL, delay_seconds REAL,
                    payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                    UNIQUE(source_id,url)
                );
                CREATE INDEX IF NOT EXISTS source_latency_receipt_source
                    ON source_latency_receipts(source_id, received_at DESC);
                CREATE INDEX IF NOT EXISTS source_latency_receipt_url
                    ON source_latency_receipts(url, kind);
            """)
            existing = storage.connection.execute("SELECT * FROM source_latency_activation WHERE id=1").fetchone()
            if existing and activated_at is not None and existing["activated_at"] != _iso(supplied):
                raise ValueError("The source timing activation timestamp is immutable.")
            if not existing:
                storage.connection.execute("INSERT INTO source_latency_activation VALUES(1,?,?)", (_iso(supplied), METHOD_VERSION))
            for table in ("source_latency_activation", "source_latency_fetches", "source_latency_receipts"):
                for operation in ("UPDATE", "DELETE"):
                    storage.connection.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()}_immutable
                        BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Source timing observations are immutable'); END""")
        with storage.lock:
            row = storage.connection.execute("SELECT * FROM source_latency_activation WHERE id=1").fetchone()
        self.activated_at = row["activated_at"]

    def record_fetch(self, source: Mapping[str, Any], report: Mapping[str, Any], *,
                     started_at: str, finished_at: str, http_headers: Mapping[str, Any] | None = None,
                     articles: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """articles are accepted feed occurrences, including already archived URLs.

        finished_at is actual response receipt, not the end of Telegram delivery.
        report.duration_ms may include retry, parsing and storage overhead.
        """
        if report.get("status") == "deferred":
            return {"status": "deferred", "new_receipts": 0}
        status = report.get("status")
        if status not in ("ok", "error"):
            raise ValueError("Fetch status must be ok or error.")
        start, finish = _time(started_at), _time(finished_at)
        activation = _time(self.activated_at)
        if start is None or finish is None or finish < start or start < activation:
            raise ValueError("Fetch timestamps must follow activation and preserve receipt ordering.")
        source_id = _text(source.get("id"), 100)
        if not source_id:
            raise ValueError("A source identifier is required.")
        if articles is not None and (not isinstance(articles, list) or len(articles) > MAX_ARTICLES):
            raise ValueError("At most 500 accepted feed occurrences are supported per response.")
        not_modified = bool(report.get("not_modified"))
        if not_modified and articles:
            raise ValueError("A 304 response has no new article inventory.")
        identity = {"method_version": METHOD_VERSION, "source_id": source_id,
                    "started_at": _iso(start), "finished_at": _iso(finish), "status": status}
        identifier = _digest(identity)
        fetch = {**identity, "id": identifier, "source_name": _text(source.get("name"), 200) or source_id,
                 "source_category": _text(source.get("category"), 100), "source_url": _url(source.get("url")),
                 "configured_min_interval_minutes": _number(source.get("min_interval_minutes"), maximum=1440),
                 "not_modified": not_modified, "initial_source_import": bool(report.get("seeded")),
                 "items": _number(report.get("items"), maximum=MAX_ARTICLES),
                 "accepted": _number(report.get("accepted"), maximum=MAX_ARTICLES),
                 "new": _number(report.get("new"), maximum=MAX_ARTICLES),
                 "attempt_duration_ms": _number(report.get("duration_ms"), maximum=3600000),
                 "receipt_elapsed_ms": (finish - start).total_seconds() * 1000,
                 "receipt_timestamp_resolution": _resolution(finished_at),
                 "http_headers": _headers(http_headers), "error": _text(report.get("error"), 300)}
        created = prospective = 0
        with self.storage.lock, self.storage.connection:
            connection = self.storage.connection
            if connection.execute("SELECT 1 FROM source_latency_fetches WHERE id=?", (identifier,)).fetchone():
                return {"status": "duplicate", "fetch_id": identifier, "new_receipts": 0, "prospective_receipts": 0}
            prior_inventory = connection.execute("""SELECT payload FROM source_latency_fetches
                WHERE source_id=? AND status='ok' AND not_modified=0
                ORDER BY started_at DESC,id DESC LIMIT 1""", (source_id,)).fetchone()
            same_feed_inventory = prior_inventory is not None and json.loads(prior_inventory[0])["source_url"] == fetch["source_url"]
            initial = bool(report.get("seeded")) or not same_feed_inventory
            serialized = _dump(fetch)
            connection.execute("INSERT INTO source_latency_fetches VALUES(?,?,?,?,?,?,?,?)",
                (identifier, source_id, _iso(start), _iso(finish), status, int(not_modified), serialized,
                 hashlib.sha256(serialized.encode("utf-8")).hexdigest()))
            for article in articles or []:
                url = _url(article.get("url"))
                if not url:
                    continue
                published, first_seen = _time(article.get("published_at")), _time(article.get("first_seen_at"))
                delay = (finish - published).total_seconds() if published else None
                timestamp_kind = article.get("publication_timestamp_kind")
                timestamp_kind = timestamp_kind if timestamp_kind in ("published", "updated") else "unspecified"
                # Start with a complete feed inventory. Old articles cannot become
                # fresh receipt measurements merely because this module is new.
                if status != "ok":
                    kind = "partial_failed_fetch"
                elif initial:
                    kind = "initial_snapshot"
                elif not article.get("is_new") and (first_seen is None or first_seen < activation):
                    kind = "existing_archive"
                elif timestamp_kind == "updated":
                    kind = "updated_timestamp_only"
                elif published is None:
                    kind = "missing_publication_time"
                elif delay < 0:
                    kind = "future_publication_time"
                elif published < activation or delay > FRESHNESS_SECONDS:
                    kind = "historical_or_late_receipt"
                else:
                    kind = "prospective"
                receipt = {"method_version": METHOD_VERSION, "source_id": source_id,
                    "url": url, "article_id": _text(article.get("id") or article.get("article_id"), 100),
                    "fetch_id": identifier, "received_at": _iso(finish),
                    "published_at": _iso(published) if published else None,
                    "archive_first_seen_at": _iso(first_seen) if first_seen else None,
                    "is_new_in_global_archive": bool(article.get("is_new")), "kind": kind,
                    "publication_to_receipt_seconds": delay,
                    "publication_timestamp_kind": timestamp_kind,
                    "publication_timestamp_resolution": _resolution(article.get("published_at")),
                    "receipt_timestamp_resolution": _resolution(finished_at),
                    "publication_timestamp_status": "publisher_reported_not_verified_origin_release"}
                serialized = _dump(receipt)
                cursor = connection.execute("INSERT OR IGNORE INTO source_latency_receipts VALUES(?,?,?,?,?,?,?,?,?)",
                    (_digest({"source_id": source_id, "url": url, "method_version": METHOD_VERSION}),
                     source_id, url, identifier, _iso(finish), kind, delay if kind == "prospective" else None,
                     serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()))
                created += int(cursor.rowcount == 1)
                prospective += int(cursor.rowcount == 1 and kind == "prospective")
        return {"status": "recorded", "fetch_id": identifier, "new_receipts": created,
                "prospective_receipts": prospective, "initial_snapshot": initial}

    def summary(self, limit: int = 30) -> dict[str, Any]:
        """Return bounded diagnostic evidence, not a universal fastest-source ranking."""
        limit = max(1, min(int(limit), 50))
        sources = []
        with self.storage.lock:
            connection = self.storage.connection
            totals = connection.execute("SELECT COUNT(*) AS n,COUNT(DISTINCT source_id) AS sources FROM source_latency_fetches").fetchone()
            prospective = connection.execute("SELECT COUNT(*) FROM source_latency_receipts WHERE kind='prospective'").fetchone()[0]
            source_ids = [row[0] for row in connection.execute("""SELECT source_id FROM source_latency_fetches
                GROUP BY source_id ORDER BY MAX(started_at) DESC,source_id LIMIT ?""", (limit,))]
            for source_id in source_ids:
                rows = connection.execute("""SELECT * FROM source_latency_fetches WHERE source_id=?
                    ORDER BY started_at DESC,id DESC LIMIT ?""", (source_id, METRIC_WINDOW)).fetchall()
                last = json.loads(rows[0]["payload"])
                all_counts = connection.execute("""SELECT COUNT(*) AS attempted,
                    SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS successful,
                    SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed,
                    SUM(not_modified) AS not_modified, MIN(started_at) AS first_attempt_at
                    FROM source_latency_fetches WHERE source_id=?""", (source_id,)).fetchone()
                payloads = [json.loads(row["payload"]) for row in rows]
                attempts = sorted(_time(row["started_at"]) for row in rows)
                cadence = [(later - earlier).total_seconds() for earlier, later in zip(attempts, attempts[1:])]
                receipts = connection.execute("""SELECT * FROM source_latency_receipts WHERE source_id=? AND kind='prospective'
                    ORDER BY received_at DESC,id DESC LIMIT ?""", (source_id, METRIC_WINDOW)).fetchall()
                exclusions = {row[0]: row[1] for row in connection.execute("""SELECT kind,COUNT(*) FROM source_latency_receipts
                    WHERE source_id=? AND kind!='prospective' GROUP BY kind""", (source_id,))}
                sources.append({"source_id": source_id, "source_name": last["source_name"],
                    "source_category": last["source_category"], "source_url": last["source_url"],
                    "configured_min_interval_minutes": last["configured_min_interval_minutes"],
                    "attempt_count": all_counts["attempted"], "successful_count": all_counts["successful"],
                    "failed_count": all_counts["failed"], "not_modified_count": all_counts["not_modified"],
                    "first_attempt_at": all_counts["first_attempt_at"], "last_attempt_at": last["started_at"],
                    "last_response_at": last["finished_at"], "last_status": last["status"], "last_error": last["error"],
                    "latest_http_headers": last["http_headers"], "metric_window_fetch_count": len(rows),
                    "last_receipt_timestamp_resolution": last["receipt_timestamp_resolution"],
                    "attempt_duration_seconds": _statistics([row["attempt_duration_ms"] / 1000 for row in payloads if row["attempt_duration_ms"] is not None]),
                    "response_elapsed_seconds": _statistics([row["receipt_elapsed_ms"] / 1000 for row in payloads if row["status"] == "ok"]),
                    "failed_attempt_elapsed_seconds": _statistics([row["receipt_elapsed_ms"] / 1000 for row in payloads if row["status"] == "error"]),
                    "observed_cadence_seconds": _statistics(cadence),
                    "publication_to_receipt_seconds": _statistics([row["delay_seconds"] for row in receipts]),
                    "prospective_receipt_count": connection.execute("SELECT COUNT(*) FROM source_latency_receipts WHERE source_id=? AND kind='prospective'", (source_id,)).fetchone()[0],
                    "exclusion_counts": exclusions})
            links = connection.execute("""SELECT url,COUNT(*) AS source_count,MAX(received_at) AS last_receipt
                FROM source_latency_receipts WHERE kind='prospective' GROUP BY url HAVING COUNT(*)>1
                ORDER BY last_receipt DESC,url LIMIT ?""", (limit,)).fetchall()
            comparisons = []
            for link in links:
                rows = connection.execute("""SELECT r.source_id,r.received_at,f.payload FROM source_latency_receipts r
                    JOIN source_latency_fetches f ON f.id=r.fetch_id
                    WHERE r.url=? AND r.kind='prospective' ORDER BY r.received_at,r.source_id LIMIT 20""", (link["url"],)).fetchall()
                first = _time(rows[0]["received_at"])
                tied = sum(_time(row["received_at"]) == first for row in rows) > 1
                comparisons.append({"url": link["url"], "source_count": link["source_count"],
                    "receipts_truncated": link["source_count"] > len(rows), "comparison_basis": "same_canonical_link_local_receipt",
                    "first_receipt_at": _iso(first), "first_receipt_status": "timestamp_tie" if tied else "observed_first",
                    "receipts": [{"source_id": row["source_id"], "source_name": json.loads(row["payload"])["source_name"],
                        "received_at": row["received_at"], "seconds_after_first_local_receipt": (_time(row["received_at"]) - first).total_seconds()} for row in rows]})
        return {"method_version": METHOD_VERSION, "activated_at": self.activated_at,
            "status": "observing" if totals["n"] else "awaiting_first_fetch", "fetch_count": totals["n"],
            "source_count": totals["sources"], "sources_truncated": totals["sources"] > len(sources),
            "prospective_receipt_count": prospective, "valid_publication_delay_count": prospective,
            "metric_window_limit": METRIC_WINDOW, "freshness_window_seconds": FRESHNESS_SECONDS,
            "sources": sources, "same_link_comparisons": comparisons,
            "ranking_status": "unestablished_no_universal_fastest_claim",
            "caveats": [
                "Publication timestamps are publisher claims, not verified first public release times.",
                "The first complete feed inventory and previously archived articles are excluded from fresh timing samples.",
                "Fresh timing samples require an offset-aware publication timestamp after activation and no more than 24 hours before receipt; late or historical receipts remain separate.",
                "Updated-only feed timestamps are excluded from publication-delay samples. Timestamp resolution describes the encoded field, not verified publication accuracy.",
                "Response elapsed time includes retries and depends on the local clock; collection duration may also include parsing and storage.",
                "Publication-to-receipt gaps include feed publication delay, caching, poll cadence and computer downtime.",
                "Same-link comparisons describe receipt on this computer, not independent confirmation of the same event or its original publisher.",
                "Only selected accepted feed items are observed; no X, website, wire or SEC channel is measured unless explicitly integrated.",
                "Per-source timing distributions use at most the latest 1000 observations; null metrics mean no eligible samples.",
                "Cache headers describe the observed response and do not prove origin publication order."
            ]}
