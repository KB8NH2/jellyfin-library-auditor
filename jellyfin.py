"""Jellyfin REST API client for normalized media retrieval.

This module is responsible for talking to the Jellyfin API and converting API
responses into application data models. It does not contain audit logic,
reporting, or any assumptions about how media data will be evaluated.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace
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
ITEM_ENDPOINT_TEMPLATE = "/Items/{item_id}"
ITEM_IMAGE_ENDPOINT_TEMPLATE = "/Items/{item_id}/Images/{image_type}"
ITEM_SUBTITLE_STREAM_ENDPOINT_TEMPLATE = (
    "/Videos/{item_id}/{media_source_id}/Subtitles/{index}/Stream.{format}"
)
ITEM_SUBTITLE_UPLOAD_ENDPOINT_TEMPLATE = "/Videos/{item_id}/Subtitles"
USER_ITEM_ENDPOINT_TEMPLATE = "/Users/{user_id}/Items/{item_id}"
USERS_ENDPOINT = "/Users"
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
        # Also gated behind Fields. Used only to decide which items need a
        # fresh per-item Name re-check - see _resolve_stale_title() - not
        # stored on MediaItem itself.
        "LockedFields",
    ]
)
ITEM_DETAIL_FIELDS = ",".join(
    [
        # Path and ProductionYear are gated behind an explicit Fields
        # request in Jellyfin's API too (see ITEM_FIELDS above) - omitting
        # them here previously meant get_item() silently returned a document
        # missing those keys entirely, which then let a caller's "replace
        # the whole item" update wipe them out on the server.
        "Path",
        "ProductionYear",
        "RunTimeTicks",
        "ImageTags",
        "BackdropImageTags",
        "MediaStreams",
        # Needed to address a specific subtitle stream for transfer (the
        # download/upload endpoints are scoped by media source id, which
        # isn't otherwise part of the item document). Excluded from update
        # payloads by transfer_metadata.NON_EDITABLE_ITEM_FIELDS, so
        # fetching it here doesn't risk being sent back on a metadata write.
        "MediaSources",
        "Overview",
        "Genres",
        "Tags",
        "Studios",
        "People",
        "ProviderIds",
        # Also gated behind Fields. A caller that locks a field it just
        # edited (so a metadata provider's next refresh doesn't silently
        # revert it) needs the item's existing locks here first, or a
        # "replace the whole item" update would wipe out any locks Jellyfin
        # already had for this item.
        "LockedFields",
        # Also gated behind Fields (transfer_metadata.py includes this
        # among TRANSFERABLE_METADATA_FIELDS, but without it here that
        # transfer silently never had a source value to copy - every
        # OriginalTitle read came back missing rather than None).
        "OriginalTitle",
    ]
)
ITEM_TYPES = "Movie,Episode"
SERIES_ITEM_TYPE = "Series"
EPISODE_ITEM_TYPE = "Episode"
MOVIE_ITEM_TYPE = "Movie"
MOVIE_MATCH_FIELDS = "Path,ProductionYear"
VIDEO_STREAM_TYPE = "video"
AUDIO_STREAM_TYPE = "audio"
SUBTITLE_STREAM_TYPE = "subtitle"
RequestParamValue = str | bytes | int | float | list[str] | tuple[str, ...] | None
# Jellyfin sometimes leaves ParentIndexNumber unset on an episode even
# though it still names the containing Season item "Season N" (e.g. when it
# can't confidently number every episode file in that season) - the same gap
# reports/templates.season_sort_value already falls back around for display.
# _resolved_season_number applies the identical fallback when matching
# episodes to a requested season number, so a season lookup doesn't silently
# drop episodes whose season is only known by name.
SEASON_NAME_NUMBER_PATTERN = re.compile(r"^\s*season\s+(\d+)\s*$", re.IGNORECASE)
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PAGE_SIZE = 200
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0

LOGGER = logging.getLogger("jellyfin")
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


@dataclass(frozen=True, slots=True)
class SeriesMatch:
    """One Series item found by name, with its TheTVDB provider id if any."""

    library_name: str
    series_id: str
    tvdb_id: str | None
    # None only for a series item Jellyfin reports with no Path at all - the
    # find_series() path_filter can't match such a series regardless of what
    # is asked for.
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class MovieMatch:
    """One Movie item found by name, with the fields needed to rename it."""

    library_name: str
    movie_id: str
    name: str
    path: Path | None
    year: int | None


@dataclass(frozen=True, slots=True)
class EpisodeSummary:
    """A minimal Episode item summary used to locate items to update."""

    id: str
    name: str
    episode_number: int


@dataclass(frozen=True, slots=True)
class SeasonEpisodeSummary:
    """An Episode item summary that keeps episodes missing an episode number.

    Unlike EpisodeSummary/get_series_season_episodes, episode_number may be
    ``None`` here - apply_episode_numbers.py needs exactly those episodes to
    know which ones to fill in. path is carried along too, since it's the
    best available proxy for on-disk/aired sequence for an episode that has
    no episode number of its own to sort by.
    """

    id: str
    name: str
    path: Path | None
    episode_number: int | None


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
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
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
        self._max_retries = max(1, max_retries)
        self._retry_backoff_seconds = retry_backoff_seconds
        self._cached_user_id: str | None = None
        self._request_count = 0

        self._session.headers.update(
            {
                "Accept": "application/json",
                "X-Emby-Token": server.api_key,
            }
        )

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    @property
    def request_count(self) -> int:
        """Return the number of HTTP requests this client has issued so far.

        Counts every actual attempt sent to the server, including one that
        was retried after a timeout/connection error - each such attempt is
        a real network round trip, not just the logical operation it was
        part of.
        """
        return self._request_count

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

    def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
        """Return each TV series' TheTVDB id(s), keyed by series name.

        Used by the optional aired/DVD episode-ordering check, which needs
        TheTVDB's series id but has no reason to fetch anything else about
        the Series item itself.

        Returns every distinct TheTVDB id found for a series name, not just
        one - a Jellyfin library can have more than one Series item sharing
        the exact same display name (e.g. TheTVDB splitting a long-running
        show into a separate entry for a newer era while the old entry keeps
        the earlier episodes, with both still titled the same in Jellyfin).
        Collapsing that to a single id per name would silently keep whichever
        one happened to be returned last and drop the other, which then makes
        every TheTVDB-backed check for that name compare local episodes
        against only half the real data. Callers combine data from every id
        returned for a name instead.

        Args:
            library_id: Jellyfin library identifier.

        Returns:
            A mapping of series display name to the distinct TheTVDB provider
            id(s) found for it, in first-seen order. A name with no series
            reporting a "Tvdb" provider id is omitted.
        """
        series_tvdb_ids: dict[str, list[str]] = {}
        start_index = 0

        while True:
            payload = self._request(
                ITEMS_ENDPOINT,
                params={
                    "ParentId": library_id,
                    "Recursive": "true",
                    "IncludeItemTypes": SERIES_ITEM_TYPE,
                    "Fields": "ProviderIds",
                    "StartIndex": start_index,
                    "Limit": self._page_size,
                },
            )
            raw_items = self._get_required_list(payload, "Items", "series response")

            for raw_item in raw_items:
                name = self._get_optional_str(raw_item, "Name")
                tvdb_id = self._get_string_dict(raw_item, "ProviderIds").get("Tvdb")
                if name and tvdb_id:
                    ids = series_tvdb_ids.setdefault(name, [])
                    if tvdb_id not in ids:
                        ids.append(tvdb_id)

            total_count = self._get_optional_int(payload, "TotalRecordCount")
            start_index += len(raw_items)

            if not raw_items:
                break
            if total_count is not None and start_index >= total_count:
                break
            if len(raw_items) < self._page_size:
                break

        return {name: tuple(ids) for name, ids in series_tvdb_ids.items()}

    def find_series(
        self,
        series_name: str,
        *,
        library_name: str | None = None,
        path_filter: str | None = None,
    ) -> tuple[SeriesMatch, ...]:
        """Return every Series item in TV libraries whose name matches.

        Args:
            series_name: Series display name to match, case-insensitively.
            library_name: When given, only that library (matched
                case-insensitively) is searched instead of every TV library.
            path_filter: When given, only a series whose Path contains this
                text (case-insensitively) is included - a series with no
                Path at all never matches. Used to disambiguate a name that
                matches more than one series when --library alone isn't
                specific enough (e.g. two shows sharing a name, both in the
                same library).

        Returns:
            One SeriesMatch per matching series, across every TV library
            searched. A series with no "Tvdb" provider id is still included,
            with tvdb_id set to None.
        """
        normalized_name = series_name.strip().casefold()
        normalized_library_name = (
            library_name.strip().casefold() if library_name is not None else None
        )
        normalized_path_filter = (
            path_filter.strip().casefold() if path_filter is not None else None
        )

        matches: list[SeriesMatch] = []
        for library in self.get_libraries():
            if not library.is_tv_library:
                continue
            if (
                normalized_library_name is not None
                and library.name.strip().casefold() != normalized_library_name
            ):
                continue

            matches.extend(
                self._find_series_in_library(library, normalized_name, normalized_path_filter)
            )

        return tuple(matches)

    def _find_series_in_library(
        self,
        library: MediaLibrary,
        normalized_name: str,
        normalized_path_filter: str | None = None,
    ) -> list[SeriesMatch]:
        """Return every Series item in one library whose name matches."""
        matches: list[SeriesMatch] = []
        start_index = 0

        while True:
            payload = self._request(
                ITEMS_ENDPOINT,
                params={
                    "ParentId": library.id,
                    "Recursive": "true",
                    "IncludeItemTypes": SERIES_ITEM_TYPE,
                    "Fields": "ProviderIds,Path",
                    "StartIndex": start_index,
                    "Limit": self._page_size,
                },
            )
            raw_items = self._get_required_list(payload, "Items", "series response")

            for raw_item in raw_items:
                name = self._get_optional_str(raw_item, "Name")
                if name is None or name.strip().casefold() != normalized_name:
                    continue
                raw_path = self._get_optional_str(raw_item, "Path")
                path = Path(raw_path) if raw_path else None
                if normalized_path_filter is not None and (
                    path is None or normalized_path_filter not in str(path).casefold()
                ):
                    continue
                matches.append(
                    SeriesMatch(
                        path=path,
                        library_name=library.name,
                        series_id=self._get_required_str(raw_item, "Id", "series item"),
                        tvdb_id=self._get_string_dict(raw_item, "ProviderIds").get("Tvdb"),
                    )
                )

            total_count = self._get_optional_int(payload, "TotalRecordCount")
            start_index += len(raw_items)

            if not raw_items:
                break
            if total_count is not None and start_index >= total_count:
                break
            if len(raw_items) < self._page_size:
                break

        return matches

    def find_movie(
        self,
        movie_name: str,
        *,
        library_name: str | None = None,
        path_filter: str | None = None,
    ) -> tuple[MovieMatch, ...]:
        """Return every Movie item in movie libraries whose name matches.

        Args:
            movie_name: Movie display name to match, case-insensitively.
            library_name: When given, only that library (matched
                case-insensitively) is searched instead of every movie library.
            path_filter: When given, only a movie whose Path contains this
                text (case-insensitively) is included - a movie with no Path
                at all never matches. Used to disambiguate a name that
                matches more than one movie when --library alone isn't
                specific enough.

        Returns:
            One MovieMatch per matching movie, across every movie library
            searched.
        """
        normalized_name = movie_name.strip().casefold()
        normalized_library_name = (
            library_name.strip().casefold() if library_name is not None else None
        )
        normalized_path_filter = (
            path_filter.strip().casefold() if path_filter is not None else None
        )

        matches: list[MovieMatch] = []
        for library in self.get_libraries():
            if not library.is_movie_library:
                continue
            if (
                normalized_library_name is not None
                and library.name.strip().casefold() != normalized_library_name
            ):
                continue

            matches.extend(
                self._find_movie_in_library(library, normalized_name, normalized_path_filter)
            )

        return tuple(matches)

    def _find_movie_in_library(
        self,
        library: MediaLibrary,
        normalized_name: str,
        normalized_path_filter: str | None = None,
    ) -> list[MovieMatch]:
        """Return every Movie item in one library whose name matches."""
        matches: list[MovieMatch] = []
        start_index = 0

        while True:
            payload = self._request(
                ITEMS_ENDPOINT,
                params={
                    "ParentId": library.id,
                    "Recursive": "true",
                    "IncludeItemTypes": MOVIE_ITEM_TYPE,
                    "Fields": MOVIE_MATCH_FIELDS,
                    "StartIndex": start_index,
                    "Limit": self._page_size,
                },
            )
            raw_items = self._get_required_list(payload, "Items", "movies response")

            for raw_item in raw_items:
                name = self._get_optional_str(raw_item, "Name")
                if name is None or name.strip().casefold() != normalized_name:
                    continue
                raw_path = self._get_optional_str(raw_item, "Path")
                path = Path(raw_path) if raw_path else None
                if normalized_path_filter is not None and (
                    path is None or normalized_path_filter not in str(path).casefold()
                ):
                    continue
                matches.append(
                    MovieMatch(
                        library_name=library.name,
                        movie_id=self._get_required_str(raw_item, "Id", "movie item"),
                        name=name,
                        path=path,
                        year=self._get_optional_int(raw_item, "ProductionYear"),
                    )
                )

            total_count = self._get_optional_int(payload, "TotalRecordCount")
            start_index += len(raw_items)

            if not raw_items:
                break
            if total_count is not None and start_index >= total_count:
                break
            if len(raw_items) < self._page_size:
                break

        return matches

    def _resolved_season_number(self, raw_item: Mapping[str, Any]) -> int | None:
        """Return one episode's season number, falling back to its season name.

        Usually ParentIndexNumber alone is enough, but Jellyfin sometimes
        leaves it unset on an episode it couldn't confidently number even
        though the containing Season item still has a plain "Season N" name
        - in which case that name is the only place the season number is
        recorded at all. Without this fallback, such an episode would never
        match any season lookup, silently vanishing from
        get_series_season_episodes/get_series_season_episodes_all instead of
        being found under the season its name says it belongs to.
        """
        season_number = self._get_optional_int(raw_item, "ParentIndexNumber")
        if season_number is not None:
            return season_number

        season_name = self._get_optional_str(raw_item, "SeasonName")
        if season_name is None:
            return None

        match = SEASON_NAME_NUMBER_PATTERN.match(season_name)
        return int(match.group(1)) if match else None

    def get_series_season_episodes(
        self,
        series_id: str,
        season_number: int,
    ) -> tuple[EpisodeSummary, ...]:
        """Return every Episode item in one season of one series.

        Episode items usually carry their season number directly
        (ParentIndexNumber), so this needs no separate Season-item lookup;
        see _resolved_season_number for the fallback used when that's unset.

        Args:
            series_id: Jellyfin Series item identifier.
            season_number: Season number to match against each episode's
                resolved season number.

        Returns:
            EpisodeSummary tuples for every matching episode, sorted by
            episode number. Episodes with no episode number are omitted.
        """
        episodes: list[EpisodeSummary] = []
        start_index = 0

        while True:
            payload = self._request(
                ITEMS_ENDPOINT,
                params={
                    "ParentId": series_id,
                    "Recursive": "true",
                    "IncludeItemTypes": EPISODE_ITEM_TYPE,
                    "StartIndex": start_index,
                    "Limit": self._page_size,
                },
            )
            raw_items = self._get_required_list(payload, "Items", "episodes response")

            for raw_item in raw_items:
                if self._resolved_season_number(raw_item) != season_number:
                    continue
                episode_number = self._get_optional_int(raw_item, "IndexNumber")
                if episode_number is None:
                    continue
                episodes.append(
                    EpisodeSummary(
                        id=self._get_required_str(raw_item, "Id", "episode item"),
                        name=self._get_optional_str(raw_item, "Name") or "",
                        episode_number=episode_number,
                    )
                )

            total_count = self._get_optional_int(payload, "TotalRecordCount")
            start_index += len(raw_items)

            if not raw_items:
                break
            if total_count is not None and start_index >= total_count:
                break
            if len(raw_items) < self._page_size:
                break

        return tuple(sorted(episodes, key=lambda episode: episode.episode_number))

    def get_series_season_episodes_all(
        self,
        series_id: str,
        season_number: int,
    ) -> tuple[SeasonEpisodeSummary, ...]:
        """Return every Episode item in one season, including unnumbered ones.

        Unlike get_series_season_episodes, an episode with no IndexNumber is
        included (with episode_number set to ``None``) instead of being
        dropped, so a caller can locate and fill those in. Each item's Path
        is also fetched, since apply_episode_numbers.py needs it to identify
        each episode in its output.

        Each episode's IndexNumber is re-checked with a per-item lookup
        (see _lookup_index_number) rather than trusted from this listing, so
        a number just cleared or set outside a library scan is reflected
        immediately. A season's episode count is small enough that the extra
        per-item requests are worth the accuracy.

        Args:
            series_id: Jellyfin Series item identifier.
            season_number: Season number to match against each episode's
                resolved season number; see _resolved_season_number.

        Returns:
            SeasonEpisodeSummary tuples for every matching episode, sorted by
            episode number with unnumbered episodes (sorted by path) last.
        """
        episodes: list[SeasonEpisodeSummary] = []
        start_index = 0

        while True:
            payload = self._request(
                ITEMS_ENDPOINT,
                params={
                    "ParentId": series_id,
                    "Recursive": "true",
                    "IncludeItemTypes": EPISODE_ITEM_TYPE,
                    "Fields": "Path",
                    "StartIndex": start_index,
                    "Limit": self._page_size,
                },
            )
            raw_items = self._get_required_list(payload, "Items", "episodes response")

            for raw_item in raw_items:
                if self._resolved_season_number(raw_item) != season_number:
                    continue
                raw_path = self._get_optional_str(raw_item, "Path")
                episode_id = self._get_required_str(raw_item, "Id", "episode item")
                episodes.append(
                    SeasonEpisodeSummary(
                        id=episode_id,
                        name=self._get_optional_str(raw_item, "Name") or "",
                        path=Path(raw_path) if raw_path else None,
                        episode_number=self._lookup_index_number(episode_id),
                    )
                )

            total_count = self._get_optional_int(payload, "TotalRecordCount")
            start_index += len(raw_items)

            if not raw_items:
                break
            if total_count is not None and start_index >= total_count:
                break
            if len(raw_items) < self._page_size:
                break

        return tuple(sorted(episodes, key=self._season_episode_sort_key))

    @staticmethod
    def _season_episode_sort_key(
        episode: SeasonEpisodeSummary,
    ) -> tuple[bool, int, str]:
        """Return a sort key placing numbered episodes first, by number."""
        if episode.episode_number is not None:
            return (False, episode.episode_number, "")
        return (True, 0, str(episode.path).casefold() if episode.path else "")

    def get_series_episode_positions(self, series_id: str) -> frozenset[tuple[int, int]]:
        """Return every (season_number, episode_number) position with a local episode.

        Unlike get_series_season_episodes/get_series_season_episodes_all,
        this spans every season of the series in one call, since it exists
        to answer a different question - not "what episodes does this one
        season have" but "which TheTVDB series id actually explains this
        series' local episodes overall". apply_episode_titles.py and
        apply_dvd_metadata.py use it for exactly that: when more than one
        TheTVDB series shares this series' name, whichever one's positions
        overlap this set best is the one to trust, since Jellyfin's own
        assigned TheTVDB id can itself be the wrong one.

        An episode missing either its season or episode number is omitted -
        it can't contribute a position to compare against TheTVDB with.

        Args:
            series_id: Jellyfin Series item identifier.

        Returns:
            Every distinct (season_number, episode_number) position found
            locally for this series.
        """
        positions: set[tuple[int, int]] = set()
        start_index = 0

        while True:
            payload = self._request(
                ITEMS_ENDPOINT,
                params={
                    "ParentId": series_id,
                    "Recursive": "true",
                    "IncludeItemTypes": EPISODE_ITEM_TYPE,
                    "StartIndex": start_index,
                    "Limit": self._page_size,
                },
            )
            raw_items = self._get_required_list(payload, "Items", "episodes response")

            for raw_item in raw_items:
                season_number = self._resolved_season_number(raw_item)
                episode_number = self._get_optional_int(raw_item, "IndexNumber")
                if season_number is not None and episode_number is not None:
                    positions.add((season_number, episode_number))

            total_count = self._get_optional_int(payload, "TotalRecordCount")
            start_index += len(raw_items)

            if not raw_items:
                break
            if total_count is not None and start_index >= total_count:
                break
            if len(raw_items) < self._page_size:
                break

        return frozenset(positions)

    def get_item(self, item_id: str) -> dict[str, Any]:
        """Return the full Jellyfin metadata document for one item.

        Jellyfin's single-item lookup is scoped to a user (``/Users/{userId}/
        Items/{itemId}``); the userId-less ``/Items/{itemId}`` route rejects
        the request with a 400. This resolves and caches an administrator
        user from the server to satisfy that requirement.

        Args:
            item_id: Jellyfin item identifier.

        Returns:
            The raw item JSON document as Jellyfin returns it, suitable for
            round-tripping back through :meth:`update_item`.
        """
        user_id = self._resolve_admin_user_id()
        return self._request(
            USER_ITEM_ENDPOINT_TEMPLATE.format(user_id=user_id, item_id=item_id),
            params={"Fields": ITEM_DETAIL_FIELDS},
        )

    def _resolve_admin_user_id(self) -> str:
        """Return an administrator user id, resolving and caching it once."""
        if self._cached_user_id is not None:
            return self._cached_user_id

        payload = self._request_payload("GET", USERS_ENDPOINT)
        if not isinstance(payload, list) or not payload:
            raise JellyfinResponseError(
                "Jellyfin returned no users; cannot resolve a user context for item lookups."
            )

        selected_user: Mapping[str, Any] | None = None
        for raw_user in payload:
            if not isinstance(raw_user, Mapping):
                continue
            policy = raw_user.get("Policy")
            if isinstance(policy, Mapping) and policy.get("IsAdministrator"):
                selected_user = raw_user
                break

        if selected_user is None:
            first_user = payload[0]
            if not isinstance(first_user, Mapping):
                raise JellyfinResponseError("Jellyfin returned an unexpected user entry.")
            selected_user = first_user

        user_id = self._get_required_str(selected_user, "Id", "user object")
        self._cached_user_id = user_id
        return user_id

    def update_item(self, item_id: str, item_dto: Mapping[str, Any]) -> None:
        """Replace one Jellyfin item's metadata with a full item document.

        Args:
            item_id: Jellyfin item identifier to update.
            item_dto: Full item document, typically :meth:`get_item` output
                with selected fields overwritten. Jellyfin's update endpoint
                replaces the item's editable metadata wholesale rather than
                merging a partial payload.
        """
        self._request_payload(
            "POST",
            ITEM_ENDPOINT_TEMPLATE.format(item_id=item_id),
            json_body=item_dto,
        )

    def get_item_image(self, item_id: str, image_type: str) -> tuple[bytes, str] | None:
        """Return the raw bytes and content type of one item's cached image.

        Args:
            item_id: Jellyfin item identifier.
            image_type: Jellyfin image type (e.g. ``"Primary"``,
                ``"Backdrop"``, ``"Thumb"``).

        Returns:
            A ``(image_bytes, content_type)`` tuple, or ``None`` when the item
            has no cached image of that type.
        """
        url = self._build_url(
            ITEM_IMAGE_ENDPOINT_TEMPLATE.format(item_id=item_id, image_type=image_type)
        )
        try:
            response = self._send_request("GET", url, headers={"Accept": "*/*"})
            if response.status_code == 404:
                return None
            response.raise_for_status()
        except requests.RequestException as error:
            raise self._wrap_request_error("GET", url, error) from error

        content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        return response.content, content_type

    def upload_item_image(
        self,
        item_id: str,
        image_type: str,
        image_bytes: bytes,
        content_type: str,
    ) -> None:
        """Upload one item's cached image, replacing any existing one of that type.

        Jellyfin's image-upload endpoint expects the image bytes
        base64-encoded in the request body, with ``Content-Type`` set to the
        image's real MIME type rather than the encoding used on the wire.

        Args:
            item_id: Jellyfin item identifier to update.
            image_type: Jellyfin image type (e.g. ``"Primary"``).
            image_bytes: Raw, non-encoded image bytes.
            content_type: MIME type of ``image_bytes`` (e.g. ``"image/jpeg"``).
        """
        url = self._build_url(
            ITEM_IMAGE_ENDPOINT_TEMPLATE.format(item_id=item_id, image_type=image_type)
        )
        try:
            response = self._send_request(
                "POST",
                url,
                data=base64.b64encode(image_bytes),
                headers={"Content-Type": content_type},
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise self._wrap_request_error("POST", url, error) from error

    def get_item_subtitle(
        self,
        item_id: str,
        media_source_id: str,
        index: int,
        subtitle_format: str,
    ) -> bytes | None:
        """Return the raw bytes of one item's subtitle stream, converted to a format.

        Jellyfin serves subtitle content through this streaming endpoint
        regardless of where the underlying file actually lives on disk -
        next to the media file or in Jellyfin's own internal metadata cache
        - and transcodes on the fly when the source track uses a different
        text-based subtitle codec (e.g. ASS/SSA to SRT), so the caller never
        needs filesystem access to the source server.

        Args:
            item_id: Jellyfin item identifier.
            media_source_id: Media source identifier the stream belongs to.
            index: Stream index of the subtitle track, from its MediaStreams
                entry.
            subtitle_format: Subtitle format to request, e.g. ``"srt"``.

        Returns:
            The raw subtitle file bytes, or ``None`` when Jellyfin has
            nothing to return for this stream.
        """
        url = self._build_url(
            ITEM_SUBTITLE_STREAM_ENDPOINT_TEMPLATE.format(
                item_id=item_id,
                media_source_id=media_source_id,
                index=index,
                format=subtitle_format,
            )
        )
        try:
            response = self._send_request("GET", url, headers={"Accept": "*/*"})
            if response.status_code == 404:
                return None
            response.raise_for_status()
        except requests.RequestException as error:
            raise self._wrap_request_error("GET", url, error) from error

        return response.content or None

    def upload_item_subtitle(
        self,
        item_id: str,
        *,
        language: str,
        subtitle_format: str,
        is_forced: bool,
        is_hearing_impaired: bool,
        subtitle_bytes: bytes,
    ) -> None:
        """Upload one subtitle file to an item, adding it alongside existing tracks.

        Jellyfin's subtitle-upload endpoint expects the subtitle bytes
        base64-encoded in the request body, mirroring
        :meth:`upload_item_image`.

        Args:
            item_id: Jellyfin item identifier to update.
            language: Subtitle language code (e.g. ``"eng"``).
            subtitle_format: Subtitle file format/extension (e.g. ``"srt"``).
            is_forced: Whether the subtitle should be marked forced.
            is_hearing_impaired: Whether the subtitle should be marked
                hearing-impaired (SDH).
            subtitle_bytes: Raw, non-encoded subtitle file bytes.
        """
        self._request_payload(
            "POST",
            ITEM_SUBTITLE_UPLOAD_ENDPOINT_TEMPLATE.format(item_id=item_id),
            json_body={
                "Language": language,
                "Format": subtitle_format,
                "IsForced": is_forced,
                "IsHearingImpaired": is_hearing_impaired,
                "Data": base64.b64encode(subtitle_bytes).decode("ascii"),
            },
        )

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
                    media_item = self._resolve_missing_episode_number(media_item)
                    media_item = self._resolve_stale_title(media_item, raw_item)
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

    def _lookup_index_number(self, item_id: str) -> int | None:
        """Return one item's current IndexNumber via a direct per-item lookup.

        Jellyfin's /Items listing endpoint (whole-library or scoped to a
        ParentId) can lag behind a field edit made outside a normal library
        scan - a direct API PATCH (as apply_episode_numbers.py and
        apply_dvd_metadata.py do) or a manual edit in the Jellyfin UI - in
        either direction: it can omit an IndexNumber that is actually set,
        or keep showing a value that was since cleared. A per-item GET reads
        the current value directly and isn't affected by this lag.
        """
        detail = self.get_item(item_id)
        return self._get_optional_int(detail, "IndexNumber")

    def _resolve_missing_episode_number(self, item: MediaItem) -> MediaItem:
        """Re-check one episode's IndexNumber with a per-item lookup.

        Only used for the whole-library listing, where re-checking every
        episode individually would be too expensive - so this only covers
        the listing-says-missing-but-isn't direction, which is the one that
        matters for an audit run (it would otherwise misreport an episode as
        missing a number even though Jellyfin has it).

        Args:
            item: A media item just parsed from the whole-library listing.

        Returns:
            ``item`` unchanged, unless it is an episode with no
            episode_number and a per-item lookup finds one - in which case
            a copy with that episode_number is returned.
        """
        if not item.is_episode or item.episode_number is not None:
            return item

        episode_number = self._lookup_index_number(item.id)
        if episode_number is None:
            return item
        return replace(item, episode_number=episode_number)

    def _resolve_stale_title(self, item: MediaItem, raw_item: Mapping[str, Any]) -> MediaItem:
        """Re-check one item's Name with a per-item lookup if it's locked.

        Mirrors _resolve_missing_episode_number()'s reasoning for the same
        underlying cause: the /Items listing can lag behind a field edit
        made outside a normal library scan - a direct API PATCH (as
        apply_dvd_metadata.py, apply_episode_titles.py, and
        apply_titles_from_filename.py all do when renaming an item) or a
        manual edit in the Jellyfin UI. Every one of those tools locks Name
        immediately after changing it specifically so a metadata provider's
        next refresh can't silently revert it, so a locked Name is exactly
        the item this lag would otherwise misreport as still holding its
        pre-rename title, even though Jellyfin's own UI already shows the
        new one. Re-checking every item's Name would be too expensive for a
        whole-library listing, so this only re-checks ones already flagged
        as locked - a small fraction of a real library.

        Args:
            item: A media item just parsed from the whole-library listing.
            raw_item: That same item's raw JSON, for its LockedFields.

        Returns:
            ``item`` unchanged, unless Name is locked and a per-item lookup
            finds a different current value - in which case a copy with
            that title is returned.
        """
        locked_fields = self._get_optional_list(raw_item, "LockedFields", "media item")
        if "Name" not in locked_fields:
            return item

        detail = self.get_item(item.id)
        current_name = self._get_optional_str(detail, "Name")
        if current_name is None or current_name == item.title:
            return item
        return replace(item, title=current_name)

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
        json_body: Any = None,
    ) -> Any:
        """Perform a Jellyfin REST request and decode any JSON response body."""
        url = self._build_url(path)

        try:
            response = self._send_request(method, url, params=params, json=json_body)
            response.raise_for_status()
        except requests.RequestException as error:
            raise self._wrap_request_error(method, url, error) from error

        if not response.content:
            return None

        try:
            return response.json()
        except ValueError as error:
            raise JellyfinResponseError(
                f"Jellyfin returned invalid JSON for {method} {url}."
            ) from error

    def _send_request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Send an HTTP request, retrying on connection failures and timeouts.

        Jellyfin can intermittently time out or drop the connection under
        load; a short exponential backoff retry recovers from those without
        the caller having to notice. HTTP error status codes are left to the
        caller (via ``response.raise_for_status()``) since those are not
        necessarily transient.

        Args:
            method: HTTP method to use.
            url: Absolute URL to request.
            kwargs: Extra keyword arguments forwarded to
                :meth:`requests.Session.request` (e.g. ``params``, ``json``,
                ``data``, ``headers``).

        Returns:
            The HTTP response, which may still carry a non-2xx status code
            for the caller to handle.

        Raises:
            requests.RequestException: If every retry attempt fails with a
                connection error or timeout.
        """
        LOGGER.debug("Jellyfin %s %s", method, url)
        for attempt in range(1, self._max_retries + 1):
            try:
                self._request_count += 1
                return self._session.request(
                    method=method, url=url, timeout=self._timeout, **kwargs
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
                if attempt >= self._max_retries:
                    raise
                self._log_retry(attempt, method, url, str(error))

        raise AssertionError("unreachable: retry loop always returns or raises")

    def _log_retry(self, attempt: int, method: str, url: str, reason: str) -> None:
        """Log a retry attempt and sleep for an exponential backoff delay."""
        delay = self._retry_backoff_seconds * (2 ** (attempt - 1))
        LOGGER.warning(
            "Jellyfin request failed (attempt %d/%d): %s %s: %s; retrying in %.1fs",
            attempt,
            self._max_retries,
            method,
            url,
            reason,
            delay,
        )
        time.sleep(delay)

    @classmethod
    def _wrap_request_error(
        cls, method: str, url: str, error: requests.RequestException
    ) -> JellyfinRequestError:
        """Return a JellyfinRequestError describing a failed HTTP request."""
        if isinstance(error, requests.HTTPError):
            status_code = error.response.status_code if error.response is not None else "?"
            error_detail = cls._error_response_detail(error.response)
            detail_suffix = f": {error_detail}" if error_detail else ""
            return JellyfinRequestError(
                f"Jellyfin request failed with status {status_code}: {method} {url}{detail_suffix}"
            )
        return JellyfinRequestError(f"Jellyfin request failed: {method} {url}: {error}")

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
        index = self._get_optional_int(stream_data, "Index")
        if index is None:
            raise JellyfinResponseError(
                "Jellyfin returned a subtitle stream with no Index."
            )
        return SubtitleTrack(
            language=self._get_optional_str(stream_data, "Language") or "",
            codec=self._get_optional_str(stream_data, "Codec"),
            is_external=self._get_bool(stream_data, "IsExternal"),
            is_default=self._get_bool(stream_data, "IsDefault"),
            is_forced=self._get_bool(stream_data, "IsForced"),
            index=index,
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
            title=self._get_optional_str(stream_data, "Title")
            or self._get_optional_str(stream_data, "DisplayTitle"),
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
    "EpisodeSummary",
    "JellyfinClient",
    "JellyfinConfigurationError",
    "JellyfinError",
    "JellyfinRequestError",
    "JellyfinResponseError",
    "SeasonEpisodeSummary",
    "SeriesMatch",
]
