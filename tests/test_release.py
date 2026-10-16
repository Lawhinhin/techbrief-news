"""Check distributable source boundaries without opening local runtime data."""
import ast
import json
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
MODULES = {"__init__.py", "collector.py", "config.py", "source_latency.py", "storage.py", "telegram.py"}
IGNORED = {".git", "__pycache__", ".venv", "data", "backups"}


def sources():
    return [path for path in ROOT.rglob("*") if path.is_file() and not any(part in IGNORED for part in path.relative_to(ROOT).parts)]


class ReleaseBoundaryTests(unittest.TestCase):
    def test_core_contains_only_news_modules(self):
        actual = {path.name for path in (ROOT / "newsradar").glob("*.py")}
        self.assertEqual(actual, MODULES)
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertTrue({name for name in imported if name and name.startswith("newsradar.")} <=
                        {"newsradar.collector", "newsradar.config", "newsradar.storage", "newsradar.telegram"})

    def test_source_distribution_has_no_personal_data_or_captured_datasets(self):
        forbidden_suffixes = {".sqlite", ".sqlite3", ".db", ".parquet", ".zip", ".csv", ".log", ".pem", ".key"}
        forbidden_names = {"settings.json", "credentials.json", "process.json", ".env"}
        for path in sources():
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertNotIn(path.suffix.lower(), forbidden_suffixes)
                self.assertNotIn(path.name, forbidden_names)

    def test_python_dependencies_are_standard_library_or_local_modules(self):
        allowed = set(sys.stdlib_module_names) | {"app", "backup", "newsradar", "tests", "__future__"}
        for path in sources():
            if path.suffix != ".py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    self.assertIn(name, allowed, f"Unexpected dependency in {path.name}: {name}")

    def test_public_sources_are_curated_https_urls_without_credentials(self):
        catalog = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
        identifiers = [item["id"] for item in catalog]
        self.assertTrue(catalog)
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for source in catalog:
            parsed = urlsplit(source["url"])
            self.assertEqual(parsed.scheme, "https")
            self.assertIsNone(parsed.username)
            self.assertIsNone(parsed.password)
            self.assertIn(parsed.hostname, source["allowed_hosts"])
            self.assertNotIn("token", parsed.query.lower())

    def test_launchers_and_entrypoint_have_no_host_specific_runtime_paths(self):
        personal_path = re.compile(r"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|/home/[^/\s]+/|/Users/[^/\s]+/", re.I)
        for path in sources():
            if path.suffix.lower() not in {".cmd", ".bat", ".ps1", ".sh"} and path.name != "app.py":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, personal_path)
            self.assertNotIn("codex-runtimes", text.lower())

    def test_runtime_and_credentials_are_ignored_by_git(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        patterns = {line.strip().lstrip("/") for line in ignore.splitlines() if line.strip() and not line.startswith("#")}
        self.assertTrue({"data/", "backups/", ".env", "__pycache__/"} <= patterns)


if __name__ == "__main__":
    unittest.main()
