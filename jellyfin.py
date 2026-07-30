"""Jellyfin REST API client for normalized media retrieval.

This module is responsible for talking to the Jellyfin API and converting API
responses into application data models. It does not contain audit logic,
reporting, or any assumptions about how media data will be evaluated.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests

from config import get_config
from models import AudioTrack
from models import MediaItem
from models import MediaLibrary
from models import SubtitleTrack
from models import VideoTrack


LIBRARIES_ENDPOINT = "/Library/MediaFolders"
ITEMS_ENDPOINT = "/Items"
PING_ENDPOINT = "/System/Info/Public"
ITEM_FIELDS = ",".join(
    [
        "Path",
        "ProductionYear",
        "RunTimeTicks",
        "ImageTags",
        "MediaStreams",
    ]
)
ITEM_TYPES = "Movie,Episode"
VIDEO_STREAM_TYPE = "video"
AUDIO_STREAM_TYPE = "audio"
SUBTITLE_STREAM_TYPE = "subtitle"
RequestParamValue = str | bytes | int | float | list[str] | tuple[str, ...] | None


class JellyfinError(RuntimeError):
    """Base exception for Jellyfin client errors."""


class JellyfinConfigurationError(JellyfinError):
    """Raised when the Jellyfin client configuration is invalid."""


class JellyfinRequestError(JellyfinError):
    """Raised when a Jellyfin HTTP request fails."""


class JellyfinResponseError(JellyfinError):
    """Raised when Jellyfin returns invalid or unexpected data."""


class JellyfinClient:
    """HTTP client that reads Jellyfin data and returns normalized models."""

    def __init__(self) -> None:
        """Initialize the Jellyfin client from application configuration."""
        self._config = get_config()
        self._session = requests.Session()

        api_key = self._config.jellyfin.api_key
        if not api_key:
            raise JellyfinConfigurationError(
                "JELLYFIN_API_KEY must be configured before using JellyfinClient."
            )

        self._server_url = self._config.jellyfin.server_url.rstrip("/")
        self._timeout = self._config.jellyfin.timeout_seconds
        self._page_size = self._config.jellyfin.page_size

        self._session.headers.update(
            {
                "Accept": "application/json",
                "X-Emby-Token": api_key,
            }
        )

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def ping(self) -> bool:
        """Return whether the Jellyfin server is reachable."""
        try:
            self._request("GET", PING_ENDPOINT)
        except JellyfinError:
            return False

        return True

    def get_libraries(self) -> list[MediaLibrary]:
        """Return all available Jellyfin media libraries.

        Returns:
            A list of normalized media library objects.
        """
        payload = self._request("GET", LIBRARIES_ENDPOINT)
        raw_libraries = self._get_required_list(payload, "Items", "library response")

        libraries: list[MediaLibrary] = []
        for raw_library in raw_libraries:
            libraries.append(self._library_from_json(raw_library))

        return libraries

    def get_library_items(self, library_id: str) -> list[MediaItem]:
        """Return every movie or episode contained in a Jellyfin library.

        Args:
            library_id: Jellyfin library identifier.

        Returns:
            A list of normalized media items.
        """
        library = self._get_library_by_id(library_id)
        return self._get_library_items_for_library(library)

    def _get_library_items_for_library(self, library: MediaLibrary) -> list[MediaItem]:
        """Return every movie or episode contained in one normalized library.

        Args:
            library: The normalized media library to query.

        Returns:
            A list of normalized media items.
        """
        media_items: list[MediaItem] = []
        start_index = 0

        while True:
            payload = self._request(
                "GET",
                ITEMS_ENDPOINT,
                params={
                    "ParentId": library.id,
                    "Recursive": "true",
                    "IncludeItemTypes": ITEM_TYPES,
                    "Fields": ITEM_FIELDS,
                    "StartIndex": start_index,
                    "Limit": self._page_size,
                },
            )
            raw_items = self._get_required_list(payload, "Items", "items response")

            for raw_item in raw_items:
                media_items.append(self._media_item_from_json(raw_item, library))

            total_count = self._get_optional_int(payload, "TotalRecordCount")
            start_index += len(raw_items)

            if not raw_items:
                break

            if total_count is not None and start_index >= total_count:
                break

            if len(raw_items) < self._page_size:
                break

        return media_items

    def get_all_media(self) -> list[MediaItem]:
        """Return all media items from enabled libraries only.

        Returns:
            A list of normalized movie and episode items from enabled libraries.
        """
        media_items: list[MediaItem] = []

        for library in self.get_libraries():
            if library.is_movie_library and not self._config.processing.enable_movies:
                continue
            if library.is_tv_library and not self._config.processing.enable_tv:
                continue
            if not library.is_movie_library and not library.is_tv_library:
                continue

            media_items.extend(self._get_library_items_for_library(library))

        return media_items

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, RequestParamValue] | None = None,
    ) -> dict[str, Any]:
        """Perform a Jellyfin REST request and decode the JSON response.

        Args:
            method: HTTP method name.
            path: Relative API path.
            params: Optional query string parameters.

        Returns:
            The decoded JSON response body as a dictionary.

        Raises:
            JellyfinRequestError: If the HTTP request fails.
            JellyfinResponseError: If the response body is not valid JSON.
        """
        url = self._build_url(path)

        try:
            response = self._session.request(
                method=method,
                url=url,
                params=params,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except requests.HTTPError as error:
            status_code = error.response.status_code if error.response is not None else "?"
            raise JellyfinRequestError(
                f"Jellyfin request failed with status {status_code}: {method} {url}"
            ) from error
        except requests.RequestException as error:
            raise JellyfinRequestError(
                f"Jellyfin request failed: {method} {url}: {error}"
            ) from error

        try:
            payload = response.json()
        except ValueError as error:
            raise JellyfinResponseError(
                f"Jellyfin returned invalid JSON for {method} {url}."
            ) from error

        if not isinstance(payload, dict):
            raise JellyfinResponseError(
                f"Jellyfin returned an unexpected JSON type for {method} {url}."
            )

        return payload

    def _build_url(self, path: str) -> str:
        """Build an absolute Jellyfin API URL.

        Args:
            path: Relative API path.

        Returns:
            The absolute URL for the request.
        """
        return f"{self._server_url}/{path.lstrip('/')}"

    def _get_library_by_id(self, library_id: str) -> MediaLibrary:
        """Look up one media library by identifier.

        Args:
            library_id: Jellyfin library identifier.

        Returns:
            The matching media library.

        Raises:
            JellyfinResponseError: If the library cannot be found.
        """
        normalized_library_id = library_id.strip()

        for library in self.get_libraries():
            if library.id == normalized_library_id:
                return library

        raise JellyfinResponseError(
            f"Jellyfin library {normalized_library_id!r} was not found."
        )

    def _library_from_json(self, library_data: Mapping[str, Any]) -> MediaLibrary:
        """Convert one Jellyfin library object into a normalized model.

        Args:
            library_data: Raw Jellyfin library JSON object.

        Returns:
            A normalized media library.
        """
        raw_locations = self._get_optional_list(
            library_data,
            "Locations",
            "library object",
        )
        locations = tuple(
            Path(location.strip())
            for location in self._iter_string_values(raw_locations, "library locations")
        )

        return MediaLibrary(
            id=self._get_required_str(library_data, "Id", "library object"),
            name=self._get_required_str(library_data, "Name", "library object"),
            collection_type=self._get_optional_str(library_data, "CollectionType"),
            locations=locations,
        )

    def _media_item_from_json(
        self,
        item_data: Mapping[str, Any],
        library: MediaLibrary,
    ) -> MediaItem:
        """Convert one Jellyfin media item into a normalized model.

        Args:
            item_data: Raw Jellyfin item JSON object.
            library: The normalized library containing the item.

        Returns:
            A normalized media item.
        """
        item_type = self._get_required_str(item_data, "Type", "media item").lower()
        if item_type not in {"movie", "episode"}:
            raise JellyfinResponseError(
                f"Unsupported Jellyfin item type {item_type!r} in media item."
            )

        subtitle_tracks: list[SubtitleTrack] = []
        audio_tracks: list[AudioTrack] = []
        video_track: VideoTrack | None = None

        for stream_data in self._get_optional_list(
            item_data,
            "MediaStreams",
            "media item",
        ):
            if not isinstance(stream_data, Mapping):
                raise JellyfinResponseError(
                    "Jellyfin returned a non-object media stream in a media item."
                )

            stream_type = self._get_required_str(
                stream_data,
                "Type",
                "media stream",
            ).lower()

            if stream_type == SUBTITLE_STREAM_TYPE:
                subtitle_tracks.append(self._subtitle_track_from_stream(stream_data))
            elif stream_type == AUDIO_STREAM_TYPE:
                audio_tracks.append(self._audio_track_from_stream(stream_data))
            elif stream_type == VIDEO_STREAM_TYPE and video_track is None:
                video_track = self._video_track_from_stream(stream_data)

        return MediaItem(
            id=self._get_required_str(item_data, "Id", "media item"),
            title=self._get_required_str(item_data, "Name", "media item"),
            path=Path(self._get_required_str(item_data, "Path", "media item")),
            is_movie=item_type == "movie",
            is_episode=item_type == "episode",
            library=library.name,
            series_name=self._get_optional_str(item_data, "SeriesName"),
            season_name=self._get_optional_str(item_data, "SeasonName"),
            season_number=self._get_optional_int(item_data, "ParentIndexNumber"),
            episode_number=self._get_optional_int(item_data, "IndexNumber"),
            year=self._get_optional_int(item_data, "ProductionYear"),
            runtime_ticks=self._get_optional_int(item_data, "RunTimeTicks"),
            image_tags=self._get_string_dict(item_data, "ImageTags"),
            subtitle_tracks=tuple(subtitle_tracks),
            audio_tracks=tuple(audio_tracks),
            video_track=video_track,
        )

    def _subtitle_track_from_stream(
        self,
        stream_data: Mapping[str, Any],
    ) -> SubtitleTrack:
        """Convert one Jellyfin subtitle stream into a normalized model.

        Args:
            stream_data: Raw Jellyfin media stream JSON object.

        Returns:
            A normalized subtitle track.
        """
        return SubtitleTrack(
            language=self._get_optional_str(stream_data, "Language") or "",
            codec=self._get_optional_str(stream_data, "Codec"),
            is_external=self._get_bool(stream_data, "IsExternal"),
            is_default=self._get_bool(stream_data, "IsDefault"),
            is_forced=self._get_bool(stream_data, "IsForced"),
        )

    def _audio_track_from_stream(self, stream_data: Mapping[str, Any]) -> AudioTrack:
        """Convert one Jellyfin audio stream into a normalized model.

        Args:
            stream_data: Raw Jellyfin media stream JSON object.

        Returns:
            A normalized audio track.
        """
        return AudioTrack(
            language=self._get_optional_str(stream_data, "Language") or "",
            codec=self._get_optional_str(stream_data, "Codec") or "unknown",
            channels=self._get_optional_int(stream_data, "Channels"),
            title=self._get_optional_str(stream_data, "Title")
            or self._get_optional_str(stream_data, "DisplayTitle"),
        )

    def _video_track_from_stream(self, stream_data: Mapping[str, Any]) -> VideoTrack:
        """Convert one Jellyfin video stream into a normalized model.

        Args:
            stream_data: Raw Jellyfin media stream JSON object.

        Returns:
            A normalized video track.
        """
        video_range = self._get_optional_str(stream_data, "VideoRange")

        return VideoTrack(
            codec=self._get_optional_str(stream_data, "Codec") or "unknown",
            width=self._get_optional_int(stream_data, "Width") or 0,
            height=self._get_optional_int(stream_data, "Height") or 0,
            bitrate=self._get_optional_int(stream_data, "BitRate"),
            hdr=self._is_hdr_video_stream(stream_data, video_range),
            video_range=video_range,
        )

    def _is_hdr_video_stream(
        self,
        stream_data: Mapping[str, Any],
        video_range: str | None,
    ) -> bool:
        """Return whether a video stream should be treated as HDR.

        Args:
            stream_data: Raw Jellyfin media stream JSON object.
            video_range: Normalized video range, if available.

        Returns:
            ``True`` when the stream appears to be HDR.
        """
        if self._get_bool(stream_data, "IsHdr"):
            return True

        hdr_type = self._get_optional_str(stream_data, "HdrType")
        video_range_type = self._get_optional_str(stream_data, "VideoRangeType")
        hdr_markers = ("hdr", "hlg", "dolby vision", "dv")

        for value in (video_range, hdr_type, video_range_type):
            if value is None:
                continue
            lowered_value = value.lower()
            if any(marker in lowered_value for marker in hdr_markers):
                return True

        return False

    @staticmethod
    def _get_required_str(
        data: Mapping[str, Any],
        key: str,
        context: str,
    ) -> str:
        """Return a required non-empty string field from a JSON object."""
        value = data.get(key)
        if not isinstance(value, str):
            raise JellyfinResponseError(
                f"Expected {key!r} to be a string in {context}."
            )

        normalized_value = value.strip()
        if not normalized_value:
            raise JellyfinResponseError(
                f"Expected {key!r} to be a non-empty string in {context}."
            )

        return normalized_value

    @staticmethod
    def _get_optional_str(data: Mapping[str, Any], key: str) -> str | None:
        """Return an optional string field from a JSON object."""
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise JellyfinResponseError(f"Expected {key!r} to be a string.")

        normalized_value = value.strip()
        return normalized_value or None

    @staticmethod
    def _get_optional_int(data: Mapping[str, Any], key: str) -> int | None:
        """Return an optional integer field from a JSON object."""
        value = data.get(key)
        if value is None:
            return None
        if isinstance(value, bool):
            raise JellyfinResponseError(f"Expected {key!r} to be an integer.")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            stripped_value = value.strip()
            if not stripped_value:
                return None
            try:
                return int(stripped_value)
            except ValueError as error:
                raise JellyfinResponseError(
                    f"Expected {key!r} to be an integer."
                ) from error

        raise JellyfinResponseError(f"Expected {key!r} to be an integer.")

    @staticmethod
    def _get_bool(data: Mapping[str, Any], key: str) -> bool:
        """Return a boolean field from a JSON object."""
        value = data.get(key)
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str):
            normalized_value = value.strip().lower()
            if normalized_value in {"1", "true", "yes", "on"}:
                return True
            if normalized_value in {"0", "false", "no", "off", ""}:
                return False

        raise JellyfinResponseError(f"Expected {key!r} to be a boolean.")

    @staticmethod
    def _get_required_list(
        data: Mapping[str, Any],
        key: str,
        context: str,
    ) -> list[Any]:
        """Return a required list field from a JSON object."""
        value = data.get(key)
        if not isinstance(value, list):
            raise JellyfinResponseError(
                f"Expected {key!r} to be a list in {context}."
            )

        return value

    @staticmethod
    def _get_optional_list(
        data: Mapping[str, Any],
        key: str,
        context: str,
    ) -> list[Any]:
        """Return an optional list field from a JSON object."""
        value = data.get(key)
        if value is None:
            return []
        if not isinstance(value, list):
            raise JellyfinResponseError(
                f"Expected {key!r} to be a list in {context}."
            )

        return value

    @staticmethod
    def _get_string_dict(data: Mapping[str, Any], key: str) -> dict[str, str]:
        """Return a normalized string dictionary field from a JSON object."""
        value = data.get(key)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise JellyfinResponseError(f"Expected {key!r} to be an object.")

        normalized_dict: dict[str, str] = {}

        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not isinstance(raw_value, str):
                raise JellyfinResponseError(
                    f"Expected {key!r} to contain only string keys and values."
                )

            normalized_key = raw_key.strip()
            normalized_value = raw_value.strip()
            if normalized_key and normalized_value:
                normalized_dict[normalized_key] = normalized_value

        return normalized_dict

    @staticmethod
    def _iter_string_values(values: list[Any], context: str) -> tuple[str, ...]:
        """Return normalized string values from a raw JSON list."""
        normalized_values: list[str] = []

        for value in values:
            if not isinstance(value, str):
                raise JellyfinResponseError(
                    f"Expected only string values in {context}."
                )

            normalized_value = value.strip()
            if normalized_value:
                normalized_values.append(normalized_value)

        return tuple(normalized_values)


__all__ = [
    "JellyfinClient",
    "JellyfinConfigurationError",
    "JellyfinError",
    "JellyfinRequestError",
    "JellyfinResponseError",
]