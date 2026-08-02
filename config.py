"""Centralized application configuration for Jellyfin Library Auditor.

This module keeps configuration in one place so the rest of the project can
depend on typed settings instead of scattered environment variable lookups and
hardcoded defaults.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11+ provides tomllib.
    import tomli as tomllib  # type: ignore[no-redef]


REPORT_MEDIA_PATH_PREFIX_ENV_VAR = "REPORT_MEDIA_PATH_PREFIX"
MOVIES_CSV_FILENAME_ENV_VAR = "MOVIES_CSV_FILENAME"
TV_CSV_FILENAME_ENV_VAR = "TV_CSV_FILENAME"
AUDIT_CSV_FILENAME_ENV_VAR = "AUDIT_CSV_FILENAME"
AUDIT_HTML_FILENAME_ENV_VAR = "AUDIT_HTML_FILENAME"
ENABLE_MOVIES_ENV_VAR = "ENABLE_MOVIES"
ENABLE_TV_ENV_VAR = "ENABLE_TV"
ENGLISH_LANGUAGE_CODES_ENV_VAR = "ENGLISH_LANGUAGE_CODES"

DEFAULT_REPORT_MEDIA_PATH_PREFIX = ""
DEFAULT_MOVIES_CSV_FILENAME = "movies_report.csv"
DEFAULT_TV_CSV_FILENAME = "tv_report.csv"
DEFAULT_AUDIT_CSV_FILENAME = "audit_report.csv"
DEFAULT_AUDIT_HTML_FILENAME = "audit_results"
DEFAULT_SERVERS_TOML = "servers.toml"

REQUIRED_ENGLISH_LANGUAGE_CODES = ("en", "eng", "")
DEFAULT_ENGLISH_LANGUAGE_CODES = ("en", "eng", "")

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(ValueError):
    """Raised when a configuration value is invalid."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Connection settings for one Jellyfin server."""

    key: str
    name: str
    url: str
    api_key: str

    def __post_init__(self) -> None:
        """Normalize server configuration values."""
        object.__setattr__(self, "key", self.key.strip())
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "url", self.url.strip().rstrip("/"))
        object.__setattr__(self, "api_key", self.api_key.strip())


@dataclass(frozen=True, slots=True)
class ServerCollection:
    """Represents all configured Jellyfin servers."""

    default_server: str
    servers: dict[str, ServerConfig]

    def __post_init__(self) -> None:
        """Validate server collection consistency."""
        object.__setattr__(self, "default_server", self.default_server.strip())
        normalized_servers = {
            key.strip(): value
            for key, value in self.servers.items()
        }
        object.__setattr__(self, "servers", normalized_servers)

        if self.default_server not in normalized_servers:
            available = ", ".join(sorted(normalized_servers)) or "none"
            raise ConfigError(
                f"default_server {self.default_server!r} was not found in servers.toml. "
                f"Available servers: {available}."
            )

    def get_default(self) -> ServerConfig:
        """Return the configured default server."""
        return self.servers[self.default_server]

    def get(self, server_key: str) -> ServerConfig:
        """Return one configured server by key."""
        normalized_key = server_key.strip()
        try:
            return self.servers[normalized_key]
        except KeyError as error:
            available = ", ".join(sorted(self.servers)) or "none"
            raise ConfigError(
                f"Unknown server {server_key!r}. Available servers: {available}."
            ) from error

    def ordered(self) -> tuple[ServerConfig, ...]:
        """Return configured servers in TOML file order."""
        return tuple(self.servers.values())

    def first_two(self) -> tuple[ServerConfig, ServerConfig]:
        """Return the first two configured servers in file order."""
        ordered_servers = self.ordered()
        if len(ordered_servers) < 2:
            raise ConfigError(
                "At least two configured servers are required to use --compare without specifying server names."
            )
        return ordered_servers[0], ordered_servers[1]


@dataclass(frozen=True, slots=True)
class CsvOutputConfig:
    """Output filenames for CSV reports."""

    movies: Path
    tv: Path


@dataclass(frozen=True, slots=True)
class ReportOutputConfig:
    """Output locations for generated audit reports."""

    audit_csv: Path
    audit_html: Path


@dataclass(frozen=True, slots=True)
class ReportingConfig:
    """Settings that shape report formatting and export behavior."""

    media_path_prefix: str
    csv_output: CsvOutputConfig
    output: ReportOutputConfig
    english_language_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProcessingConfig:
    """Feature flags for top-level library processing."""

    enable_movies: bool
    enable_tv: bool

    def enabled_library_types(self) -> tuple[str, ...]:
        """Return enabled library types in display-friendly order."""
        enabled: list[str] = []
        if self.enable_movies:
            enabled.append("movies")
        if self.enable_tv:
            enabled.append("tv")
        return tuple(enabled)


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete application configuration grouped by subsystem."""

    reporting: ReportingConfig
    processing: ProcessingConfig
    servers: ServerCollection


def _read_string(name: str, default: str) -> str:
    """Read a string environment variable with whitespace trimmed."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _read_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable from common truthy and falsy values."""
    value = os.getenv(name)
    if value is None:
        return default

    normalized_value = value.strip().lower()
    if normalized_value in TRUE_VALUES:
        return True
    if normalized_value in FALSE_VALUES:
        return False

    raise ConfigError(
        f"{name} must be one of {sorted(TRUE_VALUES | FALSE_VALUES)}; "
        f"received {value!r}."
    )


