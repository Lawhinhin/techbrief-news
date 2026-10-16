"""Configuration and current-user encrypted Telegram credentials."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path

TICKERS = [
    {"symbol": "NVDA", "name": "NVIDIA", "aliases": ["Nvidia", "NVIDIA"]},
    {"symbol": "AAPL", "name": "Apple", "aliases": ["Apple", "iPhone", "iPad", "MacBook"]},
    {"symbol": "MSFT", "name": "Microsoft", "aliases": ["Microsoft", "Azure", "Xbox"]},
    {"symbol": "GOOGL", "name": "Alphabet", "aliases": ["Alphabet", "Google", "YouTube", "Waymo", "DeepMind"]},
    {"symbol": "AMZN", "name": "Amazon", "aliases": ["Amazon", "AWS", "Amazon Web Services"]},
    {"symbol": "META", "name": "Meta", "aliases": ["Meta", "Meta Platforms", "Facebook", "Instagram", "WhatsApp", "Zuckerberg"]},
    {"symbol": "AMD", "name": "AMD", "aliases": ["AMD", "Advanced Micro Devices", "Ryzen"]},
    {"symbol": "TSM", "name": "TSMC", "aliases": ["TSMC", "Taiwan Semiconductor"]},
    {"symbol": "AVGO", "name": "Broadcom", "aliases": ["Broadcom", "VMware"]},
    {"symbol": "INTC", "name": "Intel", "aliases": ["Intel"]},
    {"symbol": "ORCL", "name": "Oracle", "aliases": ["Oracle"]},
    {"symbol": "CRM", "name": "Salesforce", "aliases": ["Salesforce"]},
    {"symbol": "ADBE", "name": "Adobe", "aliases": ["Adobe", "Photoshop"]},
    {"symbol": "QCOM", "name": "Qualcomm", "aliases": ["Qualcomm", "Snapdragon"]},
    {"symbol": "ARM", "name": "Arm Holdings", "aliases": ["Arm Holdings", "Arm Ltd"]},
    {"symbol": "PLTR", "name": "Palantir", "aliases": ["Palantir"]},
]

DEFAULTS = {
    "interval_minutes": 5,
    "watchlist": ["NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "AMD", "TSM"],
    "tickers": TICKERS,
    "sources": [],
    "enabled_sources": [],
    "telegram_enabled": False,
    "telegram_max_per_scan": 5,
    "telegram_max_age_hours": 24,
}


def _private_directory(path: Path) -> None:
    """Keep local settings private; Windows uses the account's directory ACLs."""
    if path.is_symlink():
        raise ValueError("The data directory cannot be a symbolic link.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def atomic_json(path: Path, value: dict) -> None:
    """Publish complete JSON; POSIX temporary files are 0600 from creation."""
    _private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _protect(value: bytes, decrypt: bool = False) -> bytes:
    """Windows DPAPI binds credentials to this Windows account."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    incoming = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN prevents unexpected Windows credential dialogs.
    if not method(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise OSError("Windows could not unlock Telegram credentials for this account.")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel.LocalFree(outgoing.pbData)


class ConfigStore:
    def __init__(self, data_dir: Path, sources: list[dict]):
        _private_directory(data_dir)
        self.path = data_dir / "settings.json"
        self.secret_path = data_dir / "credentials.json"
        self.lock = threading.RLock()
        self.defaults = copy.deepcopy(DEFAULTS)
        self.defaults["sources"] = copy.deepcopy(sources)
        self.defaults["enabled_sources"] = [source["id"] for source in sources if source.get("enabled", True)]
        self.config = copy.deepcopy(self.defaults)
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("settings.json must contain a JSON object.")
            self.config.update({key: value for key, value in saved.items() if key in self.defaults})
            # Source definitions and aliases are versioned with the app.
            self.config["sources"] = copy.deepcopy(sources)
            self.config["tickers"] = copy.deepcopy(TICKERS)
        self._apply_sources(self.config)
        self._validate(self.config)
        self.credential_error = None
        try:
            self.secrets = self._read_secrets()
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            # A moved/corrupted credential file must not stop news collection.
            self.secrets = {}
            self.credential_error = "Saved Telegram credentials could not be opened. Reconnect your bot using this account."
        self.secrets.update({key: value for key, value in {
            "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        }.items() if value})
        if self.secrets.get("telegram_bot_token") and self.secrets.get("telegram_chat_id"):
            # Explicit environment credentials enable unattended collection.
            if os.environ.get("TELEGRAM_BOT_TOKEN") and not self.path.exists():
                self.config["telegram_enabled"] = True

    @staticmethod
    def _validate(config: dict) -> None:
        interval = config.get("interval_minutes")
        if type(interval) is not int or not 5 <= interval <= 1440:
            raise ValueError("Collection interval must be a whole number from 5 to 1440 minutes.")
        watchlist = config.get("watchlist")
        known = {stock["symbol"] for stock in TICKERS}
        if not isinstance(watchlist, list) or any(not isinstance(x, str) or x not in known for x in watchlist):
            raise ValueError("Choose watchlist tickers from the supported companies.")
        if len(set(watchlist)) != len(watchlist) or not watchlist:
            raise ValueError("Choose at least one company, without duplicate tickers.")
        if type(config.get("telegram_enabled")) is not bool:
            raise ValueError("Telegram enabled must be true or false.")
        selected = config.get("enabled_sources")
        known_sources = {source["id"] for source in config.get("sources", [])}
        if (not isinstance(selected, list) or any(not isinstance(item, str) or item not in known_sources for item in selected)
                or len(set(selected)) != len(selected) or (known_sources and not selected)):
            raise ValueError("Choose at least one source from the supported feeds, without duplicates.")

    @staticmethod
    def _apply_sources(config: dict) -> None:
        selected = config.get("enabled_sources", [])
        if isinstance(selected, list):
            for source in config.get("sources", []):
                source["enabled"] = source["id"] in selected

    def snapshot(self) -> tuple[dict, dict]:
        with self.lock:
            return copy.deepcopy(self.config), dict(self.secrets)

    def update(self, changes: dict) -> dict:
        allowed = {"interval_minutes", "watchlist", "telegram_enabled", "enabled_sources"}
        if set(changes) - allowed:
            raise ValueError("Unsupported settings field.")
        with self.lock:
            updated = dict(copy.deepcopy(self.config), **changes)
            self._validate(updated)
            self._apply_sources(updated)
            atomic_json(self.path, updated)
            self.config = updated
            return copy.deepcopy(updated)

    def _read_secrets(self) -> dict:
        if not self.secret_path.exists():
            return {}
        import base64
        saved = json.loads(self.secret_path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("Unsupported credential file.")
        if saved.get("format") == "dpapi":
            raw = _protect(base64.b64decode(saved["payload"]), decrypt=True)
            credentials = json.loads(raw.decode("utf-8"))
        elif os.name != "nt" and saved.get("format") == "local":
            credentials = saved.get("credentials", {})
        else:
            raise ValueError("Unsupported credential format; reconnect Telegram in this Windows account.")
        if not isinstance(credentials, dict) or any(not isinstance(credentials.get(key, ""), str) for key in ("telegram_bot_token", "telegram_chat_id")):
            raise ValueError("Unsupported credential values.")
        return {key: credentials.get(key, "") for key in ("telegram_bot_token", "telegram_chat_id")}

    def save_telegram(self, token: str, chat_id: str) -> None:
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Enter a valid bot token from Telegram's @BotFather.")
        if not re.fullmatch(r"-?\d{1,20}", chat_id):
            raise ValueError("Enter the numeric chat ID of your own Telegram chat.")
        import base64
        credentials = {"telegram_bot_token": token, "telegram_chat_id": chat_id}
        with self.lock:
            if os.name == "nt":
                payload = base64.b64encode(_protect(json.dumps(credentials).encode())).decode()
                saved = {"format": "dpapi", "payload": payload}
            else:
                saved = {"format": "local", "credentials": credentials}
            atomic_json(self.secret_path, saved)
            self.secrets = credentials
            self.credential_error = None
