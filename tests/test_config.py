import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsradar.config import ConfigStore


class ConfigTests(unittest.TestCase):
    def test_settings_persist_and_invalid_updates_do_not_replace(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            folder = Path(directory)
            store = ConfigStore(folder, [])
            store.update({"interval_minutes": 30, "watchlist": ["AAPL"]})
            with self.assertRaises(ValueError):
                store.update({"interval_minutes": 0})
            restored = ConfigStore(folder, [])
            self.assertEqual(restored.snapshot()[0]["interval_minutes"], 30)
            self.assertEqual(restored.snapshot()[0]["watchlist"], ["AAPL"])

    def test_credentials_roundtrip_and_invalid_token_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            folder = Path(directory)
            store = ConfigStore(folder, [])
            token = "123456789:" + "a" * 35
            with self.assertRaises(ValueError):
                store.save_telegram("wrong", "1234")
            store.save_telegram(token, "1234")
            restored = ConfigStore(folder, [])
            self.assertEqual(restored.snapshot()[1]["telegram_bot_token"], token)
            if os.name == "nt":
                self.assertNotIn(token, store.secret_path.read_text())
                self.assertEqual(json.loads(store.secret_path.read_text())["format"], "dpapi")

    def test_source_selection_survives_restart_and_rejects_unknown_source(self):
        sources = [{"id": "one", "enabled": True}, {"id": "two", "enabled": False}]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            folder = Path(directory)
            store = ConfigStore(folder, sources)
            store.update({"enabled_sources": ["two"]})
            restored = ConfigStore(folder, sources)
            self.assertEqual([row["enabled"] for row in restored.snapshot()[0]["sources"]], [False, True])
            for invalid in ([], ["unknown"], ["two", "two"]):
                with self.assertRaises(ValueError):
                    restored.update({"enabled_sources": invalid})
            self.assertEqual(sources[0]["enabled"], True)

    def test_corrupted_credentials_do_not_prevent_collection_settings_load(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            folder = Path(directory)
            (folder / "credentials.json").write_text("invalid JSON", encoding="utf-8")
            store = ConfigStore(folder, [])
            self.assertEqual(store.snapshot()[1], {})
            self.assertIsNotNone(store.credential_error)
            self.assertEqual(store.snapshot()[0]["interval_minutes"], 5)


if __name__ == "__main__":
    unittest.main()
