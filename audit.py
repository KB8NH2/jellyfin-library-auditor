"""Audit logic for normalized media items.

This module evaluates :class:`models.MediaItem` objects and returns structured
findings. It operates only on application models and helper functions from
``media.py``.
"""

from __future__ import annotations

from collections.abc import Iterable

from audit_types import AuditCategory
from audit_types import AuditFinding
from audit_types import AuditSeverity
from media import get_primary_audio_codec
from media import get_video_codec
from media import has_english_subtitles
from media import has_jellyfin_primary_image
from media import local_backdrop_exists
from media import local_poster_exists
from models import MediaItem


def audit_media_item(item: MediaItem) -> tuple[AuditFinding, ...]:
    """Run all media item audits and collect findings.

    Args:
        item: Media item to evaluate.

    Returns:
        A tuple containing every finding produced for the media item.
    """
    audits = (
        missing_english_subtitles,
        missing_poster,
        missing_backdrop,
        missing_primary_image,
        unknown_video_codec,
        unknown_audio_codec,
    )
    findings: list[AuditFinding] = []

    for audit in audits:
        finding = audit(item)
        if finding is not None:
            findings.append(finding)

    return tuple(findings)


def audit_library_items(items: Iterable[MediaItem]) -> tuple[AuditFinding, ...]:
    """Run library-level audits that require multiple media items.

    Args:
        items: Media items from one audited library.

    Returns:
        A tuple containing findings derived from gaps across TV episodes.
    """
    items_tuple = tuple(items)
    findings: list[AuditFinding] = []
    findings.extend(missing_tv_series_seasons(items_tuple))
    findings.extend(missing_tv_season_episodes(items_tuple))
    return tuple(findings)


def missing_english_subtitles(item: MediaItem) -> AuditFinding | None:
    """Return a finding when no configured English subtitles exist.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when English subtitles are present.
    """
    if has_english_subtitles(item):
        return None

    return _finding(
        item,
        category=AuditCategory.SUBTITLES,
        severity=AuditSeverity.WARNING,
        check_name="missing_english_subtitles",
        message="No configured English subtitles were found.",
    )


def missing_poster(item: MediaItem) -> AuditFinding | None:
    """Return a finding when no local poster exists.

    Args:
        item: Media item to evaluate.

    Returns:
        An informational finding, or ``None`` when a local poster exists.
    """
    if local_poster_exists(item):
        return None

    return _finding(
        item,
        category=AuditCategory.ARTWORK,
        severity=AuditSeverity.INFO,
        check_name="missing_poster",
        message="No local poster file was found.",
    )


def missing_backdrop(item: MediaItem) -> AuditFinding | None:
    """Return a finding when no local backdrop exists.

    Args:
        item: Media item to evaluate.

    Returns:
        An informational finding, or ``None`` when a local backdrop exists.
    """
    if local_backdrop_exists(item):
        return None

    return _finding(
        item,
        category=AuditCategory.ARTWORK,
        severity=AuditSeverity.INFO,
        check_name="missing_backdrop",
        message="No local backdrop file was found.",
    )


def missing_primary_image(item: MediaItem) -> AuditFinding | None:
    """Return a finding when Jellyfin has no primary image for the item.

    Args:
        item: Media item to evaluate.

    Returns:
        An informational finding, or ``None`` when Jellyfin reports a primary
        image.
    """
    if has_jellyfin_primary_image(item):
        return None

    return _finding(
        item,
        category=AuditCategory.ARTWORK,
        severity=AuditSeverity.INFO,
        check_name="missing_primary_image",
        message="No Jellyfin primary image was found.",
    )


def unknown_video_codec(item: MediaItem) -> AuditFinding | None:
    """Return a finding when the primary video codec is missing or unknown.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when the video codec is known.
    """
    codec = get_video_codec(item)
    if codec not in {None, "unknown"}:
        return None

    return _finding(
        item,
        category=AuditCategory.VIDEO,
        severity=AuditSeverity.WARNING,
        check_name="unknown_video_codec",
        message="The primary video codec is missing or unknown.",
    )


def unknown_audio_codec(item: MediaItem) -> AuditFinding | None:
    """Return a finding when the primary audio codec is missing.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when an audio codec exists.
    """
    if get_primary_audio_codec(item) is not None:
        return None

    return _finding(
        item,
        category=AuditCategory.AUDIO,
        severity=AuditSeverity.WARNING,
        check_name="unknown_audio_codec",
        message="No primary audio codec was found.",
    )


