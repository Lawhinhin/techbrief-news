"""Thread-safe SQLite storage for articles, source health, and alert delivery."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Storage:
    def __init__(self, db_path: str | Path):
        path = Path(db_path)
        if str(db_path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(str(db_path), check_same_thread=False, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=15000")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_category TEXT NOT NULL DEFAULT 'news',
                title TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL DEFAULT '',
                published_at TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                tickers TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS articles_seen ON articles(first_seen_at DESC);
            CREATE INDEX IF NOT EXISTS articles_published ON articles(published_at DESC);
            CREATE TABLE IF NOT EXISTS source_health (
                source_id TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'news',
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'waiting',
                last_attempt_at TEXT,
                last_success_at TEXT,
                last_error TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                total_fetches INTEGER NOT NULL DEFAULT 0,
                total_new_articles INTEGER NOT NULL DEFAULT 0,
                last_item_count INTEGER NOT NULL DEFAULT 0,
                last_accepted_count INTEGER NOT NULL DEFAULT 0,
                last_filtered_count INTEGER NOT NULL DEFAULT 0,
                last_not_modified INTEGER NOT NULL DEFAULT 0,
                last_new_count INTEGER NOT NULL DEFAULT 0,
                last_duration_ms INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id TEXT NOT NULL REFERENCES articles(id),
                channel TEXT NOT NULL DEFAULT 'telegram',
                destination_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                next_attempt_at TEXT NOT NULL,
                last_attempt_at TEXT,
                delivered_at TEXT,
                remote_message_id TEXT,
                last_error TEXT,
                UNIQUE(article_id, channel, destination_key)
            );
            CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(status, next_attempt_at);
            CREATE TABLE IF NOT EXISTS delivery_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
                destination_key TEXT NOT NULL,
                attempted_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS delivery_attempt_window ON delivery_attempts(destination_key, attempted_at);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # Preserve the user's archive when opening a database from version 1.
        health_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(source_health)")}
        for name in ("last_accepted_count", "last_filtered_count", "last_not_modified"):
            if name not in health_columns:
                self.connection.execute(f"ALTER TABLE source_health ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        self.connection.commit()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    @staticmethod
    def _article(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        article = dict(row)
        if isinstance(article.get("tickers"), str):
            article["tickers"] = json.loads(article["tickers"])
        return article

    def add_article(self, article: Mapping[str, Any], delivery_destination_key: str | None = None) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        identifier = hashlib.sha256(article["url"].encode("utf-8")).hexdigest()[:24]
        values = (
            identifier, article["source_id"], article["source_name"],
            article.get("source_category", "news"), article["title"], article["url"],
            article.get("summary", ""), article.get("published_at"), now, now,
            json.dumps(sorted(set(article.get("tickers", [])))),
        )
        with self.lock, self.connection:
            cursor = self.connection.execute("""
                INSERT OR IGNORE INTO articles
                (id, source_id, source_name, source_category, title, url, summary,
                 published_at, first_seen_at, last_seen_at, tickers)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, values)
            is_new = cursor.rowcount == 1
            if not is_new:
                # Keep original publication and first-seen evidence intact.
                self.connection.execute("UPDATE articles SET last_seen_at=? WHERE url=?", (now, article["url"]))
            row = self.connection.execute("SELECT * FROM articles WHERE url=?", (article["url"],)).fetchone()
            if is_new and delivery_destination_key:
                # Article and its alert enter SQLite in the same transaction:
                # a crash cannot leave a stored article with a lost alert.
                self.connection.execute("""
                    INSERT OR IGNORE INTO deliveries (article_id, destination_key, created_at, next_attempt_at)
                    VALUES (?, ?, ?, ?)
                """, (identifier, delivery_destination_key, now, now))
            return self._article(row), is_new

    def get_article(self, article_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
            return self._article(row) if row else None

    def retag_articles(self, tickers: list[Mapping[str, Any]], sources: list[Mapping[str, Any]]) -> int:
        """Reclassify company associations without changing ingestion evidence.

        This migration never queues historical alerts or changes delivery state.
        The collector owns the matching rules so ingestion and migration agree.
        """
        from .collector import match_tickers

        source_by_id = {source["id"]: source for source in sources}
        changed = 0
        with self.lock, self.connection:
            rows = self.connection.execute("SELECT id, source_id, title, summary, tickers FROM articles").fetchall()
            for row in rows:
                source = source_by_id.get(row["source_id"], {})
                associations = match_tickers(row["title"], row["summary"], tickers, company_ticker=source.get("company_ticker"))
                if associations != json.loads(row["tickers"]):
                    self.connection.execute("UPDATE articles SET tickers=? WHERE id=?", (json.dumps(associations), row["id"]))
                    changed += 1
        return changed

    @staticmethod
    def _filters(ticker=None, source=None, search=None, hours=None):
        clauses, parameters = [], []
        if ticker:
            clauses.append("EXISTS (SELECT 1 FROM json_each(articles.tickers) WHERE value = ?)")
            parameters.append(str(ticker).upper())
        if source:
            clauses.append("source_id = ?")
            parameters.append(str(source))
        if search:
            clauses.append("(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\')")
            escaped = str(search).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.extend([f"%{escaped}%", f"%{escaped}%"])
        if hours is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(0, float(hours)))).isoformat(timespec="seconds").replace("+00:00", "Z")
            clauses.append("COALESCE(published_at, first_seen_at) >= ?")
            parameters.append(cutoff)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", parameters

    def list_articles(self, limit: int = 100, ticker=None, source=None, search=None, hours=None, offset: int = 0) -> list[dict[str, Any]]:
        where, parameters = self._filters(ticker, source, search, hours)
        parameters.extend([max(1, min(int(limit), 10000)), max(0, int(offset))])
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM articles" + where + " ORDER BY COALESCE(published_at, first_seen_at) DESC, first_seen_at DESC LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
            return [self._article(row) for row in rows]

    def total_count(self, ticker=None, source=None, search=None, hours=None) -> int:
        where, parameters = self._filters(ticker, source, search, hours)
        with self.lock:
            return self.connection.execute("SELECT COUNT(*) FROM articles" + where, parameters).fetchone()[0]

    def ensure_sources(self, sources: list[dict[str, Any]]) -> None:
        with self.lock, self.connection:
            self.connection.execute("UPDATE source_health SET enabled=0")
            for source in sources:
                self.connection.execute("""
                    INSERT INTO source_health (source_id, source_name, category, enabled)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET source_name=excluded.source_name,
                    category=excluded.category, enabled=excluded.enabled
                """, (source["id"], source.get("name", source["id"]), source.get("category", "news"), int(source.get("enabled", True))))

    def record_source_result(self, source: Mapping[str, Any], *, item_count: int = 0, new_count: int = 0, accepted_count: int | None = None, filtered_count: int = 0, not_modified: bool = False, error: str | None = None, duration_ms: int = 0) -> None:
        now = utc_now()
        preserve_counts = bool(not_modified or error is not None)
        if accepted_count is None:
            accepted_count = item_count
        with self.lock, self.connection:
            self.connection.execute("""
                UPDATE source_health SET last_attempt_at=?, status=?, last_error=?,
                last_success_at=CASE WHEN ? IS NULL THEN ? ELSE last_success_at END,
                consecutive_failures=CASE WHEN ? IS NULL THEN 0 ELSE consecutive_failures+1 END,
                total_fetches=total_fetches+1, total_new_articles=total_new_articles+?,
                last_item_count=CASE WHEN ? THEN last_item_count ELSE ? END,
                last_accepted_count=CASE WHEN ? THEN last_accepted_count ELSE ? END,
                last_filtered_count=CASE WHEN ? THEN last_filtered_count ELSE ? END,
                last_not_modified=?, last_new_count=?, last_duration_ms=? WHERE source_id=?
            """, (now, "ok" if error is None else "error", error, error, now, error, new_count,
                  preserve_counts, item_count, preserve_counts, accepted_count, preserve_counts, filtered_count,
                  int(not_modified), new_count, duration_ms, source["id"]))

    def source_status(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM source_health ORDER BY source_name").fetchall()
            result = [dict(row) for row in rows]
            for row in result:
                row["enabled"] = bool(row["enabled"])
                row["last_not_modified"] = bool(row["last_not_modified"])
                # Migrated databases did not record the accepted/filtered split.
                # A cached response must not present migration defaults as facts.
                if not row["last_success_at"] or row["last_accepted_count"] + row["last_filtered_count"] != row["last_item_count"]:
                    row["last_accepted_count"] = row["last_filtered_count"] = None
            return result

    def enqueue_delivery(self, article_id: str, destination_key: str) -> bool:
        now = utc_now()
        with self.lock, self.connection:
            cursor = self.connection.execute("""
                INSERT OR IGNORE INTO deliveries (article_id, destination_key, created_at, next_attempt_at)
                VALUES (?, ?, ?, ?)
            """, (article_id, destination_key, now, now))
            return cursor.rowcount == 1

    def due_deliveries(self, destination_key: str, limit: int = 10, *, retries_only: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute("""
                SELECT d.*, a.title, a.url, a.source_name, a.published_at, a.tickers
                FROM deliveries d JOIN articles a ON a.id=d.article_id
                WHERE d.status='pending' AND d.destination_key=? AND d.next_attempt_at<=?
                AND (?=0 OR d.attempts>0)
                ORDER BY COALESCE(a.published_at, a.first_seen_at) DESC, d.id LIMIT ?
            """, (destination_key, utc_now(), int(retries_only), max(1, min(int(limit), 100)))).fetchall()
            return [self._article(row) for row in rows]

    def begin_delivery(self, delivery_id: int) -> bool:
        now = utc_now()
        with self.lock, self.connection:
            cursor = self.connection.execute("UPDATE deliveries SET attempts=attempts+1, last_attempt_at=? WHERE id=? AND status='pending' AND next_attempt_at<=?", (now, delivery_id, now))
            if cursor.rowcount != 1:
                return False
            self.connection.execute("""
                INSERT INTO delivery_attempts (delivery_id, destination_key, attempted_at)
                SELECT id, destination_key, ? FROM deliveries WHERE id=?
            """, (now, delivery_id))
            return True

    def delivery_window(self, destination_key: str, *, maximum: int = 5, seconds: int = 60) -> tuple[int, str | None]:
        """Return remaining durable attempt budget and the next budget opening."""
        maximum = max(1, min(int(maximum), 100))
        seconds = max(1, int(seconds))
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.lock:
            rows = self.connection.execute("""
                SELECT attempted_at FROM delivery_attempts WHERE destination_key=? AND attempted_at>? ORDER BY attempted_at, id
            """, (destination_key, cutoff)).fetchall()
        remaining = max(0, maximum - len(rows))
        if remaining:
            return remaining, None
        opens = datetime.fromisoformat(rows[-maximum][0].replace("Z", "+00:00")) + timedelta(seconds=seconds)
        return 0, opens.isoformat(timespec="seconds").replace("+00:00", "Z")

    def set_delivery_cooldown(self, destination_key: str, seconds: int) -> str:
        until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, min(int(seconds), 86400)))).isoformat(timespec="seconds").replace("+00:00", "Z")
        key = "delivery_cooldown:" + destination_key
        with self.lock:
            existing = self.get_metadata(key)
            if existing and existing > until:
                return existing
            self.set_metadata(key, until)
        return until

    def next_delivery_due(self, destination_key: str, *, retries_only: bool = False, maximum_per_minute: int = 5) -> str | None:
        """Return the earliest actionable UTC time including recipient cooldown."""
        with self.lock:
            row = self.connection.execute("""
                SELECT MIN(next_attempt_at) FROM deliveries WHERE status='pending' AND destination_key=?
                AND (?=0 OR attempts>0)
            """, (destination_key, int(retries_only))).fetchone()
            if not row[0]:
                return None
            candidate = row[0]
            cooldown = self.get_metadata("delivery_cooldown:" + destination_key)
            _, window_opens = self.delivery_window(destination_key, maximum=maximum_per_minute)
            return max(value for value in (candidate, cooldown, window_opens) if value)

    def mark_delivered(self, delivery_id: int, message_id: Any) -> None:
        with self.lock, self.connection:
            self.connection.execute("""
                UPDATE deliveries SET status='delivered', delivered_at=?, remote_message_id=?, last_error=NULL WHERE id=?
            """, (utc_now(), str(message_id), delivery_id))

    def fail_delivery(self, delivery_id: int, error: str, retry_seconds: int | None) -> None:
        next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=retry_seconds or 0)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.lock, self.connection:
            self.connection.execute("""
                UPDATE deliveries SET status=?, next_attempt_at=?, last_error=? WHERE id=?
            """, ("pending" if retry_seconds is not None else "failed", next_attempt, error[:300], delivery_id))

    def skip_delivery(self, delivery_id: int, reason: str) -> None:
        with self.lock, self.connection:
            self.connection.execute("UPDATE deliveries SET status='skipped', last_error=? WHERE id=?", (reason[:300], delivery_id))

    def retry_failed_deliveries(self, destination_key: str | None = None) -> int:
        query = "UPDATE deliveries SET status='pending', attempts=0, next_attempt_at=?, last_error=NULL WHERE status='failed'"
        parameters = [utc_now()]
        if destination_key:
            query += " AND destination_key=?"
            parameters.append(destination_key)
        with self.lock, self.connection:
            return self.connection.execute(query, parameters).rowcount

    def list_delivery_status(self, limit: int = 50, *, destination_key: str | None = None) -> list[dict[str, Any]]:
        where = " WHERE d.destination_key=?" if destination_key is not None else ""
        parameters = [destination_key] if destination_key is not None else []
        parameters.append(max(1, min(int(limit), 1000)))
        with self.lock:
            return [dict(row) for row in self.connection.execute("""
                SELECT d.id, d.article_id, d.channel, d.status, d.attempts, d.created_at,
                d.next_attempt_at, d.last_attempt_at, d.delivered_at, d.last_error, a.title
                FROM deliveries d JOIN articles a ON a.id=d.article_id
            """ + where + " ORDER BY d.id DESC LIMIT ?", parameters).fetchall()]

    def summary(self) -> dict[str, Any]:
        with self.lock:
            total = self.connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            matched = self.connection.execute("SELECT COUNT(*) FROM articles WHERE tickers != '[]'").fetchone()[0]
            latest = self.connection.execute("SELECT MAX(first_seen_at) FROM articles").fetchone()[0]
            delivery_counts = {row[0]: row[1] for row in self.connection.execute("SELECT status, COUNT(*) FROM deliveries GROUP BY status")}
            ticker_counts = {row[0]: row[1] for row in self.connection.execute("SELECT value, COUNT(*) FROM articles, json_each(articles.tickers) GROUP BY value")}
            return {"total_articles": total, "matched_articles": matched, "latest_seen_at": latest, "deliveries": delivery_counts, "ticker_counts": ticker_counts}

    def get_metadata(self, key: str, default=None):
        with self.lock:
            row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_metadata(self, key: str, value: Any) -> None:
        with self.lock, self.connection:
            self.connection.execute("INSERT INTO metadata (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
