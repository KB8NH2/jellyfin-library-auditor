#!/usr/bin/python3
"""Application orchestration for Jellyfin Library Auditor.

This module coordinates configuration loading, Jellyfin access, command-line
options, and audit execution. It does not contain audit rules, filesystem
logic, or report formatting.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass

from audit import audit_media_item
from audit_types import AuditCategory
from audit_types import AuditFinding
from audit_types import AuditSeverity
from config import ConfigError
from config import ProcessingConfig
from config import get_config
from jellyfin import JellyfinClient
from jellyfin import JellyfinError
from jellyfin import JellyfinRequestError
from media import has_english_subtitles
from media import local_backdrop_exists
from media import local_nfo_exists
from media import local_poster_exists
from models import MediaLibrary
from reports import write_csv_report, write_html_report
from results import AuditServerResult
from results import LibraryAuditResult


LOGGER = logging.getLogger(__name__)


class CommandLineUsageError(ValueError):
    """Raised when command-line arguments are valid syntactically but unusable."""


@dataclass(frozen=True, slots=True)
class AuditRunOptions:
    """Normalized command-line options for an audit run."""

    write_csv: bool
    write_html: bool
    library_names: tuple[str, ...]
    categories: frozenset[AuditCategory] | None
    severities: frozenset[AuditSeverity] | None


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


def audit_server(
    requested_library_names: Iterable[str] = (),
) -> AuditServerResult:
    """Audit all enabled movie and TV libraries on the configured server.

    Args:
        requested_library_names: Optional library names that restrict the audit
            scope to matching enabled Jellyfin libraries.

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
        selected_libraries = _select_audit_libraries(
            libraries,
            config.processing,
            requested_library_names=requested_library_names,
        )

        findings: list[AuditFinding] = []
        library_results: list[LibraryAuditResult] = []
        media_items_processed = 0

        for library in selected_libraries:
            LOGGER.info("Auditing library %s...", library.name)
            library_result = _audit_library_result(client, library)
            library_results.append(library_result)
            media_items_processed += library_result.media_items_processed
            findings.extend(library_result.findings)

    return AuditServerResult(
        libraries_audited=len(selected_libraries),
        media_items_processed=media_items_processed,
        library_results=tuple(library_results),
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


def parse_args(argv: Sequence[str] | None = None) -> AuditRunOptions:
    """Parse command-line arguments into normalized run options.

    Args:
        argv: Optional argument list for testing or embedding.

    Returns:
        Parsed and normalized audit run options.

    Raises:
        CommandLineUsageError: If argument values cannot be used.
    """
    parser = _build_argument_parser()

    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as error:
        raise CommandLineUsageError(str(error)) from error

    report_flags_selected = args.csv or args.html
    return AuditRunOptions(
        write_csv=args.csv or not report_flags_selected,
        write_html=args.html or not report_flags_selected,
        library_names=_normalize_requested_library_names(args.library),
        categories=_parse_categories(args.category),
        severities=_parse_severities(args.severity),
    )


def filter_audit_result(
    result: AuditServerResult,
    *,
    categories: frozenset[AuditCategory] | None = None,
    severities: frozenset[AuditSeverity] | None = None,
) -> AuditServerResult:
    """Return a copy of server results filtered by category and severity."""
    findings = tuple(
        finding
        for finding in result.findings
        if (categories is None or finding.category in categories)
        and (severities is None or finding.severity in severities)
    )
    return AuditServerResult(
        libraries_audited=result.libraries_audited,
        media_items_processed=result.media_items_processed,
        library_results=result.library_results,
        findings=findings,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the application audit workflow and return an exit code."""
    configure_logging()

    try:
        options = parse_args(argv)
        result = audit_server(options.library_names)
        filtered_result = filter_audit_result(
            result,
            categories=options.categories,
            severities=options.severities,
        )

        if options.write_csv:
            write_csv_report(filtered_result)
        if options.write_html:
            write_html_report(filtered_result)
    except CommandLineUsageError as error:
        LOGGER.error("%s", error)
        return 2
    except (ConfigError, JellyfinError) as error:
        LOGGER.error("%s", error)
        return 1
    except Exception:
        LOGGER.exception("Unexpected application failure.")
        return 1

    findings_by_category = summarize_findings(filtered_result.findings)
    LOGGER.info("Libraries audited: %d", filtered_result.libraries_audited)
    LOGGER.info("Media items processed: %d", filtered_result.media_items_processed)
    LOGGER.info("Total findings: %d", len(filtered_result.findings))
    _log_library_summaries(filtered_result.library_results)

    for category, count in sorted(findings_by_category.items(), key=lambda entry: entry[0]):
        LOGGER.info("Findings in %s: %d", category.value, count)

    return 0


def _audit_library_result(client: JellyfinClient, library: MediaLibrary) -> LibraryAuditResult:
    """Return full audit results for one library."""
    items = client.get_library_items(library.id)
    findings: list[AuditFinding] = []
    items_with_english_subtitles = 0
    items_with_local_nfo = 0
    items_with_local_poster = 0
    items_with_local_backdrop = 0

    for item in items:
        items_with_english_subtitles += int(has_english_subtitles(item))
        items_with_local_nfo += int(local_nfo_exists(item))
        items_with_local_poster += int(local_poster_exists(item))
        items_with_local_backdrop += int(local_backdrop_exists(item))
        findings.extend(audit_media_item(item))

    return LibraryAuditResult(
        library=library,
        media_items_processed=len(items),
        items_with_english_subtitles=items_with_english_subtitles,
        items_with_local_nfo=items_with_local_nfo,
        items_with_local_poster=items_with_local_poster,
        items_with_local_backdrop=items_with_local_backdrop,
        findings=tuple(findings),
    )


def _log_library_summaries(library_results: Iterable[LibraryAuditResult]) -> None:
    """Log per-library content coverage summaries."""
    for library_result in library_results:
        LOGGER.info(
            (
                "Library summary for %s: English subtitles %s, local NFO %s, "
                "posters %s, backdrop %s"
            ),
            library_result.library.name,
            _format_percentage(
                library_result.items_with_english_subtitles,
                library_result.media_items_processed,
            ),
            _format_percentage(
                library_result.items_with_local_nfo,
                library_result.media_items_processed,
            ),
            _format_percentage(
                library_result.items_with_local_poster,
                library_result.media_items_processed,
            ),
            _format_percentage(
                library_result.items_with_local_backdrop,
                library_result.media_items_processed,
            ),
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
    requested_library_names: Iterable[str] = (),
) -> tuple[MediaLibrary, ...]:
    """Filter libraries down to supported and enabled audit targets."""
    enabled_libraries = tuple(
        library
        for library in libraries
        if _is_enabled_library_type(library, processing)
    )

    requested_names_by_key = {
        name.casefold(): name for name in requested_library_names
    }
    if not requested_names_by_key:
        return enabled_libraries

    selected_libraries = tuple(
        library
        for library in enabled_libraries
        if library.name.casefold() in requested_names_by_key
    )
    selected_names = {library.name.casefold() for library in selected_libraries}
    missing_names = tuple(
        requested_names_by_key[key]
        for key in requested_names_by_key
        if key not in selected_names
    )
    if missing_names:
        available_names = ", ".join(
            sorted(library.name for library in enabled_libraries)
        ) or "none"
        requested_text = ", ".join(missing_names)
        raise CommandLineUsageError(
            f"Requested library selection did not match any enabled library: "
            f"{requested_text}. Available libraries: {available_names}."
        )

    return selected_libraries


def _build_argument_parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the auditor entrypoint."""
    parser = argparse.ArgumentParser(
        prog="auditor",
        description="Audit a Jellyfin library and write filtered reports.",
        exit_on_error=False,
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Write the HTML audit report. Defaults to enabled unless --csv/--html is used.",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Write the CSV audit report. Defaults to enabled unless --csv/--html is used.",
    )
    parser.add_argument(
        "--library",
        action="append",
        default=[],
        metavar="NAME",
        help="Limit auditing to a Jellyfin library name. Repeat the option for multiple libraries.",
    )
    parser.add_argument(
        "--category",
        action="append",
        choices=tuple(category.value for category in AuditCategory),
        default=[],
        metavar="CATEGORY",
        help="Filter findings by category. Repeat the option for multiple categories.",
    )
    parser.add_argument(
        "--severity",
        action="append",
        choices=tuple(severity.value for severity in AuditSeverity),
        default=[],
        metavar="SEVERITY",
        help="Filter findings by severity. Repeat the option for multiple severities.",
    )
    return parser


def _normalize_requested_library_names(names: Iterable[str]) -> tuple[str, ...]:
    """Return unique, non-empty library names preserving input order."""
    normalized_names: list[str] = []
    seen: set[str] = set()

    for name in names:
        normalized_name = name.strip()
        if not normalized_name:
            raise CommandLineUsageError("--library requires a non-empty library name.")

        key = normalized_name.casefold()
        if key in seen:
            continue

        normalized_names.append(normalized_name)
        seen.add(key)

    return tuple(normalized_names)


def _parse_categories(values: Iterable[str]) -> frozenset[AuditCategory] | None:
    """Parse category strings into enum values."""
    categories = frozenset(AuditCategory(value) for value in values)
    return categories or None


def _parse_severities(values: Iterable[str]) -> frozenset[AuditSeverity] | None:
    """Parse severity strings into enum values."""
    severities = frozenset(AuditSeverity(value) for value in values)
    return severities or None


def _format_percentage(count: int, total: int) -> str:
    """Return a display-friendly percentage with supporting counts."""
    if total <= 0:
        return "0.0% (0/0)"
    return f"{(count / total) * 100.0:.1f}% ({count}/{total})"


if __name__ == "__main__":
    raise SystemExit(main())
