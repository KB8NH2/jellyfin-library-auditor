"""Media and filesystem helper functions for normalized media items.

This module works only with application models and local filesystem state. It
does not talk to the Jellyfin API, generate reports, or apply audit-specific
rules.
"""

from __future__ import annotations

from pathlib import Path

from config import get_config
from models import AudioTrack
from models import MediaItem
from models import SubtitleTrack
from models import VideoTrack


POSTER_FILENAMES = (
    "poster.jpg",
    "poster.png",
    "folder.jpg",
    "folder.png",
)
BACKDROP_FILENAMES = (
    "backdrop.jpg",
    "backdrop.png",
    "fanart.jpg",
    "fanart.png",
)
GENERIC_NFO_FILENAMES = (
    "movie.nfo",
    "tvshow.nfo",
    "season.nfo",
)
PRIMARY_IMAGE_TAG = "Primary"
BACKDROP_IMAGE_TAG = "Backdrop"
LOGO_IMAGE_TAG = "Logo"
THUMB_IMAGE_TAG = "Thumb"
JELLYFIN_IMAGE_TAGS = (
    BACKDROP_IMAGE_TAG,
    LOGO_IMAGE_TAG,
    PRIMARY_IMAGE_TAG,
    THUMB_IMAGE_TAG,
)


def _configured_english_language_codes() -> frozenset[str]:
    """Return the configured set of English language codes."""
    config = get_config()
    return frozenset(config.reporting.english_language_codes)


def _item_directory(item: MediaItem) -> Path:
    """Return the directory containing the media item."""
    return item.path.parent


def _sibling_file_exists(item: MediaItem, filenames: tuple[str, ...]) -> bool:
    """Return whether any of the named sibling files exists.

    Args:
        item: Media item whose directory should be searched.
        filenames: Candidate filenames to look for.

    Returns:
        ``True`` when any matching file exists.
    """
    item_directory = _item_directory(item)
    if not item_directory.is_dir():
        return False

    return any((item_directory / filename).is_file() for filename in filenames)


def _normalize_for_prefix_match(value: str) -> str:
    """Normalize a path string for prefix matching."""
    return value.replace("/", "\\").casefold()


def _strip_configured_prefix(path: Path, prefix: str) -> str:
    """Strip a configured display prefix from a path when it matches cleanly.

    Args:
        path: Full media path.
        prefix: Configured path prefix to remove.

    Returns:
        The display path with the configured prefix removed when present.
    """
    full_path = str(path)
    if not prefix:
        return full_path

    normalized_path = _normalize_for_prefix_match(full_path)
    normalized_prefix = _normalize_for_prefix_match(prefix).rstrip("\\")

    if normalized_path == normalized_prefix:
        return ""

    prefix_with_separator = f"{normalized_prefix}\\"
    if normalized_path.startswith(prefix_with_separator):
        stripped_path = full_path[len(prefix.rstrip("\\/")):]
        return stripped_path.lstrip("\\/")

    return full_path


def _track_language_matches(track: SubtitleTrack, language_codes: frozenset[str]) -> bool:
    """Return whether a subtitle track language is in the configured set."""
    return track.language in language_codes


def _has_jellyfin_image_tag(item: MediaItem, tag_name: str) -> bool:
    """Return whether Jellyfin reported a non-empty image tag for the item.

    Args:
        item: Media item to inspect.
        tag_name: Image tag name from Jellyfin.

    Returns:
        ``True`` when the image tag exists and is non-empty.
    """
    tag_value = item.image_tags.get(tag_name)
    if tag_value is None:
        return False

    return bool(tag_value.strip())


# Subtitle helpers


def has_english_subtitles(item: MediaItem) -> bool:
    """Return whether the item has any configured English subtitle tracks.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when at least one subtitle language matches the configured
        English language codes.
    """
    return bool(get_english_subtitle_tracks(item))


def has_external_subtitles(item: MediaItem) -> bool:
    """Return whether the item has any external subtitle tracks.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when at least one subtitle track is external.
    """
    return item.has_external_subtitles


def has_embedded_subtitles(item: MediaItem) -> bool:
    """Return whether the item has any embedded subtitle tracks.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when at least one subtitle track is not external.
    """
    return any(not track.is_external for track in item.subtitle_tracks)


def get_english_subtitle_tracks(item: MediaItem) -> tuple[SubtitleTrack, ...]:
    """Return subtitle tracks whose language matches configured English codes.

    Args:
        item: Media item to inspect.

    Returns:
        A tuple of matching subtitle tracks.
    """
    language_codes = _configured_english_language_codes()
    return tuple(
        track
        for track in item.subtitle_tracks
        if _track_language_matches(track, language_codes)
    )


# Artwork helpers


def has_jellyfin_primary_image(item: MediaItem) -> bool:
    """Return whether Jellyfin reports a primary image tag for the item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a non-empty ``Primary`` image tag is present.
    """
    return _has_jellyfin_image_tag(item, PRIMARY_IMAGE_TAG)


