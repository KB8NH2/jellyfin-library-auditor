"""Tests for config.py's configuration loading."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import config
from config import ServerCollection
from config import ServerConfig


class ConfigLoadingTests(unittest.TestCase):
    def test_load_server_collection_reads_servers_toml(self) -> None:
        with TemporaryDirectory() as temp_dir:
            servers_path = Path(temp_dir) / "servers.toml"
            servers_path.write_text(
                "\n".join(
                    (
                        'default_server = "primary"',
                        "",
                        "[servers.primary]",
                        'name = "Primary"',
                        'url = "http://primary:8096"',
                        'api_key = "abc123"',
                        "",
                        "[servers.backup]",
                        'name = "Backup"',
                        'url = "http://backup:8096"',
                        'api_key = "def456"',
                    )
                ),
                encoding="utf-8",
            )

            collection = config.load_server_collection(servers_path)

        self.assertIsInstance(collection, ServerCollection)
        self.assertEqual(collection.default_server, "primary")
        self.assertEqual(collection.get_default(), ServerConfig("primary", "Primary", "http://primary:8096", "abc123"))
        self.assertEqual(collection.get("backup").name, "Backup")
        self.assertEqual(
            collection.ordered(),
            (
                ServerConfig("primary", "Primary", "http://primary:8096", "abc123"),
                ServerConfig("backup", "Backup", "http://backup:8096", "def456"),
            ),
        )
        first_server, second_server = collection.first_two()
        self.assertEqual(first_server.key, "primary")
        self.assertEqual(second_server.key, "backup")

    def test_load_tvdb_api_key_reads_tvdb_table(self) -> None:
        with TemporaryDirectory() as temp_dir:
            servers_path = Path(temp_dir) / "servers.toml"
            servers_path.write_text(
                "\n".join(
                    (
                        'default_server = "primary"',
                        "",
                        "[servers.primary]",
                        'name = "Primary"',
                        'url = "http://primary:8096"',
                        'api_key = "abc123"',
                        "",
                        "[tvdb]",
                        'api_key = "tvdb-secret"',
                    )
                ),
                encoding="utf-8",
            )

            api_key = config.load_tvdb_api_key(servers_path)

        self.assertEqual(api_key, "tvdb-secret")

    def test_load_tvdb_api_key_returns_none_when_table_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            servers_path = Path(temp_dir) / "servers.toml"
            servers_path.write_text(
                "\n".join(
                    (
                        'default_server = "primary"',
                        "",
                        "[servers.primary]",
                        'name = "Primary"',
                        'url = "http://primary:8096"',
                        'api_key = "abc123"',
                    )
                ),
                encoding="utf-8",
            )

            api_key = config.load_tvdb_api_key(servers_path)

        self.assertIsNone(api_key)

    def test_load_tvdb_api_key_returns_none_when_key_blank(self) -> None:
        with TemporaryDirectory() as temp_dir:
            servers_path = Path(temp_dir) / "servers.toml"
            servers_path.write_text(
                "\n".join(
                    (
                        'default_server = "primary"',
                        "",
                        "[servers.primary]",
                        'name = "Primary"',
                        'url = "http://primary:8096"',
                        'api_key = "abc123"',
                        "",
                        "[tvdb]",
                        'api_key = "  "',
                    )
                ),
                encoding="utf-8",
            )

            api_key = config.load_tvdb_api_key(servers_path)

        self.assertIsNone(api_key)

    def test_server_collection_first_two_requires_two_servers(self) -> None:
        collection = ServerCollection(
            default_server="primary",
            servers={
                "primary": ServerConfig(
                    key="primary",
                    name="Primary",
                    url="http://primary:8096",
                    api_key="abc123",
                ),
            },
        )

        with self.assertRaises(config.ConfigError):
            collection.first_two()
