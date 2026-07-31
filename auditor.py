"""Application orchestration for Jellyfin Library Auditor.

This module coordinates configuration loading, Jellyfin access, and audit
execution. It does not contain audit rules, filesystem logic, or report
formatting.
"""

from __future__ import annotations
from config import ProcessingConfig

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from audit import AuditCategory
from audit import AuditFinding
from audit import audit_media_item
from config import ConfigError
from config import get_config
from jellyfin import JellyfinClient
from jellyfin import JellyfinError
from jellyfin import JellyfinRequestError
from models import MediaLibrary


LOGGER = logging.getLogger(__name__)


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


def configure_logging() -> None:
    """Configure INFO-level application logging with timestamps."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def audit_library(client: JellyfinClient, library: MediaLibrary) -> tuple[AuditFinding, ...]:
    """Audit all media items in one library.

    Args:
        client: Jellyfin client used to retrieve media items.
        library: Library to audit.

    Returns:
        Every finding produced for the library.
    """
    return _audit_library_result(client, library).findings


def audit_server() -> AuditServerResult:
    """Audit all enabled movie and TV libraries on the configured server.

    Returns:
        Structured audit results for the server.

    Raises:
        ConfigError: If application configuration is invalid.
        JellyfinError: If Jellyfin cannot be reached or returns invalid data.
    """
    config = get_config()

    with JellyfinClient() as client:
        if not client.ping():
            raise JellyfinRequestError(
                f"Unable to reach Jellyfin server at {config.jellyfin.server_url}."
            )

        libraries = client.get_libraries()
        selected_libraries = _select_audit_libraries(libraries, config.processing)

        findings: list[AuditFinding] = []
        media_items_processed = 0

        for library in selected_libraries:
            LOGGER.info("Auditing library %s...", library.name)
            library_result = _audit_library_result(client, library)
            media_items_processed += library_result.media_items_processed
            findings.extend(library_result.findings)

    return AuditServerResult(
        libraries_audited=len(selected_libraries),
        media_items_processed=media_items_processed,
        findings=tuple(findings),
    )


def summarize_findings(findings: Iterable[AuditFinding]) -> dict[AuditCategory, int]:
    """Summarize findings by category.

    Args:
        findings: Findings to summarize.

    Returns:
        A dictionary keyed by audit category with finding counts.
    """
    summary: dict[AuditCategory, int] = {}

    for finding in findings:
        summary[finding.category] = summary.get(finding.category, 0) + 1

    return summary


def main() -> int:
    """Run the application audit workflow and return an exit code."""
    configure_logging()

    try:
        result = audit_server()
    except (ConfigError, JellyfinError) as error:
        LOGGER.error("%s", error)
        return 1
    except Exception:
        LOGGER.exception("Unexpected application failure.")
        return 1

    findings_by_category = summarize_findings(result.findings)
    LOGGER.info("Libraries audited: %d", result.libraries_audited)
    LOGGER.info("Media items processed: %d", result.media_items_processed)
    LOGGER.info("Total findings: %d", len(result.findings))

    for category, count in sorted(findings_by_category.items(), key=lambda entry: entry[0]):
        LOGGER.info("Findings in %s: %d", category.value, count)

    return 0


def _audit_library_result(client: JellyfinClient, library: MediaLibrary) -> LibraryAuditResult:
    """Return full audit results for one library."""
    items = client.get_library_items(library.id)
    findings: list[AuditFinding] = []

    for item in items:
        findings.extend(audit_media_item(item))

    return LibraryAuditResult(
        library=library,
        media_items_processed=len(items),
        findings=tuple(findings),
    )


def _is_enabled_library_type(library: MediaLibrary, processing: ProcessingConfig) -> bool:
    """Return whether a library should be audited for the current configuration."""
    if library.is_movie_library:
        return bool(processing.enable_movies)
    if library.is_tv_library:
        return bool(processing.enable_tv)
    return False


def _select_audit_libraries(
    libraries: Iterable[MediaLibrary],
    processing: ProcessingConfig,
) -> tuple[MediaLibrary, ...]:
    """Filter libraries down to supported and enabled audit targets."""
    return tuple(
        library
        for library in libraries
        if _is_enabled_library_type(library, processing)
    )


if __name__ == "__main__":
    raise SystemExit(main())
