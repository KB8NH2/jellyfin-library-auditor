from dataclasses import dataclass

from audit_types import AuditFinding
from models import MediaItem
from models import MediaLibrary


@dataclass(frozen=True, slots=True)
class ComparisonSetting:
    """One display-ready configuration setting for comparison output."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class LibraryComparisonSettings:
    """Display-ready comparison settings for one Jellyfin library."""

    library_name: str
    settings: tuple[ComparisonSetting, ...]


@dataclass(frozen=True, slots=True)
class LibraryAuditResult:
    """Structured audit output for one library.

    Attributes:
        library: Library that was audited.
        media_items_processed: Number of media items evaluated.
        audited_items: Media items that were evaluated for the library.
        items_with_english_subtitles: Number of items with configured English
            subtitles.
        items_with_local_nfo: Number of items with a local NFO file.
        items_with_local_backdrop: Number of items with a local backdrop file.
        findings: Findings produced while auditing the library.
    """

    library: MediaLibrary
    media_items_processed: int
    audited_items: tuple[MediaItem, ...]
    items_with_english_subtitles: int
    items_with_local_nfo: int
    items_with_local_backdrop: int
    findings: tuple[AuditFinding, ...]

    @property
    def findings_count(self) -> int:
        return len(self.findings)

    @property
    def english_subtitles_percentage(self) -> float:
        return _percentage(self.items_with_english_subtitles, self.media_items_processed)

    @property
    def local_nfo_percentage(self) -> float:
        return _percentage(self.items_with_local_nfo, self.media_items_processed)

    @property
    def local_backdrop_percentage(self) -> float:
        return _percentage(self.items_with_local_backdrop, self.media_items_processed)


@dataclass(frozen=True, slots=True)
class AuditServerResult:
    """Structured audit output for one Jellyfin server run.

    Attributes:
        libraries_audited: Number of libraries that were audited.
        media_items_processed: Number of media items evaluated.
        library_results: Per-library audit results and summary metrics.
        server_key: Configured server key used for the audit.
        server_name: Jellyfin server display name when available.
        server_url: Jellyfin server URL used for the audit.
        findings: Findings produced while auditing the server.
        server_settings: Optional user-experience-oriented server settings for
            comparison reports.
        library_settings: Optional user-experience-oriented library settings for
            comparison reports.
    """

    libraries_audited: int
    media_items_processed: int
    library_results: tuple[LibraryAuditResult, ...]
    findings: tuple[AuditFinding, ...]
    server_key: str | None = None
    server_name: str | None = None
    server_url: str | None = None
    server_settings: tuple[ComparisonSetting, ...] = ()
    library_settings: tuple[LibraryComparisonSettings, ...] = ()

    @property
    def findings_count(self) -> int:
        return len(self.findings)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    @property
    def audited_items(self) -> tuple[MediaItem, ...]:
        """Return every media item evaluated across all audited libraries."""
        items: list[MediaItem] = []
        for library_result in self.library_results:
            items.extend(library_result.audited_items)
        return tuple(items)


def _percentage(count: int, total: int) -> float:
    """Return the percentage represented by count and total."""
    if total <= 0:
        return 0.0
    return (count / total) * 100.0


__all__ = [
    "AuditFinding",
    "AuditServerResult",
    "ComparisonSetting",
    "LibraryComparisonSettings",
    "LibraryAuditResult",
    "MediaItem",
]
