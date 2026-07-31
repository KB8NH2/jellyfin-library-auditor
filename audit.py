"""Audit logic for normalized media items.

This module evaluates :class:`models.MediaItem` objects and returns structured
findings. It operates only on application models and helper functions from
``media.py``.
"""

from __future__ import annotations

from audit_types import AuditCategory
from audit_types import AuditFinding
from audit_types import AuditSeverity
from media import get_primary_audio_codec
from media import get_video_codec
from media import has_english_subtitles
from media import is_hdr
from media import local_backdrop_exists
from media import local_nfo_exists
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
        missing_nfo,
        unknown_video_codec,
        unknown_audio_codec,
        hdr_video,
    )
    findings: list[AuditFinding] = []

    for audit in audits:
        finding = audit(item)
        if finding is not None:
            findings.append(finding)

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


def missing_nfo(item: MediaItem) -> AuditFinding | None:
    """Return a finding when no NFO file exists.

    Args:
        item: Media item to evaluate.

    Returns:
        An informational finding, or ``None`` when an NFO file exists.
    """
    if local_nfo_exists(item):
        return None

    return _finding(
        item,
        category=AuditCategory.METADATA,
        severity=AuditSeverity.INFO,
        check_name="missing_nfo",
        message="No local NFO file was found.",
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


def hdr_video(item: MediaItem) -> AuditFinding | None:
    """Return an informational finding when the item is HDR.

    Args:
        item: Media item to evaluate.

    Returns:
        An informational finding, or ``None`` when the item is not HDR.
    """
    if not is_hdr(item):
        return None

    return _finding(
        item,
        category=AuditCategory.VIDEO,
        severity=AuditSeverity.INFO,
        check_name="hdr_video",
        message="The media item has HDR video.",
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
    "audit_media_item",
    "hdr_video",
    "missing_backdrop",
    "missing_english_subtitles",
    "missing_nfo",
    "missing_poster",
    "unknown_audio_codec",
    "unknown_video_codec",
]
