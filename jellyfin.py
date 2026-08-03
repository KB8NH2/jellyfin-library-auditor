"""Jellyfin REST API client for normalized media retrieval.

This module is responsible for talking to the Jellyfin API and converting API
responses into application data models. It does not contain audit logic,
reporting, or any assumptions about how media data will be evaluated.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests

from config import ProcessingConfig
from config import ServerConfig
from models import AudioTrack
from models import MediaItem
from models import MediaLibrary
from models import SubtitleTrack
from models import VideoTrack
from results import ComparisonSetting
from results import LibraryComparisonSettings


LIBRARIES_ENDPOINT = "/Library/MediaFolders"
ITEMS_ENDPOINT = "/Items"
PING_ENDPOINT = "/System/Info/Public"
SYSTEM_CONFIGURATION_ENDPOINT = "/System/Configuration"
VIRTUAL_FOLDERS_ENDPOINT = "/Library/VirtualFolders"
ENCODING_CONFIGURATION_KEY = "encoding"
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
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PAGE_SIZE = 200
SERVER_USER_EXPERIENCE_FIELDS = (
    ("ServerName", "Server Name"),
    ("UICulture", "UI Culture"),
    ("PreferredMetadataLanguage", "Preferred Metadata Language"),
    ("MetadataCountryCode", "Metadata Country Code"),
    ("EnableFolderView", "Enable Folder View"),
    ("EnableGroupingMoviesIntoCollections", "Group Movies Into Collections"),
    ("EnableGroupingShowsIntoCollections", "Group Shows Into Collections"),
    ("DisplaySpecialsWithinSeasons", "Display Specials Within Seasons"),
    ("EnableExternalContentInSuggestions", "External Content In Suggestions"),
    ("ImageSavingConvention", "Image Saving Convention"),
    ("ChapterImageResolution", "Chapter Image Resolution"),
    ("RemoteClientBitrateLimit", "Remote Client Bitrate Limit"),
    ("MinResumePct", "Minimum Resume Percent"),
    ("MaxResumePct", "Maximum Resume Percent"),
    ("MinResumeDurationSeconds", "Minimum Resume Duration Seconds"),
    ("MinAudiobookResume", "Minimum Audiobook Resume"),
    ("MaxAudiobookResume", "Maximum Audiobook Resume"),
    ("SortRemoveCharacters", "Sort Remove Characters"),
    ("SortRemoveWords", "Sort Remove Words"),
    ("SortReplaceCharacters", "Sort Replace Characters"),
)
PLAYBACK_USER_EXPERIENCE_FIELDS = (
    ("HardwareAccelerationType", "Playback Hardware Acceleration"),
    ("EnableHardwareEncoding", "Playback Hardware Encoding"),
    ("HardwareDecodingCodecs", "Playback Hardware Decoding Codecs"),
    ("AllowHevcEncoding", "Playback Allow HEVC Encoding"),
    ("AllowAv1Encoding", "Playback Allow AV1 Encoding"),
    ("RemoteClientBitrateLimit", "Playback Remote Client Bitrate Limit"),
    ("EncodingThreadCount", "Playback Encoding Thread Count"),
    ("EncoderPreset", "Playback Encoder Preset"),
    ("H264Crf", "Playback H264 CRF"),
    ("H265Crf", "Playback H265 CRF"),
    ("EnableAudioVbr", "Playback Audio VBR"),
    ("DownMixAudioBoost", "Playback Downmix Audio Boost"),
    ("DownMixStereoAlgorithm", "Playback Downmix Stereo Algorithm"),
    ("MaxMuxingQueueSize", "Playback Max Muxing Queue Size"),
    ("EnableThrottling", "Playback Transcoding Throttling"),
    ("ThrottleDelaySeconds", "Playback Throttle Delay Seconds"),
    ("EnableSegmentDeletion", "Playback Segment Deletion"),
    ("SegmentKeepSeconds", "Playback Segment Keep Seconds"),
    ("EnableSubtitleExtraction", "Playback Subtitle Extraction"),
    ("EnableFallbackFont", "Playback Fallback Font"),
    ("FallbackFontPath", "Playback Fallback Font Path"),
    ("TranscodingTempPath", "Playback Transcoding Temp Path"),
    ("DeinterlaceMethod", "Playback Deinterlace Method"),
    ("DeinterlaceDoubleRate", "Playback Double Rate Deinterlace"),
    ("EnableTonemapping", "Playback Tonemapping"),
    ("TonemappingAlgorithm", "Playback Tonemapping Algorithm"),
    ("TonemappingMode", "Playback Tonemapping Mode"),
    ("TonemappingRange", "Playback Tonemapping Range"),
    ("TonemappingDesat", "Playback Tonemapping Desaturation"),
    ("TonemappingPeak", "Playback Tonemapping Peak"),
    ("TonemappingParam", "Playback Tonemapping Parameter"),
    ("EnableVppTonemapping", "Playback VPP Tonemapping"),
    ("VppTonemappingBrightness", "Playback VPP Brightness"),
    ("VppTonemappingContrast", "Playback VPP Contrast"),
    ("EnableVideoToolboxTonemapping", "Playback VideoToolbox Tonemapping"),
    ("EnableEnhancedNvdecDecoder", "Playback Enhanced NVDEC Decoder"),
    ("PreferSystemNativeHwDecoder", "Playback Prefer Native HW Decoder"),
    ("EnableIntelLowPowerH264HwEncoder", "Playback Intel Low Power H264"),
    ("EnableIntelLowPowerHevcHwEncoder", "Playback Intel Low Power HEVC"),
    ("QsvDevice", "Playback QSV Device"),
    ("VaapiDevice", "Playback VAAPI Device"),
    ("EnableDecodingColorDepth10Hevc", "Playback 10-bit HEVC Decode"),
    ("EnableDecodingColorDepth10Vp9", "Playback 10-bit VP9 Decode"),
    ("EnableDecodingColorDepth10HevcRext", "Playback 10-bit HEVC RExt Decode"),
    ("EnableDecodingColorDepth12HevcRext", "Playback 12-bit HEVC RExt Decode"),
)
LIBRARY_USER_EXPERIENCE_FIELDS = (
    (("CollectionType",), "Collection Type"),
    (("Locations",), "Locations"),
    (("LibraryOptions", "PathInfos"), "Managed Paths"),
    (("LibraryOptions", "Enabled"), "Enabled"),
    (("LibraryOptions", "EnablePhotos"), "Enable Photos"),
    (("LibraryOptions", "AutomaticallyAddToCollection"), "Automatically Add To Collection"),
    (("LibraryOptions", "EnableRealtimeMonitor"), "Realtime Monitor"),
    (
        ("LibraryOptions", "AutomaticRefreshIntervalDays"),
        "Automatic Refresh Interval Days",
    ),
    (
        ("LibraryOptions", "EnableAutomaticSeriesGrouping"),
        "Automatic Series Grouping",
    ),
    (("LibraryOptions", "EnableEmbeddedTitles"), "Embedded Titles"),
    (
        ("LibraryOptions", "EnableEmbeddedEpisodeInfos"),
        "Embedded Episode Info",
    ),
    (
        ("LibraryOptions", "EnableEmbeddedExtrasTitles"),
        "Embedded Extras Titles",
    ),
    (("LibraryOptions", "EnableInternetProviders"), "Internet Providers"),
    (("LibraryOptions", "SeasonZeroDisplayName"), "Season Zero Display Name"),
    (("LibraryOptions", "EnableLUFSScan"), "Enable LUFS Scan"),
    (
        ("LibraryOptions", "PreferredMetadataLanguage"),
        "Preferred Metadata Language",
    ),
    (("LibraryOptions", "MetadataCountryCode"), "Metadata Country Code"),
    (("LibraryOptions", "SaveLocalMetadata"), "Save Local Metadata"),
    (("LibraryOptions", "MetadataSavers"), "Metadata Savers"),
    (
        ("LibraryOptions", "DisabledLocalMetadataReaders"),
        "Disabled Local Metadata Readers",
    ),
    (
        ("LibraryOptions", "LocalMetadataReaderOrder"),
        "Local Metadata Reader Order",
    ),
    (("LibraryOptions", "AllowEmbeddedSubtitles"), "Allow Embedded Subtitles"),
    (
        ("LibraryOptions", "RequirePerfectSubtitleMatch"),
        "Require Perfect Subtitle Match",
    ),
    (
        ("LibraryOptions", "SkipSubtitlesIfEmbeddedSubtitlesPresent"),
        "Skip If Embedded Subtitles Present",
    ),
    (
        ("LibraryOptions", "SkipSubtitlesIfAudioTrackMatches"),
        "Skip If Audio Matches Subtitle Language",
    ),
    (
        ("LibraryOptions", "SubtitleDownloadLanguages"),
        "Subtitle Download Languages",
    ),
    (("LibraryOptions", "SubtitleFetcherOrder"), "Subtitle Fetcher Order"),
    (
        ("LibraryOptions", "DisabledSubtitleFetchers"),
        "Disabled Subtitle Fetchers",
    ),
    (("LibraryOptions", "SaveSubtitlesWithMedia"), "Save Subtitles With Media"),
    (("LibraryOptions", "SaveLyricsWithMedia"), "Save Lyrics With Media"),
    (
        ("LibraryOptions", "PreferNonstandardArtistsTag"),
        "Prefer Nonstandard Artists Tag",
    ),
    (("LibraryOptions", "UseCustomTagDelimiters"), "Use Custom Tag Delimiters"),
    (("LibraryOptions", "CustomTagDelimiters"), "Custom Tag Delimiters"),
    (("LibraryOptions", "DelimiterWhitelist"), "Delimiter Whitelist"),
    (
        ("LibraryOptions", "DisabledMediaSegmentProviders"),
        "Disabled Media Segment Providers",
    ),
    (
        ("LibraryOptions", "MediaSegmentProviderOrder"),
        "Media Segment Provider Order",
    ),
    (("LibraryOptions", "DisabledLyricFetchers"), "Disabled Lyric Fetchers"),
    (("LibraryOptions", "LyricFetcherOrder"), "Lyric Fetcher Order"),
    (
        ("LibraryOptions", "EnableChapterImageExtraction"),
        "Chapter Image Extraction",
    ),
    (
        ("LibraryOptions", "ExtractChapterImagesDuringLibraryScan"),
        "Extract Chapter Images During Scan",
    ),
    (
        ("LibraryOptions", "EnableTrickplayImageExtraction"),
        "Trickplay Image Extraction",
    ),
    (
        ("LibraryOptions", "ExtractTrickplayImagesDuringLibraryScan"),
        "Extract Trickplay Images During Scan",
    ),
    (
        ("LibraryOptions", "SaveTrickplayWithMedia"),
        "Save Trickplay With Media",
    ),
)


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

    def __init__(
        self,
        server: ServerConfig,
        *,
        processing: ProcessingConfig | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        """Initialize the Jellyfin client from one server configuration."""
        self._session = requests.Session()
        self._processing = processing

        if not server.api_key:
            raise JellyfinConfigurationError(
                "servers.toml must define a non-empty api_key for the selected server."
            )

        self._server = server
        self._server_url = server.url.rstrip("/")
        self._timeout = timeout_seconds
        self._page_size = page_size

        self._session.headers.update(
            {
                "Accept": "application/json",
                "X-Emby-Token": server.api_key,
            }
        )

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> JellyfinClient:
        """Return the client for context manager usage."""
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Close the HTTP session when leaving a context manager."""
        self.close()

    def ping(self) -> bool:
        """Return whether the Jellyfin server is reachable."""
        try:
            self._request(PING_ENDPOINT)
        except JellyfinError:
            return False

        return True

    def get_libraries(self) -> list[MediaLibrary]:
        """Return all available Jellyfin media libraries.

        Returns:
            A list of normalized media library objects.
        """
        payload = self._request(LIBRARIES_ENDPOINT)
        raw_libraries = self._get_required_list(payload, "Items", "library response")

        libraries: list[MediaLibrary] = []
        for raw_library in raw_libraries:
            libraries.append(self._library_from_json(raw_library))

        return libraries

    def get_server_name(self) -> str | None:
        """Return the Jellyfin server name reported by the API.

        Returns:
            The server display name, or ``None`` when Jellyfin does not provide
            one in the public system info response.
        """
        payload = self._request(PING_ENDPOINT)
        server_name = self._get_optional_str(payload, "ServerName")
        if server_name is not None:
            return server_name

        return self._get_optional_str(payload, "Name")

    def get_library_items(self, library_id: str) -> list[MediaItem]:
        """Return every movie or episode contained in a Jellyfin library.

        Args:
            library_id: Jellyfin library identifier.

        Returns:
            A list of normalized media items.
        """
        library = self._get_library_by_id(library_id)
        return self._get_library_items_for_library(library)

    def get_server_user_experience_settings(self) -> tuple[ComparisonSetting, ...]:
        """Return selected server settings that can affect user-visible behavior."""
        payload = self._request(SYSTEM_CONFIGURATION_ENDPOINT)
        encoding_payload = self._request_named_configuration(
            ENCODING_CONFIGURATION_KEY
        )
        return tuple(
            [
                *(
                    ComparisonSetting(
                        label=label,
                        value=self._stringify_setting_value(payload.get(key)),
                    )
                    for key, label in SERVER_USER_EXPERIENCE_FIELDS
                ),
                *(
                    ComparisonSetting(
                        label=label,
                        value=self._stringify_setting_value(encoding_payload.get(key)),
                    )
                    for key, label in PLAYBACK_USER_EXPERIENCE_FIELDS
                ),
            ]
        )

    def get_library_user_experience_settings(
        self,
        library_names: Iterable[str] | None = None,
    ) -> tuple[LibraryComparisonSettings, ...]:
        """Return selected library settings that can affect user-visible behavior."""
        payload = self._request_payload("GET", VIRTUAL_FOLDERS_ENDPOINT)
        if not isinstance(payload, list):
            raise JellyfinResponseError(
                "Jellyfin returned an unexpected JSON type for virtual folders."
            )

        included_names = (
            None
            if library_names is None
            else {name.strip().casefold() for name in library_names if name.strip()}
        )
        settings: list[LibraryComparisonSettings] = []
        for entry in payload:
            if not isinstance(entry, Mapping):
                raise JellyfinResponseError(
                    "Jellyfin returned a non-object virtual folder entry."
                )
            library_name = self._get_required_str(entry, "Name", "virtual folder")
            if included_names is not None and library_name.casefold() not in included_names:
                continue
            library_settings = tuple(
                [
                    *(
                        ComparisonSetting(
                            label=label,
                            value=self._stringify_setting_value(
                                self._nested_value(entry, path_parts)
                            ),
                        )
                        for path_parts, label in LIBRARY_USER_EXPERIENCE_FIELDS
                    ),
                    *self._type_option_settings(entry),
                ]
            )
            settings.append(
                LibraryComparisonSettings(
                    library_name=library_name,
                    settings=library_settings,
                )
            )

        return tuple(
            sorted(settings, key=lambda item: item.library_name.casefold())
        )

    def _request_named_configuration(self, key: str) -> dict[str, Any]:
        """Return one named Jellyfin configuration object."""
        return self._request(f"{SYSTEM_CONFIGURATION_ENDPOINT}/{key}")

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
                media_item = self._media_item_from_json(raw_item, library)
                if media_item is not None:
                    media_items.append(media_item)
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
            if (
                self._processing is not None
                and library.is_movie_library
                and not self._processing.enable_movies
            ):
                continue
            if (
                self._processing is not None
                and library.is_tv_library
                and not self._processing.enable_tv
            ):
                continue
            if not library.is_movie_library and not library.is_tv_library:
                continue

            media_items.extend(self._get_library_items_for_library(library))

        return media_items

    def _request(
        self,
        path: str,
        *,
        params: Mapping[str, RequestParamValue] | None = None,
    ) -> dict[str, Any]:
        """Perform a Jellyfin REST request and decode the JSON response.

        Args:
            path: Relative API path.
            params: Optional query string parameters.

        Returns:
            The decoded JSON response body as a dictionary.

        Raises:
            JellyfinRequestError: If the HTTP request fails.
            JellyfinResponseError: If the response body is not valid JSON.
        """
        payload = self._request_payload("GET", path, params=params)
        if not isinstance(payload, dict):
            raise JellyfinResponseError(
                f"Jellyfin returned an unexpected JSON type for GET {self._build_url(path)}."
            )

        return payload

    def _request_payload(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, RequestParamValue] | None = None,
    ) -> Any:
        """Perform a Jellyfin REST request and decode any JSON response body."""
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
            return response.json()
        except ValueError as error:
            raise JellyfinResponseError(
                f"Jellyfin returned invalid JSON for {method} {url}."
            ) from error

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
    ) -> MediaItem | None:
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
        try:
            if "Path" not in item_data:
                return None
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
        except Exception as e:
            print(item_data)
            raise e

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

    @classmethod
    def _nested_value(
        cls,
        data: Mapping[str, Any],
        path_parts: tuple[str, ...],
    ) -> Any:
        """Return a nested mapping value or ``None`` when any segment is missing."""
        current: Any = data
        for path_part in path_parts:
            if not isinstance(current, Mapping):
                return None
            current = current.get(path_part)
            if current is None:
                return None
        return current

    @classmethod
    def _stringify_setting_value(cls, value: Any) -> str:
        """Return a compact display value for one configuration setting."""
        if value is None:
            return ""
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, Mapping):
            return ", ".join(
                f"{key}={cls._stringify_setting_value(item_value)}"
                for key, item_value in value.items()
                if cls._stringify_setting_value(item_value)
            )
        if isinstance(value, list):
            rendered_items = [cls._stringify_setting_value(item) for item in value]
            return ", ".join(item for item in rendered_items if item)
        return str(value)

    @classmethod
    def _type_option_settings(
        cls,
        entry: Mapping[str, Any],
    ) -> tuple[ComparisonSetting, ...]:
        """Return settings derived from per-type library options."""
        type_options = cls._nested_value(entry, ("LibraryOptions", "TypeOptions"))
        if not isinstance(type_options, list):
            return ()

        settings: list[ComparisonSetting] = []
        for raw_type_option in type_options:
            if not isinstance(raw_type_option, Mapping):
                continue
            raw_type_name = raw_type_option.get("Type")
            if not isinstance(raw_type_name, str) or not raw_type_name.strip():
                continue
            type_name = raw_type_name.strip()
            settings.extend(
                (
                    ComparisonSetting(
                        f"{type_name} Metadata Fetchers",
                        cls._stringify_setting_value(
                            raw_type_option.get("MetadataFetchers")
                        ),
                    ),
                    ComparisonSetting(
                        f"{type_name} Metadata Fetcher Order",
                        cls._stringify_setting_value(
                            raw_type_option.get("MetadataFetcherOrder")
                        ),
                    ),
                    ComparisonSetting(
                        f"{type_name} Image Fetchers",
                        cls._stringify_setting_value(raw_type_option.get("ImageFetchers")),
                    ),
                    ComparisonSetting(
                        f"{type_name} Image Fetcher Order",
                        cls._stringify_setting_value(
                            raw_type_option.get("ImageFetcherOrder")
                        ),
                    ),
                    ComparisonSetting(
                        f"{type_name} Similar Item Providers",
                        cls._stringify_setting_value(
                            raw_type_option.get("SimilarItemProviders")
                        ),
                    ),
                    ComparisonSetting(
                        f"{type_name} Similar Item Provider Order",
                        cls._stringify_setting_value(
                            raw_type_option.get("SimilarItemProviderOrder")
                        ),
                    ),
                    ComparisonSetting(
                        f"{type_name} Image Options",
                        cls._image_options_summary(raw_type_option.get("ImageOptions")),
                    ),
                )
            )

        return tuple(settings)

    @classmethod
    def _image_options_summary(cls, value: Any) -> str:
        """Return a compact summary for one TypeOptions.ImageOptions list."""
        if not isinstance(value, list):
            return cls._stringify_setting_value(value)

        parts: list[str] = []
        for raw_item in value:
            if not isinstance(raw_item, Mapping):
                continue
            type_name = cls._stringify_setting_value(raw_item.get("Type"))
            limit = cls._stringify_setting_value(raw_item.get("Limit"))
            min_width = cls._stringify_setting_value(raw_item.get("MinWidth"))
            item_parts = [part for part in (type_name, f"limit={limit}" if limit else "", f"minWidth={min_width}" if min_width else "") if part]
            if item_parts:
                parts.append(" ".join(item_parts))
        return "; ".join(parts)


__all__ = [
    "JellyfinClient",
    "JellyfinConfigurationError",
    "JellyfinError",
    "JellyfinRequestError",
    "JellyfinResponseError",
]
