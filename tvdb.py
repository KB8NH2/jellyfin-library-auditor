"""TheTVDB v4 REST API client for episode ordering lookups.

This module is responsible for talking to TheTVDB API and converting API
responses into normalized episode records. It does not contain audit logic,
reporting, or any assumptions about how episode data will be evaluated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import logging
from pathlib import Path
from typing import Any
from typing import Literal

import requests


LOGGER = logging.getLogger("tvdb")
TVDB_BASE_URL = "https://api4.thetvdb.com/v4"
LOGIN_ENDPOINT = "/login"
SERIES_EPISODES_ENDPOINT_TEMPLATE = "/series/{series_id}/episodes/{season_type}"
DEFAULT_TIMEOUT_SECONDS = 30.0
# Safety cap on pagination so a misbehaving/looping response can't hang a run;
# no real series has anywhere near this many pages of episodes.
MAX_EPISODE_PAGES = 50

SeasonType = Literal["official", "dvd"]

DEFAULT_TVDB_CACHE_PATH = Path("tvdb_cache.json")
DEFAULT_CACHE_TTL = timedelta(days=7)
# Bump whenever TvdbEpisode's cached shape changes (e.g. a new field is
# added) so an existing on-disk cache from before the change is discarded
# and refetched instead of silently loading with the new field missing.
CACHE_SCHEMA_VERSION = 2


class TvdbError(RuntimeError):
    """Base exception for TheTVDB client errors."""


class TvdbConfigurationError(TvdbError):
    """Raised when the TheTVDB client configuration is invalid."""


class TvdbRequestError(TvdbError):
    """Raised when a TheTVDB HTTP request fails."""


class TvdbResponseError(TvdbError):
    """Raised when TheTVDB returns invalid or unexpected data."""


@dataclass(frozen=True, slots=True)
class TvdbEpisode:
    """Represents one normalized TheTVDB episode record."""

    id: int
    season_number: int
    episode_number: int
    name: str
    overview: str | None
    runtime_minutes: int | None
    image_url: str | None = None


class TvdbEpisodeCache:
    """Disk-backed cache of TheTVDB episode-ordering lookups.

    Fetching every series' aired and DVD episode lists on every audit run is
    the dominant cost of ``--check-episode-order`` (two HTTP round-trips per
    series), even though that data almost never changes for anything except a
    currently-airing season. This persists each (series, ordering) lookup to
    a JSON file with a fetch timestamp, so subsequent runs within the TTL
    window can skip the network entirely.
    """

    def __init__(
        self,
        path: Path = DEFAULT_TVDB_CACHE_PATH,
        *,
        ttl: timedelta = DEFAULT_CACHE_TTL,
        force_refresh: bool = False,
    ) -> None:
        """Load a TheTVDB episode cache from disk, if present.

        Args:
            path: Cache file location.
            ttl: How long a cached lookup stays valid before a fresh fetch is
                required.
            force_refresh: When ``True``, every read is treated as a miss
                (a fresh fetch is always performed), but a successful fetch
                still updates the cache on disk.
        """
        self._path = path
        self._ttl = ttl
        self._force_refresh = force_refresh
        self._entries: dict[tuple[str, str], tuple[datetime, tuple[TvdbEpisode, ...]]] = (
            self._load()
        )

    def get(self, series_id: str, season_type: str) -> tuple[TvdbEpisode, ...] | None:
        """Return a cached episode list, or ``None`` on a miss or forced refresh.

        Args:
            series_id: TheTVDB series identifier.
            season_type: Which episode ordering was requested.

        Returns:
            The cached episodes, or ``None`` when there is no fresh entry.
        """
        if self._force_refresh:
            return None

        entry = self._entries.get((series_id, season_type))
        if entry is None:
            return None

        fetched_at, episodes = entry
        if datetime.now(timezone.utc) - fetched_at > self._ttl:
            return None

        return episodes

    def set(
        self,
        series_id: str,
        season_type: str,
        episodes: tuple[TvdbEpisode, ...],
    ) -> None:
        """Store a freshly-fetched episode list and persist it immediately.

        Args:
            series_id: TheTVDB series identifier.
            season_type: Which episode ordering was fetched.
            episodes: The episodes to cache.
        """
        self._entries[(series_id, season_type)] = (datetime.now(timezone.utc), episodes)
        self._save()

    def _load(self) -> dict[tuple[str, str], tuple[datetime, tuple[TvdbEpisode, ...]]]:
        """Return cache entries loaded from disk, or an empty cache on any problem."""
        if not self._path.is_file():
            return {}

        try:
            raw_document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            LOGGER.warning("Ignoring unreadable TheTVDB cache at %s: %s", self._path, error)
            return {}

        if not isinstance(raw_document, dict):
            LOGGER.warning("Ignoring malformed TheTVDB cache at %s.", self._path)
            return {}

        if raw_document.get("version") != CACHE_SCHEMA_VERSION:
            LOGGER.info(
                "Ignoring TheTVDB cache at %s from an older schema version; "
                "it will be refetched.",
                self._path,
            )
            return {}

        raw_series = raw_document.get("series")
        if not isinstance(raw_series, dict):
            return {}

        entries: dict[tuple[str, str], tuple[datetime, tuple[TvdbEpisode, ...]]] = {}
        for series_id, orderings in raw_series.items():
            if not isinstance(orderings, dict):
                continue
            for season_type, entry_document in orderings.items():
                entry = self._entry_from_json(entry_document)
                if entry is not None:
                    entries[(series_id, season_type)] = entry

        return entries

    @classmethod
    def _entry_from_json(
        cls, entry_document: Any
    ) -> tuple[datetime, tuple[TvdbEpisode, ...]] | None:
        """Return one parsed cache entry, or ``None`` when it's malformed."""
        if not isinstance(entry_document, dict):
            return None

        fetched_at_text = entry_document.get("fetched_at")
        raw_episodes = entry_document.get("episodes")
        if not isinstance(fetched_at_text, str) or not isinstance(raw_episodes, list):
            return None

        try:
            fetched_at = datetime.fromisoformat(fetched_at_text)
        except ValueError:
            return None

        episodes: list[TvdbEpisode] = []
        for raw_episode in raw_episodes:
            episode = cls._episode_from_cache_json(raw_episode)
            if episode is not None:
                episodes.append(episode)

        return fetched_at, tuple(episodes)

    @staticmethod
    def _episode_from_cache_json(raw_episode: Any) -> TvdbEpisode | None:
        """Return one cached episode parsed from its stored JSON shape."""
        if not isinstance(raw_episode, dict):
            return None
        try:
            return TvdbEpisode(
                id=raw_episode["id"],
                season_number=raw_episode["season_number"],
                episode_number=raw_episode["episode_number"],
                name=raw_episode["name"],
                overview=raw_episode.get("overview"),
                runtime_minutes=raw_episode.get("runtime_minutes"),
                image_url=raw_episode.get("image_url"),
            )
        except (KeyError, TypeError):
            return None

    @staticmethod
    def _episode_to_cache_json(episode: TvdbEpisode) -> dict[str, Any]:
        """Return one episode's stored JSON shape."""
        return {
            "id": episode.id,
            "season_number": episode.season_number,
            "episode_number": episode.episode_number,
            "name": episode.name,
            "overview": episode.overview,
            "runtime_minutes": episode.runtime_minutes,
            "image_url": episode.image_url,
        }

    def _save(self) -> None:
        """Persist the current cache contents to disk."""
        series: dict[str, dict[str, Any]] = {}
        for (series_id, season_type), (fetched_at, episodes) in self._entries.items():
            series.setdefault(series_id, {})[season_type] = {
                "fetched_at": fetched_at.isoformat(),
                "episodes": [self._episode_to_cache_json(episode) for episode in episodes],
            }

        document = {"version": CACHE_SCHEMA_VERSION, "series": series}

        try:
            self._path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        except OSError as error:
            LOGGER.warning("Failed to write TheTVDB cache to %s: %s", self._path, error)


