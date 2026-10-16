"""Collect bounded RSS/Atom feeds and map explicit company mentions to tickers."""
from __future__ import annotations

import copy
import html
import ipaddress
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Mapping

from .storage import Storage, utc_now
from .source_latency import SourceLatency
from .telegram import TelegramClient, TelegramError, destination_key, redact

MAX_FEED_BYTES = 4 * 1024 * 1024
MAX_FEED_ITEMS = 500
TRACKING_PARAMETERS = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "ref_src", "ref_url", "guccounter"}


class FeedError(Exception):
    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _receipt_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _response_metadata(headers) -> dict[str, Any]:
    return {"received_at": _receipt_now(), "headers": {
        name: str(headers.get(name))[:1024] for name in
        ("Date", "Cache-Control", "Age", "Last-Modified", "ETag") if headers and headers.get(name)
    }}


def _retry_after_seconds(raw: str | None) -> int | None:
    if not raw:
        return None
    if raw.strip().isdigit():
        maximum = (datetime.max.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days * 86400
        return min(int(raw.strip()), maximum) if len(raw.strip()) <= 20 else maximum
    parsed = parse_timestamp(raw)
    if parsed:
        return max(0, int((datetime.fromisoformat(parsed.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()))
    return None


def _feed_refresh_minutes(payload: bytes | None, headers: Mapping[str, Any]) -> float:
    """Publisher cache lifetime/RSS TTL are floors, not a real-time SLA."""
    floor = 0.0
    cache = str(headers.get("Cache-Control") or headers.get("cache-control") or "")
    age_raw = str(headers.get("Age") or headers.get("age") or "0").strip()
    age = float(age_raw[:10]) if age_raw.isdigit() and len(age_raw) <= 10 else 0
    for match in re.finditer(r"(?:^|,)\s*(?:s-maxage|max-age)\s*=\s*\"?(\d+)", cache, re.IGNORECASE):
        floor = max(floor, min(max(0, float(match.group(1)[:10]) - age) / 60, 1440))
    if payload is not None:
        # Call only after parse_feed has rejected entity declarations and bad XML.
        root = ET.fromstring(payload)
        for channel in root:
            if _local_name(channel.tag) == "channel":
                raw = _text(_child(channel, ("ttl",)))
                if raw.isdigit():
                    floor = max(floor, min(float(raw[:10]), 1440))
    return floor


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.rsplit(":", 1)[-1]
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "br", "div", "li", "tr"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        tag = tag.rsplit(":", 1)[-1]
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif tag in ("p", "div", "li", "tr"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_text(value: str, limit: int = 2000) -> str:
    parser = _PlainText()
    try:
        parser.feed(str(value)[:50000])
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]*>", " ", str(value))
    text = html.unescape(text)
    text = "".join(char for char in text if char.isprintable() or char.isspace())
    return " ".join(text.split())[:limit]


def normalize_url(raw: str, base_url: str | None = None) -> str | None:
    """Permit public HTTP(S) links and remove fragments and known tracking keys."""
    if not isinstance(raw, str) or len(raw) > 8192 or any(ord(char) < 32 for char in raw):
        return None
    raw = raw.strip()
    if base_url:
        raw = urllib.parse.urljoin(base_url, raw)
    try:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return None
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return None
        except ValueError:
            pass
        port = parsed.port
        if port not in (None, 80, 443):
            return None
        scheme = parsed.scheme.lower()
        netloc = f"[{host}]" if ":" in host else host
        if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc += f":{port}"
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=100)
        pairs = [(key, value) for key, value in pairs if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS]
        query = urllib.parse.urlencode(sorted(pairs))
        return urllib.parse.urlunsplit((scheme, netloc, parsed.path or "/", query, ""))
    except (ValueError, UnicodeError):
        return None


def _allowed_source_url(raw: str, hosts: set[str]) -> str:
    normalized = normalize_url(raw)
    if normalized is None or urllib.parse.urlsplit(normalized).hostname not in hosts:
        raise FeedError("Feed URL or redirect is outside the source hostname allowlist.")
    return normalized


class _SourceRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: set[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _allowed_source_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_feed(source: Mapping[str, Any], *, timeout: float = 12, retries: int = 2, stop_event: threading.Event | None = None, validators: Mapping[str, Any] | None = None) -> tuple[bytes | None, str, dict[str, Any]]:
    raw_url = str(source.get("url", ""))
    specified_hosts = source.get("allowed_hosts") or [urllib.parse.urlsplit(raw_url).hostname or ""]
    hosts = {str(host).lower().rstrip(".") for host in specified_hosts}
    url = _allowed_source_url(raw_url, hosts)
    opener = urllib.request.build_opener(_SourceRedirects(hosts))
    last_error = "Feed network request failed."
    for attempt in range(max(0, min(int(retries), 3)) + 1):
        if stop_event and stop_event.is_set():
            raise FeedError("Collection was stopped.")
        headers = {
            "User-Agent": "NewsRadar/1.0 (+local personal RSS reader)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
            "Accept-Encoding": "identity",
        }
        for key, header in (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")):
            value = (validators or {}).get(key)
            if isinstance(value, str) and len(value) <= 1024 and not any(ord(char) < 32 for char in value):
                headers[header] = value
        request = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(request, timeout=max(1, min(float(timeout), 30))) as response:
                final_url = _allowed_source_url(response.geturl(), hosts)
                size_header = response.headers.get("Content-Length")
                if size_header and size_header.isdecimal() and int(size_header) > MAX_FEED_BYTES:
                    raise FeedError("Feed exceeded the 4 MB size limit.")
                payload = response.read(MAX_FEED_BYTES + 1)
                metadata = _response_metadata(response.headers)
                if len(payload) > MAX_FEED_BYTES:
                    raise FeedError("Feed exceeded the 4 MB size limit.")
                response_validators = {"_response": metadata}
                for key, header in (("etag", "ETag"), ("last_modified", "Last-Modified")):
                    value = response.headers.get(header)
                    if value and len(value) <= 1024 and not any(ord(char) < 32 for char in value):
                        response_validators[key] = value
                return payload, final_url, response_validators
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return None, url, {**dict(validators or {}), "_response": _response_metadata(exc.headers)}
            last_error = f"Feed returned HTTP {exc.code}."
            retry_after = _retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
            if exc.code in (429, 503) and retry_after is not None:
                raise FeedError(last_error, retry_after_seconds=retry_after) from None
            if exc.code not in (408, 429) and exc.code < 500:
                raise FeedError(last_error) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            last_error = "Feed network request failed or timed out."
        if attempt < retries:
            delay = min(2 ** attempt, 4)
            if stop_event:
                if stop_event.wait(delay):
                    raise FeedError("Collection was stopped.")
            else:
                time.sleep(delay)
    raise FeedError(last_error)


def parse_timestamp(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except (ValueError, TypeError, OverflowError):
            return None
    if parsed.tzinfo is None:
        # Dates without an offset cannot establish an authoritative UTC time.
        return None
    try:
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(element: ET.Element, names: tuple[str, ...]) -> ET.Element | None:
    for name in names:
        for child in element:
            if _local_name(child.tag) == name:
                return child
    return None


def _text(element: ET.Element | None) -> str:
    return "" if element is None else "".join(element.itertext()).strip()


def parse_feed(payload: bytes, source: Mapping[str, Any], base_url: str | None = None) -> list[dict[str, Any]]:
    if len(payload) > MAX_FEED_BYTES:
        raise FeedError("Feed exceeded the 4 MB size limit.")
    # Removing NUL bytes also detects declarations in UTF-16/32 feeds before
    # ElementTree can expand internal entities.
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", payload.replace(b"\x00", b""), re.IGNORECASE):
        raise FeedError("Feeds containing XML entity declarations are not supported.")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise FeedError("Feed was not valid RSS/Atom XML.") from None
    root_type = _local_name(root.tag)
    if root_type not in ("rss", "feed", "rdf"):
        raise FeedError("Source returned an unsupported feed format.")
    entries = [node for node in root.iter() if _local_name(node.tag) in ("item", "entry")][:MAX_FEED_ITEMS]
    result = []
    for entry in entries:
        title = clean_text(_text(_child(entry, ("title",))), limit=600)
        if not title:
            continue
        link = ""
        for child in entry:
            if _local_name(child.tag) == "link":
                if child.attrib.get("href") and child.attrib.get("rel", "alternate") == "alternate":
                    link = child.attrib["href"]
                    break
                if child.text and child.text.strip():
                    link = child.text.strip()
                    break
        if not link:
            guid = _child(entry, ("guid", "id"))
            if guid is not None and guid.attrib.get("isPermaLink", "true").lower() != "false":
                link = _text(guid)
        if not link:
            continue
        url = normalize_url(link, base_url or source.get("url"))
        if not url:
            continue
        allowed_article_hosts = source.get("article_allowed_hosts") or source.get("allowed_hosts") or [urllib.parse.urlsplit(str(source.get("url", ""))).hostname]
        if urllib.parse.urlsplit(url).hostname not in {str(host).lower().rstrip(".") for host in allowed_article_hosts}:
            continue
        summary_element = _child(entry, ("description", "summary", "encoded", "content"))
        summary = clean_text(_text(summary_element), limit=2000)
        # Atom XHTML content needs the element markup, otherwise adjacent text joins.
        if summary_element is not None and len(summary_element):
            summary = clean_text(ET.tostring(summary_element, encoding="unicode"), limit=2000)
        published_element = _child(entry, ("pubdate", "published", "date", "updated"))
        published_raw = _text(published_element)
        result.append({
            "source_id": source["id"], "source_name": source.get("name", source["id"]),
            "source_category": source.get("category", "news"), "title": title, "url": url,
            "summary": summary, "published_at": parse_timestamp(published_raw),
            "publication_timestamp_kind": "updated" if published_element is not None and _local_name(published_element.tag) == "updated" else "published",
        })
    return result


def match_tickers(title: str, summary: str, tickers: list[Mapping[str, Any]], *, company_ticker: str | None = None) -> list[str]:
    text = title + " " + summary
    matched = []
    for ticker in tickers:
        symbol = str(ticker.get("symbol", "")).upper()
        if not symbol:
            continue
        # Stock symbols must keep their uppercase spelling. Company aliases are
        # case-insensitive whole phrases, never a substring such as "arm" in "farm".
        symbol_pattern = r"(?<![\w])\$?" + re.escape(symbol) + r"(?![\w])"
        found = bool(re.search(symbol_pattern, text))
        aliases = ticker.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        for alias in aliases:
            alias = str(alias).strip()
            if not alias:
                continue
            pattern = r"(?<![\w])" + re.escape(alias) + r"(?![\w])"
            if re.search(pattern, text, flags=re.IGNORECASE):
                found = True
                break
        if found:
            matched.append(symbol)
    # An official company newsroom provides a direct company association even
    # when its headline only mentions a product, such as Gemini or GeForce.
    # Accept only the explicitly configured source symbol from the catalog.
    if company_ticker:
        company_symbol = str(company_ticker).upper()
        known_symbols = {str(ticker.get("symbol", "")).upper() for ticker in tickers}
        if company_symbol in known_symbols:
            matched.append(company_symbol)
    return sorted(set(matched))


class Collector:
    def __init__(self, storage: Storage, config: Mapping[str, Any], secrets: Mapping[str, Any] | None = None, stop_event: threading.Event | None = None):
        self.storage = storage
        self.stop_event = stop_event or threading.Event()
        self._scan_lock = threading.Lock()
        self._delivery_lock = threading.Lock()
        self._last_send_monotonic: float | None = None
        self._config_lock = threading.RLock()
        self._config: dict[str, Any] = {}
        self._secrets: dict[str, Any] = {}
        self.latency = SourceLatency(storage)
        self.update_config(config, secrets or {})

    def update_config(self, config: Mapping[str, Any], secrets: Mapping[str, Any] | None = None) -> None:
        with self._config_lock:
            self._config = copy.deepcopy(dict(config))
            if secrets is not None:
                previous = self._secrets
                self._secrets = dict(secrets)
                token = self._secrets.get("telegram_bot_token")
                chat_id = self._secrets.get("telegram_chat_id")
                if token and chat_id and previous and previous != self._secrets:
                    self.storage.retry_failed_deliveries(destination_key(str(chat_id)))
            self.storage.ensure_sources(self._config.get("sources", []))

    def _snapshot(self):
        with self._config_lock:
            config, secrets = copy.deepcopy(self._config), dict(self._secrets)
        enabled_since = self.storage.get_metadata("telegram_enabled_since")
        if enabled_since is not None:
            config["telegram_enabled_since"] = enabled_since
        return config, secrets

    def _collect_source(self, source: Mapping[str, Any], config: Mapping[str, Any], secrets: Mapping[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        started_at = _receipt_now()
        received_at = None
        response_headers = {}
        occurrences = []
        new_count = 0
        queued = 0
        seeded = self.storage.get_metadata("source_seeded:" + source["id"], False)
        next_fetch = self.storage.get_metadata("source_next_fetch:" + source["id"])
        if next_fetch and str(next_fetch) > utc_now():
            return {"source_id": source["id"], "status": "deferred", "new": 0, "queued": 0, "next_fetch_at": next_fetch}
        minimum_interval = max(0, min(float(source.get("min_interval_minutes", 0)), 1440))
        if minimum_interval:
            next_fetch = (datetime.now(timezone.utc) + timedelta(minutes=minimum_interval)).isoformat(timespec="seconds").replace("+00:00", "Z")
            self.storage.set_metadata("source_next_fetch:" + source["id"], next_fetch)

        def schedule(minutes: float) -> None:
            # Keep response receipt distinct from later parsing/storage work.
            base = datetime.fromisoformat((received_at or _receipt_now()).replace("Z", "+00:00"))
            if minutes > 0:
                self.storage.set_metadata("source_next_fetch:" + source["id"],
                    (base + timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z"))

        def record(report: dict[str, Any]) -> dict[str, Any]:
            report["duration_ms"] = int((time.monotonic() - started) * 1000)
            try:
                self.latency.record_fetch(source, report, started_at=started_at,
                    finished_at=_receipt_now() if report.get("status") == "error" else received_at or _receipt_now(),
                    http_headers=response_headers, articles=occurrences)
            except Exception:
                # Diagnostic failures must never convert a stored story or alert
                # into a failed collection or expose raw network/credential text.
                logging.getLogger("techbrief").warning("Source timing observation could not be saved; news collection continues.")
            return report

        try:
            validators = self.storage.get_metadata("source_http:" + source["id"], {})
            payload, final_url, updated_validators = fetch_feed(source, timeout=config.get("request_timeout_seconds", 12), retries=config.get("request_retries", 2), stop_event=self.stop_event, validators=validators)
            metadata = updated_validators.get("_response", {})
            received_at = metadata.get("received_at") or _receipt_now()
            response_headers = metadata.get("headers") or {}
            ttl_floor = float(self.storage.get_metadata("source_feed_ttl_minutes:" + source["id"], 0))
            # A validated 304 can refresh cache hints while the RSS TTL remains
            # independent. Do not repeatedly reapply a previous remaining age.
            cache_headers = dict(response_headers)
            if not any(str(key).lower() == "cache-control" for key in cache_headers):
                cache_headers["Cache-Control"] = self.storage.get_metadata("source_cache_control:" + source["id"], "")
            if payload is None:
                refresh_floor = max(ttl_floor, _feed_refresh_minutes(None, cache_headers))
                self.storage.set_metadata("source_refresh_minutes:" + source["id"], refresh_floor)
                floor = max(minimum_interval, refresh_floor)
                schedule(floor)
                self.storage.set_metadata("source_failure_count:" + source["id"], 0)
                self.storage.record_source_result(source, not_modified=True, duration_ms=int((time.monotonic() - started) * 1000))
                return record({"source_id": source["id"], "status": "ok", "not_modified": True, "items": 0, "new": 0, "queued": 0})
            articles = parse_feed(payload, source, final_url)
            refresh_floor = _feed_refresh_minutes(payload, response_headers)
            self.storage.set_metadata("source_feed_ttl_minutes:" + source["id"], _feed_refresh_minutes(payload, {}))
            self.storage.set_metadata("source_cache_control:" + source["id"], str(response_headers.get("Cache-Control") or response_headers.get("cache-control") or "")[:1024])
            self.storage.set_metadata("source_refresh_minutes:" + source["id"], refresh_floor)
            schedule(max(minimum_interval, refresh_floor))
            accepted = 0
            for article in articles:
                article["tickers"] = match_tickers(article["title"], article["summary"], config.get("tickers", []), company_ticker=source.get("company_ticker"))
                if source.get("requires_ticker") and not article["tickers"]:
                    continue
                accepted += 1
                provisional = {**article, "first_seen_at": utc_now()}
                alert_destination = destination_key(str(secrets["telegram_chat_id"])) if seeded and self._should_alert(provisional, config, secrets) else None
                stored, is_new = self.storage.add_article(article, delivery_destination_key=alert_destination)
                occurrences.append({"id": stored["id"], "url": article["url"],
                    "published_at": article["published_at"], "first_seen_at": stored["first_seen_at"],
                    "publication_timestamp_kind": article.get("publication_timestamp_kind"), "is_new": is_new})
                new_count += int(is_new)
                queued += int(is_new and alert_destination is not None)
            self.storage.set_metadata("source_seeded:" + source["id"], True)
            # Cache only a feed that parsed and stored successfully. Otherwise a
            # later 304 could turn an invalid response into a healthy source.
            self.storage.set_metadata("source_http:" + source["id"], {key: updated_validators[key] for key in ("etag", "last_modified") if key in updated_validators})
            self.storage.set_metadata("source_failure_count:" + source["id"], 0)
            self.storage.record_source_result(source, item_count=len(articles), accepted_count=accepted, filtered_count=len(articles) - accepted, new_count=new_count, duration_ms=int((time.monotonic() - started) * 1000))
            return record({"source_id": source["id"], "status": "ok", "items": len(articles), "accepted": accepted, "filtered": len(articles) - accepted, "new": new_count, "queued": queued, "seeded": not seeded})
        except Exception as exc:
            error = str(exc) if isinstance(exc, FeedError) else "Source collection failed (" + type(exc).__name__ + ")."
            error = redact(error, secrets)
            failures = min(int(self.storage.get_metadata("source_failure_count:" + source["id"], 0)) + 1, 5)
            self.storage.set_metadata("source_failure_count:" + source["id"], failures)
            schedule(max(minimum_interval, min(5 * 2 ** (failures - 1), 60), (getattr(exc, "retry_after_seconds", None) or 0) / 60))
            self.storage.record_source_result(source, error=error, new_count=new_count, duration_ms=int((time.monotonic() - started) * 1000))
            return record({"source_id": source["id"], "status": "error", "new": new_count, "queued": queued, "error": error})

    @staticmethod
    def _should_alert(article: Mapping[str, Any], config: Mapping[str, Any], secrets: Mapping[str, Any]) -> bool:
        if not config.get("telegram_enabled", False) or not secrets.get("telegram_bot_token") or not secrets.get("telegram_chat_id"):
            return False
        watchlist = {str(symbol).upper() for symbol in config.get("watchlist", [])}
        if not watchlist.intersection(article.get("tickers", [])) or not article.get("published_at"):
            return False
        try:
            published = datetime.fromisoformat(article["published_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        if published.tzinfo is None:
            return False
        now = datetime.now(timezone.utc)
        max_age = max(1, min(float(config.get("telegram_max_age_hours", 24)), 168))
        if not now - timedelta(hours=max_age) <= published <= now + timedelta(minutes=5):
            return False
        enable_since = config.get("telegram_enabled_since")
        if enable_since:
            try:
                first_seen = datetime.fromisoformat(str(article["first_seen_at"]).replace("Z", "+00:00"))
                enabled = datetime.fromisoformat(str(enable_since).replace("Z", "+00:00"))
                if first_seen.tzinfo is None or enabled.tzinfo is None or first_seen < enabled:
                    return False
            except (ValueError, TypeError):
                return False
        return True

    def deliver_pending(self, *, retries_only: bool = False) -> dict[str, Any]:
        """Deliver one bounded outbox batch without fetching any news feeds.

        Call from a separate retry worker or from a news scan. Both routes share
        the same lock, durable attempt budget, and current-settings checks.
        """
        started = time.monotonic()
        config, secrets = self._snapshot()
        report = self._send_pending(config, secrets, retries_only=retries_only)
        report.update(finished_at=utc_now(), duration_ms=int((time.monotonic() - started) * 1000))
        if report.get("status") != "busy":
            self.storage.set_metadata("last_delivery", report)
        return report

    def _send_pending(self, config: Mapping[str, Any], secrets: Mapping[str, Any], *, retries_only: bool = False) -> dict[str, Any]:
        if not self._delivery_lock.acquire(blocking=False):
            return {"status": "busy", "busy": True, "delivered": 0, "failed": 0, "retrying": 0, "skipped": 0,
                    "destination_key": destination_key(str(secrets.get("telegram_chat_id", "")))}
        try:
            return self._send_pending_locked(config, secrets, retries_only=retries_only)
        finally:
            self._delivery_lock.release()

    def _send_pending_locked(self, config: Mapping[str, Any], secrets: Mapping[str, Any], *, retries_only: bool = False) -> dict[str, Any]:
        status: dict[str, Any] = {"delivered": 0, "failed": 0, "retrying": 0, "skipped": 0,
                                 "destination_key": destination_key(str(secrets.get("telegram_chat_id", "")))}
        if not config.get("telegram_enabled", False):
            status["status"] = "disabled"
            return status
        token, chat_id = secrets.get("telegram_bot_token"), secrets.get("telegram_chat_id")
        if not token or not chat_id:
            status["status"] = "unconfigured"
            return status
        key = destination_key(str(chat_id))
        client = TelegramClient(str(token), str(chat_id), timeout=config.get("request_timeout_seconds", 12))
        status["status"] = "ready"
        maximum_per_minute = max(1, min(int(config.get("telegram_max_per_minute", 5)), 20))
        next_due = self.storage.next_delivery_due(key, retries_only=retries_only, maximum_per_minute=maximum_per_minute)
        if next_due and next_due > utc_now():
            status.update(status="waiting", next_due_at=next_due)
            return status
        for delivery in self.storage.due_deliveries(key, limit=config.get("telegram_max_per_scan", 5), retries_only=retries_only):
            if self.stop_event.is_set():
                break
            current_config, current_secrets = self._snapshot()
            if not current_config.get("telegram_enabled", False) or current_secrets != dict(secrets):
                break
            if self._last_send_monotonic is not None:
                spacing = max(0, 1.1 - (time.monotonic() - self._last_send_monotonic))
                if spacing and self.stop_event.wait(spacing):
                    break
            # Settings may change while the previous request or rate-limit
            # spacing is in progress. Check again immediately before sending.
            current_config, current_secrets = self._snapshot()
            if not current_config.get("telegram_enabled", False) or current_secrets != dict(secrets):
                break
            stored = self.storage.get_article(delivery["article_id"])
            if not stored or not self._should_alert(stored, current_config, current_secrets):
                self.storage.skip_delivery(delivery["id"], "Article is outside the current watchlist or alert time window.")
                status["skipped"] += 1
                continue
            remaining, budget_opens = self.storage.delivery_window(key, maximum=maximum_per_minute)
            if not remaining:
                status.update(status="waiting", next_due_at=budget_opens)
                break
            if not self.storage.begin_delivery(delivery["id"]):
                continue
            self._last_send_monotonic = time.monotonic()
            try:
                sent = client.send_article(delivery)
                self.storage.mark_delivered(delivery["id"], sent["message_id"])
                status["delivered"] += 1
            except Exception as exc:
                if isinstance(exc, TelegramError):
                    error = redact(str(exc), secrets)
                    transient = exc.transient
                    retry_after = exc.retry_after
                else:
                    error, transient, retry_after = "Telegram delivery failed.", True, None
                attempts = delivery["attempts"] + 1
                retry = max(retry_after or 0, min(60 * (2 ** min(attempts - 1, 8)), 3600)) if transient and attempts < 8 else None
                self.storage.fail_delivery(delivery["id"], error, retry)
                status["retrying" if retry is not None else "failed"] += 1
                status["last_error"] = error
                if isinstance(exc, TelegramError) and (exc.code == 429 or not exc.transient):
                    if exc.code == 429:
                        self.storage.set_delivery_cooldown(key, retry_after or retry or 60)
                    break
        status["next_due_at"] = self.storage.next_delivery_due(key, retries_only=retries_only, maximum_per_minute=maximum_per_minute)
        return status

    def run_once(self) -> dict[str, Any]:
        if not self._scan_lock.acquire(blocking=False):
            return {"busy": True, "status": "already_running"}
        started = time.monotonic()
        started_at = utc_now()
        try:
            config, secrets = self._snapshot()
            sources = [source for source in config.get("sources", []) if source.get("enabled", True)]
            results = []
            with ThreadPoolExecutor(max_workers=min(4, max(1, len(sources))), thread_name_prefix="news-feed") as executor:
                futures = [executor.submit(self._collect_source, source, config, secrets) for source in sources]
                for future in as_completed(futures):
                    results.append(future.result())
            # Re-read settings before delivery so disabling Telegram during a scan
            # takes effect without restarting the collector.
            telegram = self.deliver_pending()
            report = {
                "status": "complete", "started_at": started_at, "finished_at": utc_now(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "sources_ok": sum(result["status"] == "ok" for result in results),
                "sources_error": sum(result["status"] == "error" for result in results),
                "sources_deferred": sum(result["status"] == "deferred" for result in results),
                "new_articles": sum(result.get("new", 0) for result in results),
                "queued_alerts": sum(result.get("queued", 0) for result in results),
                "sources": sorted(results, key=lambda result: result["source_id"]), "telegram": telegram,
            }
            self.storage.set_metadata("last_scan", report)
            return report
        finally:
            self._scan_lock.release()

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            self.run_once()
            config, _ = self._snapshot()
            self.stop_event.wait(max(60, min(int(config.get("interval_minutes", 15)) * 60, 86400)))