def missing_tv_series_seasons(items: Iterable[MediaItem]) -> tuple[AuditFinding, ...]:
    """Return findings for series with missing numbered seasons.

    Args:
        items: Media items from one audited library.

    Returns:
        One finding per TV series with internal season-number gaps.
    """
    series_items: dict[str, list[MediaItem]] = {}
    for item in items:
        if not item.is_episode or not item.series_name:
            continue
        if item.season_number is None or item.season_number <= 0:
            continue
        series_items.setdefault(item.series_name, []).append(item)

    findings: list[AuditFinding] = []
    for _, grouped_items in sorted(series_items.items(), key=lambda entry: entry[0].casefold()):
        season_numbers = sorted({item.season_number for item in grouped_items if item.season_number is not None})
        missing_numbers = _missing_sequence_numbers(season_numbers)
        if not missing_numbers:
            continue
        representative = min(grouped_items, key=_episode_sort_key)
        findings.append(
            _finding(
                representative,
                category=AuditCategory.METADATA,
                severity=AuditSeverity.WARNING,
                check_name="missing_seasons",
                message=f"Missing seasons: {_format_missing_numbers(missing_numbers)}.",
            )
        )
    return tuple(findings)


def missing_tv_season_episodes(items: Iterable[MediaItem]) -> tuple[AuditFinding, ...]:
    """Return findings for seasons with missing numbered episodes.

    Args:
        items: Media items from one audited library.

    Returns:
        One finding per TV season with internal episode-number gaps.
    """
    season_items: dict[tuple[str, int], list[MediaItem]] = {}
    for item in items:
        if not item.is_episode or not item.series_name:
            continue
        if item.season_number is None or item.season_number <= 0:
            continue
        if item.episode_number is None or item.episode_number <= 0:
            continue
        season_items.setdefault((item.series_name, item.season_number), []).append(item)

    findings: list[AuditFinding] = []
    for _, grouped_items in sorted(
        season_items.items(),
        key=lambda entry: (entry[0][0].casefold(), entry[0][1]),
    ):
        episode_numbers = sorted(
            {item.episode_number for item in grouped_items if item.episode_number is not None}
        )
        missing_numbers = _missing_sequence_numbers(episode_numbers)
        if not missing_numbers:
            continue
        representative = min(grouped_items, key=_episode_sort_key)
        findings.append(
            _finding(
                representative,
                category=AuditCategory.METADATA,
                severity=AuditSeverity.WARNING,
                check_name="missing_episodes",
                message=f"Missing episodes: {_format_missing_numbers(missing_numbers)}.",
            )
        )
    return tuple(findings)


def _missing_sequence_numbers(numbers: Iterable[int]) -> tuple[int, ...]:
    """Return missing integers between the smallest and largest values."""
    sorted_numbers = sorted(set(numbers))
    if len(sorted_numbers) < 2:
        return ()

    missing_numbers: list[int] = []
    for previous, current in zip(sorted_numbers, sorted_numbers[1:]):
        if current - previous <= 1:
            continue
        missing_numbers.extend(range(previous + 1, current))
    return tuple(missing_numbers)


def _format_missing_numbers(numbers: Iterable[int]) -> str:
    """Return a compact string for missing number sequences."""
    sorted_numbers = sorted(set(numbers))
    if not sorted_numbers:
        return ""

    ranges: list[str] = []
    range_start = sorted_numbers[0]
    range_end = sorted_numbers[0]

    for number in sorted_numbers[1:]:
        if number == range_end + 1:
            range_end = number
            continue
        ranges.append(_format_number_range(range_start, range_end))
        range_start = number
        range_end = number

    ranges.append(_format_number_range(range_start, range_end))
    return ", ".join(ranges)


def _format_number_range(start: int, end: int) -> str:
    """Return one display range for missing season or episode numbers."""
    if start == end:
        return str(start)
    return f"{start}-{end}"


def _episode_sort_key(item: MediaItem) -> tuple[str, int, int, str]:
    """Return a stable sort key for episode representative selection."""
    return (
        (item.series_name or "").casefold(),
        item.season_number if item.season_number is not None else -1,
        item.episode_number if item.episode_number is not None else -1,
        item.title.casefold(),
    )


def _finding(
    item: MediaItem,
    *,
    category: AuditCategory,
    severity: AuditSeverity,
    check_name: str,
    message: str,
) -> AuditFinding:
    """Build an audit finding for a media item.

    Args:
        item: Media item associated with the finding.
        category: Finding category.
        severity: Finding severity.
        check_name: Stable audit check name.
        message: Human-readable description.

    Returns:
        A structured audit finding.
    """
    return AuditFinding(
        category=category,
        severity=severity,
        check_name=check_name,
        message=message,
        media_item=item,
    )


__all__ = [
    "AuditCategory",
    "AuditFinding",
    "AuditSeverity",
    "audit_library_items",
    "audit_media_item",
    "missing_backdrop",
    "missing_english_subtitles",
    "missing_tv_season_episodes",
    "missing_tv_series_seasons",
    "missing_primary_image",
    "missing_poster",
    "unknown_audio_codec",
    "unknown_video_codec",
]