class TvdbClient:
    """HTTP client that reads TheTVDB episode ordering data."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cache: TvdbEpisodeCache | None = None,
    ) -> None:
        """Initialize the TheTVDB client from an API key."""
        if not api_key or not api_key.strip():
            raise TvdbConfigurationError(
                "[tvdb] api_key in servers.toml must be a non-empty API key."
            )

        self._api_key = api_key.strip()
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._timeout = timeout_seconds
        self._token: str | None = None
        self._cache = cache

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> TvdbClient:
        """Return the client for context manager usage."""
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Close the HTTP session when leaving a context manager."""
        self.close()

    def get_series_episodes(
        self,
        series_id: str,
        season_type: SeasonType,
    ) -> tuple[TvdbEpisode, ...]:
        """Return every episode of one series in the requested ordering.

        Args:
            series_id: TheTVDB series identifier (as reported by Jellyfin's
                ``ProviderIds["Tvdb"]``).
            season_type: Which episode ordering to fetch - ``"official"`` for
                aired order, ``"dvd"`` for DVD order.

        Returns:
            Every episode TheTVDB reports for the series in that ordering.

        Raises:
            TvdbRequestError: If the HTTP request fails.
            TvdbResponseError: If the response body is missing expected data.
        """
        if self._cache is not None:
            cached_episodes = self._cache.get(series_id, season_type)
            if cached_episodes is not None:
                return cached_episodes

        self._ensure_token()

        episodes: list[TvdbEpisode] = []
        page = 0

        while page < MAX_EPISODE_PAGES:
            payload = self._request(
                SERIES_EPISODES_ENDPOINT_TEMPLATE.format(
                    series_id=series_id,
                    season_type=season_type,
                ),
                params={"page": page},
            )
            data = self._get_required_dict(payload, "data", "episodes response")
            raw_episodes = self._get_optional_list(data, "episodes", "episodes response")
            if not raw_episodes:
                break

            for raw_episode in raw_episodes:
                episode = self._episode_from_json(raw_episode)
                if episode is not None:
                    episodes.append(episode)

            page += 1

        result = tuple(episodes)
        if self._cache is not None:
            self._cache.set(series_id, season_type, result)

        return result

    def _ensure_token(self) -> None:
        """Log in and cache a bearer token, if not already cached."""
        if self._token is not None:
            return

        payload = self._request_payload(
            "POST",
            LOGIN_ENDPOINT,
            json_body={"apikey": self._api_key},
        )
        if not isinstance(payload, dict):
            raise TvdbResponseError(
                f"TheTVDB returned an unexpected JSON type for POST {self._build_url(LOGIN_ENDPOINT)}."
            )

        data = self._get_required_dict(payload, "data", "login response")
        token = self._get_required_str(data, "token", "login response")
        self._token = token
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    def _episode_from_json(self, episode_data: Mapping[str, Any]) -> TvdbEpisode | None:
        """Convert one TheTVDB episode object into a normalized model."""
        season_number = self._get_optional_int(episode_data, "seasonNumber")
        episode_number = self._get_optional_int(episode_data, "number")
        if season_number is None or episode_number is None:
            return None

        return TvdbEpisode(
            id=self._get_required_int(episode_data, "id", "episode object"),
            season_number=season_number,
            episode_number=episode_number,
            name=self._get_optional_str(episode_data, "name") or "",
            overview=self._get_optional_str(episode_data, "overview"),
            runtime_minutes=self._get_optional_int(episode_data, "runtime"),
            image_url=self._get_optional_str(episode_data, "image"),
        )

    def download_image(self, url: str) -> tuple[bytes, str]:
        """Download one TheTVDB-hosted image and return its bytes and content type.

        Args:
            url: An absolute image URL, e.g. from ``TvdbEpisode.image_url``.

        Returns:
            A ``(image_bytes, content_type)`` tuple.

        Raises:
            TvdbRequestError: If the HTTP request fails.
        """
        LOGGER.debug("TheTVDB GET %s (image)", url)
        try:
            response = self._session.request("GET", url, timeout=self._timeout)
            response.raise_for_status()
        except requests.RequestException as error:
            raise self._wrap_request_error("GET", url, error) from error

        content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        return response.content, content_type

    def _request(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform a TheTVDB REST request and decode the JSON response."""
        payload = self._request_payload("GET", path, params=params)
        if not isinstance(payload, dict):
            raise TvdbResponseError(
                f"TheTVDB returned an unexpected JSON type for GET {self._build_url(path)}."
            )

        return payload

    def _request_payload(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Perform a TheTVDB REST request and decode any JSON response body."""
        url = self._build_url(path)

        LOGGER.debug("TheTVDB %s %s", method, url)
        try:
            response = self._session.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise self._wrap_request_error(method, url, error) from error

        if not response.content:
            return None

        try:
            return response.json()
        except ValueError as error:
            raise TvdbResponseError(
                f"TheTVDB returned invalid JSON for {method} {url}."
            ) from error

    @classmethod
    def _wrap_request_error(
        cls, method: str, url: str, error: requests.RequestException
    ) -> TvdbRequestError:
        """Return a TvdbRequestError describing a failed HTTP request."""
        if isinstance(error, requests.HTTPError):
            status_code = error.response.status_code if error.response is not None else "?"
            error_detail = cls._error_response_detail(error.response)
            detail_suffix = f": {error_detail}" if error_detail else ""
            return TvdbRequestError(
                f"TheTVDB request failed with status {status_code}: {method} {url}{detail_suffix}"
            )
        return TvdbRequestError(f"TheTVDB request failed: {method} {url}: {error}")

    @staticmethod
    def _error_response_detail(response: requests.Response | None) -> str:
        """Return a truncated, single-line error body for a failed response."""
        if response is None or not response.content:
            return ""

        text = response.text.strip()
        if not text:
            return ""

        single_line = " ".join(text.split())
        max_length = 500
        if len(single_line) > max_length:
            single_line = f"{single_line[:max_length]}..."
        return single_line

    def _build_url(self, path: str) -> str:
        """Build an absolute TheTVDB API URL."""
        return f"{TVDB_BASE_URL}/{path.lstrip('/')}"

    @staticmethod
    def _get_required_str(data: Mapping[str, Any], key: str, context: str) -> str:
        """Return a required non-empty string field from a JSON object."""
        value = data.get(key)
        if not isinstance(value, str):
            raise TvdbResponseError(f"Expected {key!r} to be a string in {context}.")

        normalized_value = value.strip()
        if not normalized_value:
            raise TvdbResponseError(f"Expected {key!r} to be a non-empty string in {context}.")

        return normalized_value

    @staticmethod
    def _get_optional_str(data: Mapping[str, Any], key: str) -> str | None:
        """Return an optional string field from a JSON object."""
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise TvdbResponseError(f"Expected {key!r} to be a string.")

        normalized_value = value.strip()
        return normalized_value or None

    @staticmethod
    def _get_required_int(data: Mapping[str, Any], key: str, context: str) -> int:
        """Return a required integer field from a JSON object."""
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TvdbResponseError(f"Expected {key!r} to be an integer in {context}.")

        return value

    @staticmethod
    def _get_optional_int(data: Mapping[str, Any], key: str) -> int | None:
        """Return an optional integer field from a JSON object."""
        value = data.get(key)
        if value is None:
            return None
        if isinstance(value, bool):
            raise TvdbResponseError(f"Expected {key!r} to be an integer.")
        if isinstance(value, int):
            return value

        raise TvdbResponseError(f"Expected {key!r} to be an integer.")

    @staticmethod
    def _get_required_dict(data: Mapping[str, Any], key: str, context: str) -> dict[str, Any]:
        """Return a required object field from a JSON object."""
        value = data.get(key)
        if not isinstance(value, Mapping):
            raise TvdbResponseError(f"Expected {key!r} to be an object in {context}.")

        return dict(value)

    @staticmethod
    def _get_optional_list(data: Mapping[str, Any], key: str, context: str) -> list[Any]:
        """Return an optional list field from a JSON object."""
        value = data.get(key)
        if value is None:
            return []
        if not isinstance(value, list):
            raise TvdbResponseError(f"Expected {key!r} to be a list in {context}.")

        return value


__all__ = [
    "SeasonType",
    "TvdbClient",
    "TvdbConfigurationError",
    "TvdbEpisode",
    "TvdbEpisodeCache",
    "TvdbError",
    "TvdbRequestError",
    "TvdbResponseError",
]
