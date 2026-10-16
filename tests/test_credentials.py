"""Synthetic local credential-file checks; no real tokens or network calls."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsradar.config import ConfigStore, atomic_json


class PrivateSettingsTests(unittest.TestCase):
    def test_failed_replace_preserves_previous_json_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            atomic_json(path, {"previous": True})
            with patch("newsradar.config.os.replace", side_effect=OSError("Synthetic failure")):
                with self.assertRaises(OSError):
                    atomic_json(path, {"previous": False})
            self.assertEqual(json.loads(path.read_text()), {"previous": True})
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_serialization_failure_does_not_leave_a_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            with self.assertRaises(TypeError):
                atomic_json(path, {"not_json": object()})
            self.assertFalse(path.exists())
            self.assertEqual(list(path.parent.iterdir()), [])

    def test_unknown_preferences_are_not_loaded_or_accepted(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            folder = Path(directory)
            (folder / "settings.json").write_text(json.dumps({"unsupported_feature": True}), encoding="utf-8")
            store = ConfigStore(folder, [])
            self.assertNotIn("unsupported_feature", store.snapshot()[0])
            with self.assertRaises(ValueError):
                store.update({"unsupported_feature": True})
            self.assertFalse(store.snapshot()[0]["telegram_enabled"])

    @unittest.skipIf(os.name == "nt", "POSIX permissions; Windows uses DPAPI and account ACLs.")
    def test_secret_file_and_parent_are_private_before_first_json_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "data"
            path = folder / "credentials.json"
            real_dump = json.dump

            def check_then_dump(value, handle, **kwargs):
                self.assertEqual(stat.S_IMODE(os.stat(handle.fileno()).st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
                self.assertFalse(path.exists())
                return real_dump(value, handle, **kwargs)

            with patch("newsradar.config.json.dump", side_effect=check_then_dump):
                atomic_json(path, {"synthetic": "example"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX permissions; Windows uses DPAPI and account ACLs.")
    def test_existing_data_directory_permissions_are_tightened(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            folder = Path(directory) / "data"
            folder.mkdir(mode=0o755)
            folder.chmod(0o755)
            store = ConfigStore(folder, [])
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
            store.save_telegram("123456789:" + "a" * 35, "1234")
            self.assertEqual(stat.S_IMODE(store.secret_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