def _read_path(name: str, default: str | Path) -> Path:
    """Read a filesystem path from an environment variable."""
    value = os.getenv(name)
    if value is None:
        return Path(default)
    return Path(value.strip())


def _read_language_codes(name: str) -> tuple[str, ...]:
    """Read, normalize, and complete the configured English language codes."""
    value = os.getenv(name)
    if value is None:
        return DEFAULT_ENGLISH_LANGUAGE_CODES

    raw_codes = (segment.strip() for segment in value.split(","))
    return _normalize_language_codes(raw_codes)


def _normalize_language_codes(codes: Iterable[str]) -> tuple[str, ...]:
    """Normalize language code values and ensure required English codes exist."""
    normalized_codes: list[str] = []
    seen: set[str] = set()

    for code in codes:
        normalized_code = str(code).strip().lower()
        if normalized_code in seen:
            continue
        normalized_codes.append(normalized_code)
        seen.add(normalized_code)

    for required_code in REQUIRED_ENGLISH_LANGUAGE_CODES:
        if required_code in seen:
            continue
        normalized_codes.append(required_code)
        seen.add(required_code)

    return tuple(normalized_codes)


def load_config() -> AppConfig:
    """Build application configuration from configuration sources."""
    reporting_config = ReportingConfig(
        media_path_prefix=_read_string(
            REPORT_MEDIA_PATH_PREFIX_ENV_VAR,
            DEFAULT_REPORT_MEDIA_PATH_PREFIX,
        ),
        csv_output=CsvOutputConfig(
            movies=_read_path(
                MOVIES_CSV_FILENAME_ENV_VAR,
                DEFAULT_MOVIES_CSV_FILENAME,
            ),
            tv=_read_path(
                TV_CSV_FILENAME_ENV_VAR,
                DEFAULT_TV_CSV_FILENAME,
            ),
        ),
        output=ReportOutputConfig(
            audit_csv=_read_path(
                AUDIT_CSV_FILENAME_ENV_VAR,
                DEFAULT_AUDIT_CSV_FILENAME,
            ),
            audit_html=_read_path(
                AUDIT_HTML_FILENAME_ENV_VAR,
                DEFAULT_AUDIT_HTML_FILENAME,
            ),
        ),
        english_language_codes=_read_language_codes(ENGLISH_LANGUAGE_CODES_ENV_VAR),
    )

    processing_config = ProcessingConfig(
        enable_movies=_read_bool(ENABLE_MOVIES_ENV_VAR, default=True),
        enable_tv=_read_bool(ENABLE_TV_ENV_VAR, default=True),
    )

    return AppConfig(
        reporting=reporting_config,
        processing=processing_config,
        servers=load_server_collection(),
    )


def load_server_collection(path: Path | None = None) -> ServerCollection:
    """Load the configured Jellyfin servers from TOML."""
    servers_path = path or _default_servers_path()
    if not servers_path.is_file():
        raise ConfigError(
            f"Server configuration file was not found at {servers_path}."
        )

    try:
        payload = tomllib.loads(servers_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid TOML in {servers_path}: {error}") from error

    if not isinstance(payload, dict):
        raise ConfigError(f"{servers_path} must contain a TOML table.")

    default_server = payload.get("default_server")
    if not isinstance(default_server, str) or not default_server.strip():
        raise ConfigError(f"{servers_path} must define a non-empty default_server.")

    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, dict) or not raw_servers:
        raise ConfigError(f"{servers_path} must define at least one [servers.*] table.")

    servers: dict[str, ServerConfig] = {}
    for key, raw_server in raw_servers.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError(f"{servers_path} contains an invalid server key.")
        if not isinstance(raw_server, dict):
            raise ConfigError(f"[servers.{key}] must be a TOML table.")
        servers[key.strip()] = ServerConfig(
            key=key,
            name=_required_toml_string(raw_server, "name", f"[servers.{key}]"),
            url=_required_toml_string(raw_server, "url", f"[servers.{key}]"),
            api_key=_required_toml_string(raw_server, "api_key", f"[servers.{key}]"),
        )

    return ServerCollection(
        default_server=default_server,
        servers=servers,
    )


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Return the cached application configuration."""
    return load_config()


@lru_cache(maxsize=1)
def get_server_collection() -> ServerCollection:
    """Return the cached server configuration collection."""
    return load_server_collection()


def clear_config_cache() -> None:
    """Clear cached configuration, mainly for tests or reloads."""
    get_config.cache_clear()
    get_server_collection.cache_clear()


def _default_servers_path() -> Path:
    """Return the default path to servers.toml."""
    return Path(__file__).resolve().parent / DEFAULT_SERVERS_TOML


def _required_toml_string(
    data: dict[str, object],
    key: str,
    context: str,
) -> str:
    """Return a required non-empty string from parsed TOML data."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must define a non-empty {key!r} value.")
    return value.strip()
