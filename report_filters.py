"""Helpers that suppress findings from user-facing report output."""

from __future__ import annotations

from results import AuditServerResult
from results import LibraryAuditResult


EXCLUDED_REPORT_CHECKS = frozenset({"missing_backdrop"})


def filter_report_output(result: AuditServerResult) -> AuditServerResult:
    """Return a copy of one server result with suppressed report findings removed."""
    library_results = tuple(
        _filter_library_result(library_result)
        for library_result in result.library_results
    )
    findings = tuple(
        finding
        for finding in result.findings
        if finding.check_name not in EXCLUDED_REPORT_CHECKS
    )
    return AuditServerResult(
        libraries_audited=result.libraries_audited,
        media_items_processed=result.media_items_processed,
        library_results=library_results,
        findings=findings,
        server_key=result.server_key,
        server_name=result.server_name,
        server_url=result.server_url,
        server_settings=result.server_settings,
        library_settings=result.library_settings,
    )


def _filter_library_result(library_result: LibraryAuditResult) -> LibraryAuditResult:
    """Return a per-library result with suppressed report findings removed."""
    findings = tuple(
        finding
        for finding in library_result.findings
        if finding.check_name not in EXCLUDED_REPORT_CHECKS
    )
    return LibraryAuditResult(
        library=library_result.library,
        media_items_processed=library_result.media_items_processed,
        audited_items=library_result.audited_items,
        items_with_english_subtitles=library_result.items_with_english_subtitles,
        items_with_local_nfo=library_result.items_with_local_nfo,
        items_with_local_poster=library_result.items_with_local_poster,
        items_with_local_backdrop=library_result.items_with_local_backdrop,
        findings=findings,
    )
