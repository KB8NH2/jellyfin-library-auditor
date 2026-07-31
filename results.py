from dataclasses import dataclass

from audit_types import AuditFinding
from models import MediaLibrary


@dataclass(frozen=True, slots=True)
class LibraryAuditResult:
    """Structured audit output for one library.

    Attributes:
        library: Library that was audited.
        media_items_processed: Number of media items evaluated.
        findings: Findings produced while auditing the library.
    """

    library: MediaLibrary
    media_items_processed: int
    findings: tuple[AuditFinding, ...]

    @property
    def findings_count(self) -> int:
        return len(self.findings)


@dataclass(frozen=True, slots=True)
class AuditServerResult:
    """Structured audit output for one Jellyfin server run.

    Attributes:
        libraries_audited: Number of libraries that were audited.
        media_items_processed: Number of media items evaluated.
        findings: Findings produced while auditing the server.
    """

    libraries_audited: int
    media_items_processed: int
    findings: tuple[AuditFinding, ...]

    @property
    def findings_count(self) -> int:
        return len(self.findings)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)


__all__ = [
    "AuditFinding",
    "AuditServerResult",
    "LibraryAuditResult",
]
