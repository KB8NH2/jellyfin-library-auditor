"""Shared audit enums and finding models.

This module is intentionally neutral so other modules can depend on it without
creating import cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from models import MediaItem


class AuditSeverity(StrEnum):
    """Severity levels for audit findings."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AuditCategory(StrEnum):
    """Categories used to group audit findings."""

    SUBTITLES = "subtitles"
    ARTWORK = "artwork"
    METADATA = "metadata"
    EPISODE_ORDER = "episode_order"
    VIDEO = "video"
    AUDIO = "audio"
    FILESYSTEM = "filesystem"


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """Represents one structured finding for a media item.

    Attributes:
        category: High-level area the finding belongs to.
        severity: Severity level of the finding.
        check_name: Stable name of the audit check that produced the finding.
        message: Human-readable finding description.
        media_item: Media item associated with the finding.
    """

    category: AuditCategory
    severity: AuditSeverity
    check_name: str
    message: str
    media_item: MediaItem


__all__ = [
    "AuditCategory",
    "AuditFinding",
    "AuditSeverity",
]
