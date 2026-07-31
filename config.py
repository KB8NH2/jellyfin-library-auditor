"""Centralized application configuration for Jellyfin Library Auditor.

This module keeps configuration in one place so the rest of the project can
depend on typed settings instead of scattered environment variable lookups and
hardcoded defaults.

The structure is intentionally grouped by subsystem to make future audit areas
easy to add, such as subtitle, poster, NFO, codec, HDR, chapter, and duplicate
checks.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


JELLYFIN_API_KEY_ENV_VAR = "JELLYFIN_API_KEY"
JELLYFIN_SERVER_URL_ENV_VAR = "JELLYFIN_SERVER_URL"
REPORT_MEDIA_PATH_PREFIX_ENV_VAR = "REPORT_MEDIA_PATH_PREFIX"
MOVIES_CSV_FILENAME_ENV_VAR = "MOVIES_CSV_FILENAME"
TV_CSV_FILENAME_ENV_VAR = "TV_CSV_FILENAME"
AUDIT_CSV_FILENAME_ENV_VAR = "AUDIT_CSV_FILENAME"
AUDIT_HTML_FILENAME_ENV_VAR = "AUDIT_HTML_FILENAME"
ENABLE_MOVIES_ENV_VAR = "ENABLE_MOVIES"
ENABLE_TV_ENV_VAR = "ENABLE_TV"
HTTP_TIMEOUT_SECONDS_ENV_VAR = "HTTP_TIMEOUT_SECONDS"
JELLYFIN_PAGE_SIZE_ENV_VAR = "JELLYFIN_PAGE_SIZE"
ENGLISH_LANGUAGE_CODES_ENV_VAR = "ENGLISH_LANGUAGE_CODES"

DEFAULT_JELLYFIN_SERVER_URL = "http://jellyfin:8096"
DEFAULT_REPORT_MEDIA_PATH_PREFIX = ""
DEFAULT_MOVIES_CSV_FILENAME = "movies_report.csv"
DEFAULT_TV_CSV_FILENAME = "tv_report.csv"
DEFAULT_AUDIT_CSV_FILENAME = "audit_report.csv"
DEFAULT_AUDIT_HTML_FILENAME = "audit_report.html"
DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
DEFAULT_JELLYFIN_PAGE_SIZE = 200

# A blank language code is common when media metadata is missing or incomplete.
REQUIRED_ENGLISH_LANGUAGE_CODES = ("en", "eng", "")
DEFAULT_ENGLISH_LANGUAGE_CODES = ("en", "eng", "")

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(ValueError):
    """Raised when a configuration value is invalid."""


@dataclass(frozen=True, slots=True)
class JellyfinConfig:
    """Settings for Jellyfin API access and pagination."""

    api_key: str | None
    server_url: str
    timeout_seconds: float
    page_size: int


@dataclass(frozen=True, slots=True)
class CsvOutputConfig:
    """Output filenames for CSV reports."""

    movies: Path
    tv: Path


@dataclass(frozen=True, slots=True)
class ReportOutputConfig:
    """Output filenames for generated audit reports."""

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
        """Return the enabled library types in display-friendly order."""
        enabled: list[str] = []

        if self.enable_movies:
            enabled.append("movies")
        if self.enable_tv:
            enabled.append("tv")

        return tuple(enabled)


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete application configuration grouped by subsystem."""

    jellyfin: JellyfinConfig
    reporting: ReportingConfig
    processing: ProcessingConfig


def _read_string(name: str, default: str) -> str:
    """Read a string environment variable with whitespace trimmed."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _read_optional_string(name: str) -> str | None:
    """Read an optional string environment variable with whitespace trimmed."""
    value = os.getenv(name)
    if value is None:
        return None

    stripped_value = value.strip()
    return stripped_value or None


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


def _read_positive_int(name: str, default: int) -> int:
    """Read a positive integer environment variable."""
    value = os.getenv(name)
    if value is None:
        return default

    try:
        parsed_value = int(value.strip())
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer; received {value!r}.") from error

    if parsed_value <= 0:
        raise ConfigError(f"{name} must be greater than zero; received {value!r}.")

    return parsed_value


def _read_positive_float(name: str, default: float) -> float:
    """Read a positive floating-point environment variable."""
    value = os.getenv(name)
    if value is None:
        return default

    try:
        parsed_value = float(value.strip())
    except ValueError as error:
        raise ConfigError(
            f"{name} must be a number; received {value!r}."
        ) from error

    if parsed_value <= 0:
        raise ConfigError(f"{name} must be greater than zero; received {value!r}.")

    return parsed_value


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
    """Build application configuration from environment variables."""
    jellyfin_config = JellyfinConfig(
        api_key=_read_optional_string(JELLYFIN_API_KEY_ENV_VAR),
        server_url=_read_string(
            JELLYFIN_SERVER_URL_ENV_VAR,
            DEFAULT_JELLYFIN_SERVER_URL,
        ),
        timeout_seconds=_read_positive_float(
            HTTP_TIMEOUT_SECONDS_ENV_VAR,
            DEFAULT_HTTP_TIMEOUT_SECONDS,
        ),
        page_size=_read_positive_int(
            JELLYFIN_PAGE_SIZE_ENV_VAR,
            DEFAULT_JELLYFIN_PAGE_SIZE,
        ),
    )

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
        english_language_codes=_read_language_codes(
            ENGLISH_LANGUAGE_CODES_ENV_VAR,
        ),
    )

    processing_config = ProcessingConfig(
        enable_movies=_read_bool(ENABLE_MOVIES_ENV_VAR, default=True),
        enable_tv=_read_bool(ENABLE_TV_ENV_VAR, default=True),
    )

    return AppConfig(
        jellyfin=jellyfin_config,
        reporting=reporting_config,
        processing=processing_config,
    )


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Return the cached application configuration."""
    return load_config()


def clear_config_cache() -> None:
    """Clear the cached configuration, mainly for tests or environment reloads."""
    get_config.cache_clear()
