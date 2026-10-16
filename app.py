"""TechBrief News: a local RSS/Atom collector with optional Telegram delivery."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import secrets
import threading
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from newsradar.collector import Collector
from newsradar.config import ConfigStore, TICKERS
from newsradar.storage import Storage
from newsradar.telegram import TelegramClient, TelegramError, destination_key

ROOT = Path(__file__).resolve().parent
VERSION = "0.1.0"
LOG = logging.getLogger("techbrief")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fingerprint(credentials):
    return hashlib.sha256(credentials.get("telegram_bot_token", "").encode()).hexdigest()


class Application:
    def __init__(self, data_dir=None):
        data_dir = Path(data_dir) if data_dir is not None else ROOT / "data"
        self.settings = ConfigStore(data_dir, json.loads((ROOT / "sources.json").read_text(encoding="utf-8")))
        self.storage = Storage(data_dir / "techbrief.sqlite3")
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.scan_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.telegram_lock = threading.Lock()
        self.collector = Collector(self.storage, *self.settings.snapshot(), self.stop)
        self.csrf_token = secrets.token_urlsafe(32)
        self.pairing_code = secrets.token_urlsafe(12)
        saved = self.storage.get_metadata("last_scan", {})
        self.last_scan = saved.get("finished_at")
        self.next_scan = now()
        self.last_error = None
        self.started_at = now()
        self.workers = []

    def start(self):
        for function, name in ((self._work, "news-collector"), (self._retry, "telegram-retries")):
            worker = threading.Thread(target=function, name=name, daemon=True)
            worker.start()
            self.workers.append(worker)

    def close(self):
        self.stop.set()
        self.wake.set()
        for worker in self.workers:
            worker.join(timeout=15)
        if not any(worker.is_alive() for worker in self.workers):
            self.storage.close()

    def _work(self):
        while not self.stop.is_set():
            self.wake.clear()
            self.scan()
            interval = self.settings.snapshot()[0]["interval_minutes"]
            with self.state_lock:
                self.next_scan = (datetime.now(timezone.utc) + timedelta(minutes=interval)).isoformat(timespec="seconds")
            self.wake.wait(interval * 60)

    def _retry(self):
        while not self.stop.wait(15):
            config, credentials = self.settings.snapshot()
            if not config["telegram_enabled"] or not credentials.get("telegram_bot_token") or not credentials.get("telegram_chat_id"):
                continue
            try:
                self.collector.deliver_pending()
            except Exception:
                LOG.error("Telegram retry failed; saved deliveries remain queued. No credentials were logged.")

    def scan(self):
        if not self.scan_lock.acquire(blocking=False):
            return False
        try:
            self.collector.run_once()
            with self.state_lock:
                self.last_scan, self.last_error = now(), None
            return True
        except Exception:
            with self.state_lock:
                self.last_scan = now()
                self.last_error = "Collection failed. Saved stories remain available; the next scan will retry."
            LOG.error("Collection failed. No credentials or request URLs were logged.")
            return False
        finally:
            self.scan_lock.release()

    def status(self):
        config, credentials = self.settings.snapshot()
        key = destination_key(credentials.get("telegram_chat_id", ""))
        health = {item["source_id"]: item for item in self.storage.source_status()}
        sources = [{**source, **health.get(source["id"], {}), "enabled": source.get("enabled", True)} for source in config["sources"]]
        for source in sources:
            source.setdefault("status", "waiting")
            if not source["enabled"]:
                source["status"] = "disabled"
        with self.storage.lock:
            counts = {row[0]: row[1] for row in self.storage.connection.execute(
                "SELECT status,COUNT(*) FROM deliveries WHERE destination_key=? GROUP BY status", (key,))}
        test = self.storage.get_metadata("telegram_test", {})
        if test.get("destination_key") != key or test.get("bot_fingerprint") != fingerprint(credentials):
            test = {}
        with self.state_lock:
            state = {"last_scan": self.last_scan, "next_scan": self.next_scan, "last_error": self.last_error}
        return dict(state, app="TechBrief News", version=VERSION, running=not self.stop.is_set(),
                    scanning=self.scan_lock.locked(), started_at=self.started_at, csrf_token=self.csrf_token,
                    article_count=self.storage.total_count(), interval_minutes=config["interval_minutes"], sources=sources,
                    watchlist=[{"ticker": item["symbol"], "name": item["name"]} for item in TICKERS if item["symbol"] in config["watchlist"]],
                    telegram={"configured": bool(credentials.get("telegram_bot_token") and credentials.get("telegram_chat_id")),
                              "enabled": config["telegram_enabled"], "pending": counts.get("pending", 0),
                              "delivered": counts.get("delivered", 0), "failed": counts.get("failed", 0),
                              "last_test_status": test.get("status", "not_tested"), "last_test_at": test.get("at"),
                              "pairing_code": self.pairing_code, "last_error": self.settings.credential_error})


class Handler(BaseHTTPRequestHandler):
    server_version = "TechBriefNews/" + VERSION

    @property
    def application(self):
        return self.server.application

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        LOG.debug("HTTP %s %s", self.command, urlsplit(self.path).path)

    def send(self, body, content_type="application/json; charset=utf-8", status=200, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        for key, value in {"Content-Type": content_type, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                           "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                           "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'", **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, value, status=200):
        self.send(json.dumps(value, ensure_ascii=False, allow_nan=False), status=status)

    def local_host(self):
        port = self.server.server_address[1]
        return self.headers.get("Host", "").lower() in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def do_GET(self):
        if not self.local_host():
            self.json({"error": "Only the local dashboard can access this server."}, 403)
            return
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        try:
            if path == "/api/status":
                self.json(self.application.status())
            elif path == "/api/settings":
                config, _ = self.application.settings.snapshot()
                self.json({**{key: config[key] for key in ("interval_minutes", "watchlist", "telegram_enabled", "enabled_sources")},
                           "sources": config["sources"], "companies": [{"symbol": item["symbol"], "name": item["name"]} for item in TICKERS]})
            elif path == "/api/articles" or path == "/api/export.csv":
                options = {key: query.get(key, [None])[0] for key in ("ticker", "source", "search", "hours")}
                if options["hours"] is not None:
                    options["hours"] = min(max(int(options["hours"]), 1), 24 * 365)
                if path.endswith(".csv"):
                    self.export(options)
                else:
                    limit = min(max(int(query.get("limit", ["50"])[0]), 1), 100)
                    offset = min(max(int(query.get("offset", ["0"])[0]), 0), 1_000_000)
                    self.json({"articles": self.application.storage.list_articles(limit=limit, offset=offset, **options),
                               "total": self.application.storage.total_count(**options), "offset": offset, "limit": limit})
            elif path == "/api/deliveries":
                _, credentials = self.application.settings.snapshot()
                self.json({"deliveries": self.application.storage.list_delivery_status(limit=30, destination_key=destination_key(credentials.get("telegram_chat_id", "")))})
            elif path == "/api/sources/latency":
                self.json(self.application.collector.latency.summary(limit=30))
            elif path in {"/", "/index.html", "/app.js", "/style.css"}:
                name = "index.html" if path == "/" else path[1:]
                kind = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
                self.send((ROOT / "web" / name).read_bytes(), kind + "; charset=utf-8")
            else:
                self.json({"error": "Route not found."}, 404)
        except (ValueError, TypeError):
            self.json({"error": "Invalid request parameters."}, 400)
        except Exception:
            LOG.error("Local read failed; no request data or credentials were logged.")
            self.json({"error": "The local archive could not be read. Try again."}, 500)

    def export(self, options):
        fields = ["title", "source_name", "url", "published_at", "first_seen_at", "tickers"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fields, extrasaction="ignore")
        writer.writeheader()
        offset = 0
        # Bounded export avoids an unbounded HTTP response; filters narrow larger archives.
        while offset < 10000:
            articles = self.application.storage.list_articles(limit=100, offset=offset, **options)
            for article in articles:
                item = {**article, "tickers": ", ".join(article["tickers"])}
                for key in fields:
                    text = str(item.get(key) or "")
                    item[key] = "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
                writer.writerow(item)
            if len(articles) < 100:
                break
            offset += 100
        self.send(b"\xef\xbb\xbf" + output.getvalue().encode(), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="techbrief-news.csv"'})

    def do_POST(self):
        # Drain small rejected requests before replying: an unread body can cause
        # Windows to reset the connection before the browser receives our error.
        raw, body_error = b"", False
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 65536:
                raise ValueError("Invalid body length.")
            raw = self.rfile.read(size)
            body_error = not 1 <= size <= 16384 or len(raw) != size
        except (ValueError, OSError):
            body_error = True
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if (not self.local_host() or origin not in allowed | {None}
                or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.application.csrf_token)):
            self.json({"error": "Use this local dashboard to make changes."}, 403)
            return
        try:
            if body_error or not self.headers.get("Content-Type", "").startswith("application/json") or self.headers.get("Transfer-Encoding"):
                raise ValueError("Use a bounded JSON object.")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Use a JSON object.")
            path = urlsplit(self.path).path
            if path == "/api/settings":
                with self.application.settings.lock:
                    before, _ = self.application.settings.snapshot()
                    updated = self.application.settings.update(body)
                    if updated["telegram_enabled"] and not before["telegram_enabled"]:
                        self.application.storage.set_metadata("telegram_enabled_since", now())
                    self.application.collector.update_config(*self.application.settings.snapshot())
                self.application.wake.set()
                self.json({"saved": True})
            elif path == "/api/scan":
                self.application.wake.set()
                self.json({"accepted": True}, 202)
            elif path == "/api/telegram/configure":
                with self.application.telegram_lock:
                    self.configure_telegram(body)
            elif path == "/api/telegram/test":
                _, credentials = self.application.settings.snapshot()
                key = destination_key(credentials.get("telegram_chat_id", ""))
                evidence = {"destination_key": key, "bot_fingerprint": fingerprint(credentials)}
                client = TelegramClient(credentials.get("telegram_bot_token", ""), credentials.get("telegram_chat_id", ""))
                try:
                    client.send_message("TechBrief News is connected. New matching headlines and original links will appear here.")
                except TelegramError as error:
                    self.application.storage.set_metadata("telegram_test", dict(evidence, status="error", at=now()))
                    raise error
                self.application.storage.set_metadata("telegram_test", dict(evidence, status="success", at=now()))
                self.json({"sent": True})
            elif path == "/api/shutdown":
                self.json({"stopped": True})
                self.application.stop.set()
                self.application.wake.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.json({"error": "Route not found."}, 404)
        except TelegramError as error:
            self.json({"error": str(error)}, 400)
        except (ValueError, TypeError):
            self.json({"error": "Check the submitted fields and try again."}, 400)
        except Exception:
            LOG.error("Local change failed; no request data or credentials were logged.")
            self.json({"error": "The change could not be saved. Try again."}, 500)

    def configure_telegram(self, body):
        import re
        token, chat_id = str(body.get("token", "")).strip(), str(body.get("chat_id", "")).strip()
        enabled = body.get("enabled", True)
        if type(enabled) is not bool or not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Use a valid bot token and notification choice.")
        client = TelegramClient(token, chat_id)
        client.get_me()
        if not chat_id:
            if client.get_webhook_info().get("url"):
                raise ValueError("This bot already has a webhook; use a dedicated bot or an explicit chat ID.")
            chats = {str(chat["id"]) for update in client.get_updates()
                     if isinstance(update.get("message"), dict)
                     for message in [update["message"]] for chat in [message.get("chat", {})]
                     if chat.get("type") == "private" and "id" in chat
                     and str(message.get("text", "")).strip() == "/start " + self.application.pairing_code}
            if len(chats) != 1:
                raise ValueError("Send the displayed /start pairing command privately to the bot, then connect again.")
            chat_id = chats.pop()
        with self.application.settings.lock:
            self.application.settings.save_telegram(token, chat_id)
            self.application.storage.set_metadata("telegram_enabled_since", now())
            self.application.storage.set_metadata("telegram_test", {"status": "not_tested", "destination_key": destination_key(chat_id), "bot_fingerprint": fingerprint({"telegram_bot_token": token})})
            self.application.settings.update({"telegram_enabled": enabled})
            self.application.collector.update_config(*self.application.settings.snapshot())
        self.application.pairing_code = secrets.token_urlsafe(12)
        self.application.wake.set()
        self.json({"configured": True})


def main():
    parser = argparse.ArgumentParser(description="Collect tech-company RSS headlines locally and optionally send them to Telegram.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the local dashboard in your browser.")
    parser.add_argument("--once", action="store_true", help="Collect once and exit.")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    application = Application()
    if args.once:
        try:
            return 0 if application.scan() else 1
        finally:
            application.close()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        application.close()
        print("That port is busy. Choose another with --port or open the existing collector.")
        return 1
    server.application, server.daemon_threads = application, True
    application.start()
    address = f"http://127.0.0.1:{args.port}"
    print("TechBrief News: " + address + " (Ctrl+C to stop)")
    if args.open:
        webbrowser.open(address)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        application.stop.set()
        application.wake.set()
        server.server_close()
        application.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