def has_jellyfin_backdrop(item: MediaItem) -> bool:
    """Return whether Jellyfin reports a backdrop image tag for the item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a non-empty ``Backdrop`` image tag is present.
    """
    return _has_jellyfin_image_tag(item, BACKDROP_IMAGE_TAG)


def has_jellyfin_logo(item: MediaItem) -> bool:
    """Return whether Jellyfin reports a logo image tag for the item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a non-empty ``Logo`` image tag is present.
    """
    return _has_jellyfin_image_tag(item, LOGO_IMAGE_TAG)


def has_jellyfin_thumb(item: MediaItem) -> bool:
    """Return whether Jellyfin reports a thumb image tag for the item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a non-empty ``Thumb`` image tag is present.
    """
    return _has_jellyfin_image_tag(item, THUMB_IMAGE_TAG)


def jellyfin_image_types(item: MediaItem) -> tuple[str, ...]:
    """Return the sorted Jellyfin image tag types reported for the item.

    Args:
        item: Media item to inspect.

    Returns:
        A tuple containing each known Jellyfin image tag type whose value exists
        and is non-empty, sorted alphabetically.
    """
    return tuple(
        sorted(
            tag_name
            for tag_name in JELLYFIN_IMAGE_TAGS
            if _has_jellyfin_image_tag(item, tag_name)
        )
    )


def local_poster_exists(item: MediaItem) -> bool:
    """Return whether a common local poster file exists beside the media item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a common poster filename exists.
    """
    return _sibling_file_exists(item, POSTER_FILENAMES)


def local_backdrop_exists(item: MediaItem) -> bool:
    """Return whether a common local backdrop file exists beside the item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a common backdrop filename exists.
    """
    return _sibling_file_exists(item, BACKDROP_FILENAMES)


# Metadata helpers


def local_nfo_exists(item: MediaItem) -> bool:
    """Return whether a common local NFO file exists beside the media item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a known NFO filename exists in the media directory.
    """
    basename_nfo = f"{item.path.stem}.nfo"
    filenames = (*GENERIC_NFO_FILENAMES, basename_nfo)
    return _sibling_file_exists(item, filenames)


# Video helpers


def get_video_codec(item: MediaItem) -> str | None:
    """Return the normalized primary video codec for the item.

    Args:
        item: Media item to inspect.

    Returns:
        The primary video codec, or ``None`` when no video track exists.
    """
    video_track: VideoTrack | None = item.video_track
    if video_track is None:
        return None

    return video_track.codec


def is_hdr(item: MediaItem) -> bool:
    """Return whether the item's primary video track is HDR.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when the primary video track is HDR.
    """
    video_track = item.video_track
    if video_track is None:
        return False

    return video_track.hdr


def get_resolution(item: MediaItem) -> str | None:
    """Return the display-friendly video resolution for the item.

    Args:
        item: Media item to inspect.

    Returns:
        A resolution label such as ``"2160p"`` or ``None`` when unavailable.
    """
    return item.resolution


# Audio helpers


def get_primary_audio_codec(item: MediaItem) -> str | None:
    """Return the codec of the first available audio track.

    Args:
        item: Media item to inspect.

    Returns:
        The primary audio codec, or ``None`` when no audio track exists.
    """
    primary_audio_track: AudioTrack | None = next(iter(item.audio_tracks), None)
    if primary_audio_track is None:
        return None

    return primary_audio_track.codec


def get_audio_languages(item: MediaItem) -> tuple[str, ...]:
    """Return distinct audio languages in first-seen order.

    Args:
        item: Media item to inspect.

    Returns:
        A tuple of normalized audio language codes.
    """
    seen_languages: set[str] = set()
    languages: list[str] = []

    for track in item.audio_tracks:
        if track.language in seen_languages:
            continue

        seen_languages.add(track.language)
        languages.append(track.language)

    return tuple(languages)


# Filesystem helpers


def get_display_path(item: MediaItem) -> str:
    """Return the media path with the configured display prefix removed.

    Args:
        item: Media item to format.

    Returns:
        The media path with ``REPORT_MEDIA_PATH_PREFIX`` removed from the start
        when present.
    """
    media_path_prefix = get_config().reporting.media_path_prefix
    return _strip_configured_prefix(item.path, media_path_prefix)


def media_files(item: MediaItem) -> tuple[Path, ...]:
    """Return media-adjacent files that share the same basename.

    Args:
        item: Media item to inspect.

    Returns:
        All files in the item's directory whose names match the media basename,
        sorted alphabetically.
    """
    item_directory = _item_directory(item)
    if not item_directory.is_dir():
        return ()

    basename = item.path.stem
    basename_prefix = f"{basename}"

    matching_files = [
        path
        for path in item_directory.iterdir()
        if path.is_file()
        and (path.name == basename or path.name.startswith(basename_prefix))
    ]
    matching_files.sort(key=lambda path: path.name.casefold())
    return tuple(matching_files)
