"""Telegram Bot API client. Exceptions never contain request URLs or credentials."""
from __future__ import annotations

import hashlib
import json
import re
import socket
import urllib.error
import urllib.request
from typing import Any, Mapping


class TelegramError(Exception):
    def __init__(self, message: str, *, transient: bool = True, retry_after: int | None = None, code: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after
        self.code = code


def redact(value: str, secrets: Mapping[str, Any] | None = None) -> str:
    text = str(value)
    if secrets:
        for secret in secrets.values():
            if isinstance(secret, str) and secret:
                text = text.replace(secret, "[redacted]")
    return re.sub(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])", "[redacted]", text)[:300]


def destination_key(chat_id: str) -> str:
    """Store an identifier for the recipient without storing its raw chat ID."""
    return hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:20]


def format_article(article: Mapping[str, Any]) -> str:
    symbols = ", ".join(article.get("tickers", [])) or "Tech news"
    published = article.get("published_at") or "Publication time unavailable"
    title = str(article.get("title", "Untitled"))[:600]
    source = str(article.get("source_name", "Source"))[:150]
    url = str(article.get("url", ""))[:2000]
    return f"{symbols}\n{title}\n\n{source} · {published}\n{url}\n\nCompany associations from source or text; no stock-impact prediction."


class TelegramClient:
    def __init__(self, token: str, chat_id: str, timeout: float = 12):
        self._token = str(token).strip()
        self._chat_id = str(chat_id).strip()
        self.timeout = max(1, min(float(timeout), 30))
        if not self._token:
            raise TelegramError("Telegram bot token is missing.", transient=False)

    def __repr__(self) -> str:
        return "TelegramClient(token=[redacted], chat_id=[redacted])"

    def send_message(self, text: str) -> dict[str, Any]:
        if not self._chat_id:
            raise TelegramError("Telegram chat ID is missing.", transient=False)
        # Plain text avoids HTML/Markdown injection by publisher-controlled titles.
        result = self._call("sendMessage", {"chat_id": self._chat_id, "text": text[:4096], "disable_web_page_preview": True})
        if not isinstance(result, dict) or "message_id" not in result:
            raise TelegramError("Telegram response omitted the delivery confirmation.")
        return result

    def get_me(self) -> dict[str, Any]:
        result = self._call("getMe", {})
        if not isinstance(result, dict):
            raise TelegramError("Telegram returned an invalid bot profile.")
        return result

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        parameters = {"timeout": max(0, min(int(timeout), 20)), "limit": 100, "allowed_updates": ["message"]}
        if offset is not None:
            parameters["offset"] = int(offset)
        result = self._call("getUpdates", parameters)
        if not isinstance(result, list):
            raise TelegramError("Telegram returned an invalid update list.")
        return result

    def get_webhook_info(self) -> dict[str, Any]:
        result = self._call("getWebhookInfo", {})
        if not isinstance(result, dict):
            raise TelegramError("Telegram returned invalid webhook information.")
        return result

    def _call(self, method: str, parameters: dict[str, Any]) -> Any:
        payload = json.dumps(parameters).encode("utf-8")
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + self._token + "/" + method,
            data=payload, method="POST", headers={"Content-Type": "application/json", "User-Agent": "NewsRadar/1.0"},
        )
        # Do not allow a redirect to expose the token or chat ID to another host.
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise TelegramError("Telegram response exceeded the size limit.")
        except urllib.error.HTTPError as exc:
            body = None
            try:
                body = json.loads(exc.read(65536).decode("utf-8"))
            except (ValueError, UnicodeError, OSError):
                pass
            retry_after = _retry_after(body)
            if exc.code == 429:
                raise TelegramError("Telegram rate limit reached.", retry_after=retry_after or 60, code=exc.code) from None
            if exc.code in (400, 401, 403, 404):
                raise TelegramError("Telegram rejected the bot token, chat ID, or chat permissions.", transient=False, code=exc.code) from None
            raise TelegramError(f"Telegram returned HTTP {exc.code}.", transient=exc.code >= 500, code=exc.code) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise TelegramError("Telegram network request failed or timed out.") from None
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise TelegramError("Telegram returned invalid JSON.") from None
        if not isinstance(result, dict) or not result.get("ok"):
            code = result.get("error_code") if isinstance(result, dict) else None
            raise TelegramError("Telegram did not accept the message.", transient=code not in (400, 401, 403, 404), code=code, retry_after=_retry_after(result))
        return result.get("result")

    def send_article(self, article: Mapping[str, Any]) -> dict[str, Any]:
        return self.send_message(format_article(article))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry_after(body: Any) -> int | None:
    if not isinstance(body, dict):
        return None
    parameters = body.get("parameters")
    if not isinstance(parameters, dict):
        return None
    try:
        return max(1, min(int(parameters.get("retry_after")), 86400))
    except (ValueError, TypeError):
        return None
