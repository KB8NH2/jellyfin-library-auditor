#!/usr/bin/python3
"""Application orchestration for Jellyfin Library Auditor.

This module coordinates configuration loading, Jellyfin access, command-line
options, and audit execution. It does not contain audit rules, filesystem
logic, or report formatting.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import timedelta
import logging
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from audit import audit_episode_ordering
from audit import audit_library_items
from audit import audit_media_item
from audit_types import AuditCategory
from audit_types import AuditFinding
from audit_types import AuditSeverity
from comparison import ImageTransferResult
from comparison import MetadataTransferResult
from comparison import SubtitleTransferResult
from comparison import comparison_summary_counts
from comparison import mismatched_metadata_transfer_targets
from comparison import missing_image_transfer_targets
from comparison import missing_subtitle_transfer_targets
from comparison import write_comparison_reports
from config import ConfigError
from config import ProcessingConfig
from config import get_config
from config import ServerConfig
from jellyfin import JellyfinClient
from jellyfin import JellyfinError
from jellyfin import JellyfinRequestError
from media import configured_english_language_codes
from media import has_english_subtitles
from media import local_backdrop_exists
from media import local_nfo_exists
from models import MediaItem
from models import MediaLibrary
from output_layout import audit_results_root
from output_layout import reset_audit_results_root
from output_layout import write_audit_results_index
from report_filters import filter_report_output
from reports import write_csv_report, write_html_report
from results import AuditServerResult
from results import LibraryAuditResult
from tvdb import TvdbClient
from tvdb import TvdbEpisode
from tvdb import TvdbEpisodeCache
from tvdb import TvdbError
import transfer_images
import transfer_metadata
import transfer_subtitles


LOGGER = logging.getLogger("auditor")
AUTO_COMPARE_SENTINEL = "__auto_compare__"

# Written alongside the per-transfer-type log files whenever at least one
# --transfer-metadata/--transfer-images/--transfer-subtitles flag is used, so
# audit progress, comparison writing, and --verify output land in their own
# file instead of being mixed into a log meant to be a clean per-transfer-type
# record.
AUDIT_LOG_FILE = Path("audit.log")

# The bulk --transfer-images run only attempts Primary: Backdrop and Thumb
# are rarely populated on these libraries' source servers in practice, so
# attempting them on every candidate item was pure wasted work (an API call
# per item for a type that's essentially always "no source image"). The
# standalone transfer_images.py CLI still offers all of transfer_images.
# IMAGE_TYPES for one-off testing.
BULK_IMAGE_TYPES = ("Primary",)


class CommandLineUsageError(ValueError):
    """Raised when command-line arguments are valid syntactically but unusable."""


@dataclass(frozen=True, slots=True)
class AuditRunOptions:
    """Normalized command-line options for an audit run."""

    server_key: str | None
    compare_server_key: str | None
    audit_all: bool
    write_csv: bool
    write_html: bool
    library_names: tuple[str, ...]
    categories: frozenset[AuditCategory] | None
    severities: frozenset[AuditSeverity] | None
    check_episode_order: bool
    refresh_tvdb_cache: bool
    transfer_metadata: bool
    transfer_metadata_dry_run: bool
    transfer_metadata_yes: bool
    transfer_images: bool
    transfer_subtitles: bool
    transfer_limit: int | None
    verify: bool


def configure_logging() -> None:
    """Configure INFO-level application logging with timestamps."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _add_file_handler(logger: logging.Logger, log_file: Path) -> None:
    """Attach a timestamped file handler to a logger."""
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(file_handler)


def _enable_general_file_logging() -> None:
    """Persist this run's non-transfer log output to the shared audit log file.

    Only attached when at least one --transfer-metadata/--transfer-images/
    --transfer-subtitles flag is used, matching the other
    _enable_*_file_logging() helpers below, so a plain audit run still only
    logs to the console. Attached to LOGGER specifically (not the per-type
    transfer loggers those helpers use), so this file only ever contains
    audit progress, comparison writing, and --verify output - not the
    per-transfer-type history each of those already gets its own file for.
    """
    _add_file_handler(LOGGER, AUDIT_LOG_FILE)


def _enable_metadata_transfer_file_logging() -> None:
    """Persist this run's metadata-transfer log output to its own log file.

    Only attached for --transfer-metadata runs, and only to
    transfer_metadata.LOGGER (not LOGGER), so the log file only ever contains
    metadata-transfer history rather than routine audit noise or another
    transfer type's output. Uses the same file transfer_metadata.py's own CLI
    writes to, so a nightly --compare --transfer-metadata run and a manual
    one-off transfer via the report's copy-command button share one audit
    trail.
    """
    _add_file_handler(transfer_metadata.LOGGER, transfer_metadata.METADATA_TRANSFER_LOG_FILE)


def _enable_image_transfer_file_logging() -> None:
    """Persist this run's image-transfer log output to its own log file.

    Mirrors _enable_metadata_transfer_file_logging(): only attached for
    --transfer-images runs, only to transfer_images.LOGGER, writing to the
    same file transfer_images.py's own CLI writes to, so a bulk
    --compare --transfer-images run and a manual one-off transfer share one
    audit trail.
    """
    _add_file_handler(transfer_images.LOGGER, transfer_images.IMAGE_TRANSFER_LOG_FILE)


def _enable_subtitle_transfer_file_logging() -> None:
    """Persist this run's subtitle-transfer log output to its own log file.

    Mirrors _enable_image_transfer_file_logging(): only attached for
    --transfer-subtitles runs, only to transfer_subtitles.LOGGER, writing to
    the same file transfer_subtitles.py's own CLI writes to, so a bulk
    --compare --transfer-subtitles run and a manual one-off transfer share
    one audit trail.
    """
    _add_file_handler(transfer_subtitles.LOGGER, transfer_subtitles.SUBTITLE_TRANSFER_LOG_FILE)


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
    server_key: str | None = None,
    requested_library_names: Iterable[str] = (),
    *,
    include_configuration_snapshot: bool = False,
    tvdb_client: TvdbClient | None = None,
) -> AuditServerResult:
    """Audit all enabled movie and TV libraries on the configured server.

    Args:
        server_key: Optional configured server key to audit.
        requested_library_names: Optional library names that restrict the audit
            scope to matching enabled Jellyfin libraries.
        include_configuration_snapshot: Whether to include extra server and
            library settings intended for comparison reporting.
        tvdb_client: Optional TheTVDB client. When provided, TV libraries are
            also checked for aired/DVD episode-ordering mismatches.

    Returns:
        Structured audit results for the server.

    Raises:
        ConfigError: If application configuration is invalid.
        JellyfinError: If Jellyfin cannot be reached or returns invalid data.
    """
    config = get_config()
    server = _select_server(config, server_key)

    with JellyfinClient(server, processing=config.processing) as client:
        if not client.ping():
            raise JellyfinRequestError(
                f"Unable to reach Jellyfin server at {server.url}."
            )
        server_name = client.get_server_name()
        server_display_name = server_name or server.name

        libraries = client.get_libraries()
        selected_libraries = _select_audit_libraries(
            libraries,
            config.processing,
            requested_library_names=requested_library_names,
        )

        findings: list[AuditFinding] = []
        library_results: list[LibraryAuditResult] = []
        media_items_processed = 0
        server_settings = ()
        library_settings = ()

        for library in selected_libraries:
            LOGGER.info(
                "Auditing library %s on server %s...",
                library.name,
                server_display_name,
            )
            library_result = _audit_library_result(client, library, tvdb_client=tvdb_client)
            library_results.append(library_result)
            media_items_processed += library_result.media_items_processed
            findings.extend(library_result.findings)

        if include_configuration_snapshot:
            selected_library_names = tuple(
                library.name for library in selected_libraries
            )
            server_settings = client.get_server_user_experience_settings()
            library_settings = client.get_library_user_experience_settings(
                selected_library_names
            )

    return AuditServerResult(
        libraries_audited=len(selected_libraries),
        media_items_processed=media_items_processed,
        library_results=tuple(library_results),
        findings=tuple(findings),
        server_key=server.key,
        server_name=server_name,
        server_url=server.url,
        server_settings=server_settings,
        library_settings=library_settings,
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

    any_transfer_flag = args.transfer_metadata or args.transfer_images or args.transfer_subtitles
    if args.transfer_metadata and args.compare is None:
        raise CommandLineUsageError("--transfer-metadata requires --compare.")
    if args.transfer_images and args.compare is None:
        raise CommandLineUsageError("--transfer-images requires --compare.")
    if args.transfer_subtitles and args.compare is None:
        raise CommandLineUsageError("--transfer-subtitles requires --compare.")
    if args.dry_run and not any_transfer_flag:
        raise CommandLineUsageError(
            "--dry-run requires --transfer-metadata, --transfer-images, or --transfer-subtitles."
        )
    if args.yes and not any_transfer_flag:
        raise CommandLineUsageError(
            "--yes requires --transfer-metadata, --transfer-images, or --transfer-subtitles."
        )
    if args.limit is not None and not any_transfer_flag:
        raise CommandLineUsageError(
            "--limit requires --transfer-metadata, --transfer-images, or --transfer-subtitles."
        )
    if args.limit is not None and args.limit < 1:
        raise CommandLineUsageError("--limit must be a positive integer.")
    if args.verify and not any_transfer_flag:
        raise CommandLineUsageError(
            "--verify requires --transfer-metadata, --transfer-images, or --transfer-subtitles."
        )
    if args.check_episode_order and not get_config().tvdb.api_key:
        raise CommandLineUsageError(
            "--check-episode-order requires api_key to be set in the [tvdb] table of servers.toml."
        )
    if args.refresh_tvdb_cache and not args.check_episode_order:
        raise CommandLineUsageError(
            "--refresh-tvdb-cache requires --check-episode-order."
        )

    report_flags_selected = args.csv or args.html
    return AuditRunOptions(
        server_key=_normalize_optional_server_key(args.server),
        compare_server_key=_normalize_optional_compare_server_key(args.compare),
        audit_all=bool(args.all),
        write_csv=args.csv or not report_flags_selected,
        write_html=args.html or not report_flags_selected,
        library_names=_normalize_requested_library_names(args.library),
        categories=_parse_categories(args.category),
        severities=_parse_severities(args.severity),
        check_episode_order=bool(args.check_episode_order),
        refresh_tvdb_cache=bool(args.refresh_tvdb_cache),
        transfer_metadata=bool(args.transfer_metadata),
        transfer_metadata_dry_run=bool(args.dry_run),
        transfer_metadata_yes=bool(args.yes),
        transfer_images=bool(args.transfer_images),
        transfer_subtitles=bool(args.transfer_subtitles),
        transfer_limit=args.limit,
        verify=bool(args.verify),
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
        server_key=result.server_key,
        server_name=result.server_name,
        server_url=result.server_url,
        server_settings=result.server_settings,
        library_settings=result.library_settings,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the application audit workflow and return an exit code."""
    configure_logging()

    try:
        options = parse_args(argv)
        if options.transfer_metadata or options.transfer_images or options.transfer_subtitles:
            _enable_general_file_logging()
        if options.transfer_metadata:
            _enable_metadata_transfer_file_logging()
        if options.transfer_images:
            _enable_image_transfer_file_logging()
        if options.transfer_subtitles:
            _enable_subtitle_transfer_file_logging()
        selected_server_keys, compare_server_key = _resolve_run_targets(options)
        include_configuration_snapshot = compare_server_key is not None
        tvdb_client_context = (
            TvdbClient(
                get_config().tvdb.api_key,
                cache=TvdbEpisodeCache(
                    ttl=timedelta(days=get_config().tvdb.cache_ttl_days),
                    force_refresh=options.refresh_tvdb_cache,
                ),
            )
            if options.check_episode_order
            else contextlib.nullcontext(None)
        )
        with tvdb_client_context as tvdb_client:
            results = tuple(
                audit_server(
                    server_key,
                    options.library_names,
                    include_configuration_snapshot=include_configuration_snapshot,
                    tvdb_client=tvdb_client,
                )
                for server_key in selected_server_keys
            )
        filtered_results = tuple(
            filter_report_output(
                filter_audit_result(
                    result,
                    categories=options.categories,
                    severities=options.severities,
                )
            )
            for result in results
        )
        compare_result: AuditServerResult | None = None
        output_root = None
        if compare_server_key is not None:
            compare_result = results[1]
            if results[0].server_key == compare_result.server_key:
                raise CommandLineUsageError("--compare must target a different server.")

        should_write_html_site = options.write_html or compare_result is not None
        if options.write_csv or should_write_html_site:
            output_root = audit_results_root(get_config().reporting.output.audit_html)
            reset_audit_results_root(output_root)

        if options.write_csv:
            for filtered_result in filtered_results:
                write_csv_report(filtered_result)
        if should_write_html_site:
            for filtered_result in filtered_results:
                write_html_report(filtered_result)
        transfer_exit_code = 0
        transfer_results: tuple[MetadataTransferResult, ...] | None = None
        if compare_result is not None and options.transfer_metadata:
            transfer_exit_code, transfer_results = _run_bulk_metadata_transfer(
                results[0],
                compare_result,
                dry_run=options.transfer_metadata_dry_run,
                assume_yes=options.transfer_metadata_yes,
                limit=options.transfer_limit,
            )
        image_transfer_exit_code = 0
        image_transfer_results: tuple[ImageTransferResult, ...] | None = None
        if compare_result is not None and options.transfer_images:
            image_transfer_exit_code, image_transfer_results = _run_bulk_image_transfer(
                results[0],
                compare_result,
                dry_run=options.transfer_metadata_dry_run,
                assume_yes=options.transfer_metadata_yes,
                limit=options.transfer_limit,
            )
        subtitle_transfer_exit_code = 0
        subtitle_transfer_results: tuple[SubtitleTransferResult, ...] | None = None
        if compare_result is not None and options.transfer_subtitles:
            subtitle_transfer_exit_code, subtitle_transfer_results = _run_bulk_subtitle_transfer(
                results[0],
                compare_result,
                dry_run=options.transfer_metadata_dry_run,
                assume_yes=options.transfer_metadata_yes,
                limit=options.transfer_limit,
            )
        transfer_exit_code = max(transfer_exit_code, image_transfer_exit_code, subtitle_transfer_exit_code)
        if compare_result is not None and options.verify:
            if options.transfer_metadata_dry_run:
                LOGGER.info(
                    "Skipping --verify: --dry-run did not write anything, so there is nothing to verify."
                )
            elif not _any_transfer_completed(
                transfer_results, image_transfer_results, subtitle_transfer_results
            ):
                LOGGER.info(
                    "Skipping --verify: no items were actually transferred, so there is nothing to verify."
                )
            else:
                compare_result = _verify_transfer_result(results[0], compare_result, options)
        if compare_result is not None:
            _write_comparison_site(
                results[0],
                compare_result,
                transfer_results=transfer_results,
                image_transfer_results=image_transfer_results,
                subtitle_transfer_results=subtitle_transfer_results,
            )
        if should_write_html_site:
            if output_root is None:
                raise RuntimeError("Audit output root was not initialized.")
            write_audit_results_index(
                output_root,
                filtered_results,
                include_comparison=compare_result is not None,
            )
    except CommandLineUsageError as error:
        LOGGER.error("%s", error)
        return 2
    except (ConfigError, JellyfinError) as error:
        LOGGER.error("%s", error)
        return 1
    except Exception:
        LOGGER.exception("Unexpected application failure.")
        return 1

    for filtered_result in filtered_results:
        findings_by_category = summarize_findings(filtered_result.findings)
        LOGGER.info(
            "Server audit summary for %s",
            filtered_result.server_name or filtered_result.server_key or "unknown",
        )
        LOGGER.info("Libraries audited: %d", filtered_result.libraries_audited)
        LOGGER.info("Media items processed: %d", filtered_result.media_items_processed)
        LOGGER.info("Total findings: %d", len(filtered_result.findings))
        _log_library_summaries(filtered_result.library_results)

        for category, count in sorted(findings_by_category.items(), key=lambda entry: entry[0]):
            LOGGER.info("%s Findings in %s: %d", filtered_result.server_name, category.value, count)

    return transfer_exit_code


def _audit_library_result(
    client: JellyfinClient,
    library: MediaLibrary,
    *,
    tvdb_client: TvdbClient | None = None,
) -> LibraryAuditResult:
    """Return full audit results for one library."""
    items = client.get_library_items(library.id)
    findings: list[AuditFinding] = []
    items_with_english_subtitles = 0
    items_with_local_nfo = 0
    items_with_local_backdrop = 0

    for item in items:
        items_with_english_subtitles += int(has_english_subtitles(item))
        items_with_local_nfo += int(local_nfo_exists(item))
        items_with_local_backdrop += int(local_backdrop_exists(item))
        findings.extend(audit_media_item(item))
    findings.extend(audit_library_items(items))
    if tvdb_client is not None and library.is_tv_library:
        findings.extend(_audit_episode_ordering(client, tvdb_client, library, items))

    return LibraryAuditResult(
        library=library,
        media_items_processed=len(items),
        audited_items=tuple(items),
        items_with_english_subtitles=items_with_english_subtitles,
        items_with_local_nfo=items_with_local_nfo,
        items_with_local_backdrop=items_with_local_backdrop,
        findings=tuple(findings),
    )


def _audit_episode_ordering(
    client: JellyfinClient,
    tvdb_client: TvdbClient,
    library: MediaLibrary,
    items: Iterable[MediaItem],
) -> tuple[AuditFinding, ...]:
    """Return aired/DVD episode-ordering findings for one TV library.

    Looks up each series' TheTVDB id once per library, then fetches both
    orderings only for series actually present in this library's items. A
    lookup failure for one series is logged and skipped rather than failing
    the whole audit run.
    """
    series_names = {item.series_name for item in items if item.is_episode and item.series_name}
    if not series_names:
        return ()

    series_tvdb_ids = client.get_series_tvdb_ids(library.id)

    aired_positions: dict[str, dict[tuple[int, int], TvdbEpisode]] = {}
    dvd_positions: dict[str, dict[tuple[int, int], TvdbEpisode]] = {}

    for series_name in series_names:
        tvdb_id = series_tvdb_ids.get(series_name)
        if tvdb_id is None:
            continue

        try:
            aired_episodes = tvdb_client.get_series_episodes(tvdb_id, "official")
            dvd_episodes = tvdb_client.get_series_episodes(tvdb_id, "dvd")
        except TvdbError as error:
            LOGGER.warning("Skipping episode-order check for %r: %s", series_name, error)
            continue

        aired_positions[series_name] = {
            (episode.season_number, episode.episode_number): episode for episode in aired_episodes
        }
        dvd_positions[series_name] = {
            (episode.season_number, episode.episode_number): episode for episode in dvd_episodes
        }

    return audit_episode_ordering(items, aired_positions, dvd_positions)


def _log_library_summaries(library_results: Iterable[LibraryAuditResult]) -> None:
    """Log per-library content coverage summaries."""
    for library_result in library_results:
        LOGGER.info(
            "Library summary for %s: English subtitles %s",
            library_result.library.name,
            _format_percentage(
                library_result.items_with_english_subtitles,
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
        "--server",
        metavar="SERVER",
        help="Select a configured server from servers.toml. Defaults to default_server.",
    )
    parser.add_argument(
        "--compare",
        nargs="?",
        const=AUTO_COMPARE_SENTINEL,
        metavar="SERVER",
        help=(
            "Compare the selected server against another configured server and "
            "generate comparison reports. When used without a value and without "
            "--server, the first two configured servers are compared."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Audit every configured server from servers.toml.",
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
    parser.add_argument(
        "--check-episode-order",
        action="store_true",
        help=(
            "Check TV episodes against TheTVDB's aired and DVD episode "
            "orderings and flag any episode whose title differs between the "
            "two at its season/episode position, since a series stored on "
            "disk in one order but labeled with the other still looks "
            "internally consistent to every other check. Requires api_key "
            "to be set in the [tvdb] table of servers.toml."
        ),
    )
    parser.add_argument(
        "--refresh-tvdb-cache",
        action="store_true",
        help=(
            "With --check-episode-order, ignore cached TheTVDB episode "
            "lookups and fetch fresh data for every series this run, still "
            "updating the cache with the results. Requires "
            "--check-episode-order."
        ),
    )
    parser.add_argument(
        "--transfer-metadata",
        action="store_true",
        help=(
            "Transfer metadata (title, overview, genres, provider IDs, etc.) for "
            "every item with mismatched metadata from the base --server to the "
            "--compare server. Requires --compare. Prompts once for confirmation "
            "before writing anything, unless --yes is given."
        ),
    )
    parser.add_argument(
        "--transfer-images",
        action="store_true",
        help=(
            "Transfer cached Jellyfin images (Primary, Backdrop, Thumb) for every "
            "item with an artwork difference from the base --server to the "
            "--compare server. Requires --compare. Prompts once for confirmation "
            "before writing anything, unless --yes is given."
        ),
    )
    parser.add_argument(
        "--transfer-subtitles",
        action="store_true",
        help=(
            "Transfer the English subtitle track for every item with a subtitle "
            "difference from the base --server to the --compare server, reading "
            "and writing entirely through the Jellyfin API so it also picks up "
            "subtitles stored in Jellyfin's internal metadata cache rather than "
            "next to the media file. Requires --compare. Prompts once for "
            "confirmation before writing anything, unless --yes is given."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "With --transfer-metadata/--transfer-images/--transfer-subtitles, "
            "preview planned transfers without writing anything."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "With --transfer-metadata/--transfer-images/--transfer-subtitles, "
            "skip the batch confirmation prompt."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help=(
            "With --transfer-metadata/--transfer-images/--transfer-subtitles, "
            "only attempt the first N items found, regardless of outcome. "
            "Useful for quickly testing code changes in bulk mode without "
            "waiting for a full run."
        ),
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "With --transfer-metadata/--transfer-images/--transfer-subtitles, "
            "re-audit the --compare server once transfers finish and write the "
            "comparison report from that post-transfer state, so it reflects "
            "what actually changed rather than the pre-transfer snapshot. "
            "Ignored with --dry-run, or if no item was actually transferred, "
            "since there is nothing to verify."
        ),
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


def _normalize_optional_server_key(server_key: str | None) -> str | None:
    """Normalize an optional server selection key."""
    if server_key is None:
        return None
    normalized_key = server_key.strip()
    if not normalized_key:
        raise CommandLineUsageError("--server requires a non-empty server key.")
    return normalized_key


def _normalize_optional_compare_server_key(server_key: str | None) -> str | None:
    """Normalize an optional compare selection key or auto-compare sentinel."""
    if server_key == AUTO_COMPARE_SENTINEL:
        return AUTO_COMPARE_SENTINEL
    if server_key is None:
        return None
    normalized_key = server_key.strip()
    if not normalized_key:
        raise CommandLineUsageError("--compare requires a non-empty server key.")
    return normalized_key


def _select_server(config, server_key: str | None) -> ServerConfig:
    """Return the configured server selected for this audit run."""
    if server_key is None:
        return config.servers.get_default()
    return config.servers.get(server_key)


def _write_comparison_site(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
    *,
    transfer_results: tuple[MetadataTransferResult, ...] | None = None,
    image_transfer_results: tuple[ImageTransferResult, ...] | None = None,
    subtitle_transfer_results: tuple[SubtitleTransferResult, ...] | None = None,
) -> None:
    """Write comparison reports for two audited servers."""
    if left_result.server_key == right_result.server_key:
        raise CommandLineUsageError("--compare must target a different server.")

    write_comparison_reports(
        left_result,
        right_result,
        transfer_results=transfer_results,
        image_transfer_results=image_transfer_results,
        subtitle_transfer_results=subtitle_transfer_results,
    )


def _any_transfer_completed(
    transfer_results: tuple[MetadataTransferResult, ...] | None,
    image_transfer_results: tuple[ImageTransferResult, ...] | None,
    subtitle_transfer_results: tuple[SubtitleTransferResult, ...] | None,
) -> bool:
    """Return whether at least one item across the transfer batches was written.

    A finding flagged as different doesn't always mean anything was actually
    sent - an item can be rejected, fail, or turn out to have no
    transferable field differences once the full item is read from both
    servers. Verifying re-audits the compare server, which is only worth the
    round-trip if a write actually happened.
    """
    for results in (transfer_results, image_transfer_results, subtitle_transfer_results):
        if results is None:
            continue
        if any(result.status == "transferred" for result in results):
            return True
    return False


def _verify_transfer_result(
    left_result: AuditServerResult,
    compare_result: AuditServerResult,
    options: AuditRunOptions,
) -> AuditServerResult:
    """Re-audit the --compare server after transfers finish and log what remains.

    Without this, the comparison report written at the end of a
    --transfer-metadata/--transfer-images/--transfer-subtitles run still
    reflects the compare server's state from before any of those writes
    happened, so it can't actually confirm whether the transfer resolved
    what it targeted.

    Args:
        left_result: Completed pre-transfer audit results for the base
            server, reused as-is since the base server was never written to.
        compare_result: Completed pre-transfer audit results for the compare
            server, used only to identify which server to re-audit.
        options: Parsed run options, for the library selection used the
            first time.

    Returns:
        Freshly audited results for the compare server.
    """
    right_label = compare_result.server_name or compare_result.server_key or "compare server"
    LOGGER.info("Verifying transfer results: re-auditing %s...", right_label)
    verified_result = audit_server(
        compare_result.server_key,
        options.library_names,
        include_configuration_snapshot=True,
    )
    summary_counts = comparison_summary_counts(left_result, verified_result)
    LOGGER.info(
        "Post-transfer comparison for %s: %d missing media, %d missing seasons, "
        "%d missing episodes, %d mismatched metadata, %d artwork differences, "
        "%d subtitle differences remaining.",
        right_label,
        summary_counts["missing_media"],
        summary_counts["missing_seasons"],
        summary_counts["missing_episodes"],
        summary_counts["mismatched_metadata"],
        summary_counts["artwork_differences"],
        summary_counts["subtitle_differences"],
    )
    return verified_result


def _run_bulk_metadata_transfer(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
    *,
    dry_run: bool,
    assume_yes: bool,
    limit: int | None = None,
) -> tuple[int, tuple[MetadataTransferResult, ...]]:
    """Transfer metadata for every mismatched-metadata item pair between two servers.

    Reuses the same source/destination pairing the comparison report shows,
    so this only ever acts on items the "Mismatched Metadata" table would
    also flag. Continues past a single item's failure or rejection rather
    than aborting the whole batch, logging a summary at the end.

    Args:
        left_result: Completed audit results for the source server.
        right_result: Completed audit results for the destination server.
        dry_run: Preview planned transfers without writing anything.
        assume_yes: Skip the batch confirmation prompt.
        limit: When given, only attempt the first N items found, regardless
            of outcome - for quickly testing bulk-mode changes.

    Returns:
        A tuple of (exit code, per-item results). Exit code is ``0`` when
        every attempted transfer succeeded (or nothing needed transferring),
        ``1`` if any item was rejected or failed.
    """
    logger = transfer_metadata.LOGGER
    targets = mismatched_metadata_transfer_targets(left_result, right_result)
    if limit is not None:
        targets = targets[:limit]
    left_label = left_result.server_name or left_result.server_key or "left"
    right_label = right_result.server_name or right_result.server_key or "right"

    if not targets:
        logger.info("No mismatched metadata found between %s and %s.", left_label, right_label)
        return 0, ()

    logger.info(
        "%s metadata for %d item(s) from %s to %s.",
        "Would transfer" if dry_run else "About to transfer",
        len(targets),
        left_label,
        right_label,
    )
    if not dry_run and not assume_yes:
        response = input(f"Transfer metadata for {len(targets)} item(s)? [y/N] ").strip().lower()
        if response not in {"y", "yes"}:
            logger.info("Aborted.")
            return 1, ()

    config = get_config()
    server_clients: dict[str, JellyfinClient] = {}
    results: list[MetadataTransferResult] = []
    transferred = 0
    unchanged = 0
    rejected = 0
    failed = 0

    try:
        for target in targets:
            try:
                from_client = _cached_jellyfin_client(server_clients, config, target.left_server_key)
                to_client = _cached_jellyfin_client(server_clients, config, target.right_server_key)
                plan = transfer_metadata.plan_transfer(
                    from_client, to_client, target.left_item_id, target.right_item_id
                )
            except (ConfigError, JellyfinError) as error:
                failed += 1
                logger.error(
                    "[%s] %s: failed to prepare transfer: %s",
                    target.library, target.display_name, error,
                )
                results.append(
                    MetadataTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="failed",
                        detail=str(error),
                    )
                )
                continue

            if plan.is_rejected:
                rejected += 1
                logger.error(
                    "[%s] %s: refusing to update - %s",
                    target.library, target.display_name, plan.rejected_reason,
                )
                results.append(
                    MetadataTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="rejected",
                        detail=plan.rejected_reason or "",
                    )
                )
                continue

            if not plan.has_changes:
                unchanged += 1
                logger.info("[%s] %s: no transferable fields differ.", target.library, target.display_name)
                results.append(
                    MetadataTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="unchanged",
                    )
                )
                continue

            changed_fields = tuple(field for field, _, _ in plan.changes)
            change_summary = ", ".join(changed_fields)
            if dry_run:
                transferred += 1
                logger.info("[%s] %s: would change %s", target.library, target.display_name, change_summary)
                results.append(
                    MetadataTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="would_transfer",
                        changed_fields=changed_fields,
                    )
                )
                continue

            try:
                transfer_metadata.apply_transfer(to_client, plan)
            except JellyfinError as error:
                failed += 1
                logger.error("[%s] %s: update failed: %s", target.library, target.display_name, error)
                results.append(
                    MetadataTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="failed",
                        detail=str(error),
                    )
                )
                continue

            transferred += 1
            logger.info("[%s] %s: transferred %s", target.library, target.display_name, change_summary)
            results.append(
                MetadataTransferResult(
                    library=target.library,
                    display_name=target.display_name,
                    status="transferred",
                    changed_fields=changed_fields,
                )
            )
    finally:
        for client in server_clients.values():
            client.close()

    logger.info(
        "Metadata transfer summary: %d transferred, %d unchanged, %d rejected, %d failed (of %d total).",
        transferred, unchanged, rejected, failed, len(targets),
    )
    exit_code = 1 if (rejected or failed) else 0
    return exit_code, tuple(results)


def _run_bulk_image_transfer(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
    *,
    dry_run: bool,
    assume_yes: bool,
    limit: int | None = None,
) -> tuple[int, tuple[ImageTransferResult, ...]]:
    """Transfer cached images for every artwork-differing item pair between two servers.

    Reuses the same source/destination pairing the comparison report shows,
    so this only ever acts on items the "Artwork Differences" table would
    also flag - that table includes a pair when Primary differs. Only fills
    in image types the destination is actually missing: each image type in
    BULK_IMAGE_TYPES is skipped with an "already_present" result (not
    attempted, not overwritten) when the destination already has one, and
    recorded "unavailable" (not a failure) when the source has none to give.
    Continues past a single item's failure rather than aborting the whole
    batch, logging a summary at the end.

    Args:
        left_result: Completed audit results for the source server.
        right_result: Completed audit results for the destination server.
        dry_run: Preview planned transfers without writing anything.
        assume_yes: Skip the batch confirmation prompt.
        limit: When given, only attempt the first N items found, regardless
            of outcome - for quickly testing bulk-mode changes.

    Returns:
        A tuple of (exit code, per-(item, image type) results). Exit code is
        ``0`` when every attempted transfer succeeded (or nothing needed
        transferring), ``1`` if any transfer failed.
    """
    logger = transfer_images.LOGGER
    targets = missing_image_transfer_targets(left_result, right_result)
    if limit is not None:
        targets = targets[:limit]
    left_label = left_result.server_name or left_result.server_key or "left"
    right_label = right_result.server_name or right_result.server_key or "right"

    if not targets:
        logger.info("No artwork differences found between %s and %s.", left_label, right_label)
        return 0, ()

    logger.info(
        "%s images for %d item(s) from %s to %s.",
        "Would transfer" if dry_run else "About to transfer",
        len(targets),
        left_label,
        right_label,
    )
    if not dry_run and not assume_yes:
        response = input(f"Transfer images for {len(targets)} item(s)? [y/N] ").strip().lower()
        if response not in {"y", "yes"}:
            logger.info("Aborted.")
            return 1, ()

    config = get_config()
    server_clients: dict[str, JellyfinClient] = {}
    results: list[ImageTransferResult] = []
    transferred = 0
    unavailable = 0
    already_present = 0
    failed = 0

    try:
        for target in targets:
            try:
                from_client = _cached_jellyfin_client(server_clients, config, target.left_server_key)
                to_client = _cached_jellyfin_client(server_clients, config, target.right_server_key)
            except ConfigError as error:
                failed += len(BULK_IMAGE_TYPES)
                logger.error(
                    "[%s] %s: failed to prepare transfer: %s",
                    target.library, target.display_name, error,
                )
                results.extend(
                    ImageTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        image_type=image_type,
                        status="failed",
                        detail=str(error),
                    )
                    for image_type in BULK_IMAGE_TYPES
                )
                continue

            try:
                destination_item = to_client.get_item(target.right_item_id)
            except JellyfinError as error:
                failed += len(BULK_IMAGE_TYPES)
                logger.error(
                    "[%s] %s: failed to read destination item %s: %s",
                    target.library, target.display_name, target.right_item_id, error,
                )
                results.extend(
                    ImageTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        image_type=image_type,
                        status="failed",
                        detail=str(error),
                    )
                    for image_type in BULK_IMAGE_TYPES
                )
                continue

            destination_name = str(destination_item.get("Name", target.right_item_id))
            if destination_name.casefold() != target.left_title.casefold():
                # Not necessarily wrong - mismatched_metadata already tracks
                # legitimate title differences between paired items - but
                # surfaced loudly since a bulk run has no other way to catch
                # a genuine pairing/item-id mistake before it writes. Compared
                # against left_title (the bare title, matching Jellyfin's own
                # "Name" field), not display_name, which is a composed label
                # ("Series - Season - S04E13 - Title") that would never match
                # Jellyfin's Name for an episode even when correctly paired.
                logger.warning(
                    "[%s] %s: destination item %s is named %r, not %r - verify this "
                    "pairing is correct before trusting this transfer.",
                    target.library, target.display_name, target.right_item_id,
                    destination_name, target.left_title,
                )

            for image_type in BULK_IMAGE_TYPES:
                if _has_image_of_type(destination_item, image_type):
                    already_present += 1
                    results.append(
                        ImageTransferResult(
                            library=target.library,
                            display_name=target.display_name,
                            image_type=image_type,
                            status="already_present",
                        )
                    )
                    continue

                try:
                    plan = transfer_images.plan_image_transfer(
                        from_client, to_client, target.left_item_id, target.right_item_id, image_type
                    )
                except JellyfinError as error:
                    failed += 1
                    logger.error(
                        "[%s] %s: failed to read %s image: %s",
                        target.library, target.display_name, image_type, error,
                    )
                    results.append(
                        ImageTransferResult(
                            library=target.library,
                            display_name=target.display_name,
                            image_type=image_type,
                            status="failed",
                            detail=str(error),
                        )
                    )
                    continue

                if not plan.has_image:
                    unavailable += 1
                    results.append(
                        ImageTransferResult(
                            library=target.library,
                            display_name=target.display_name,
                            image_type=image_type,
                            status="unavailable",
                        )
                    )
                    continue

                if dry_run:
                    transferred += 1
                    logger.info(
                        "[%s] %s: would transfer %s image", target.library, target.display_name, image_type
                    )
                    results.append(
                        ImageTransferResult(
                            library=target.library,
                            display_name=target.display_name,
                            image_type=image_type,
                            status="would_transfer",
                        )
                    )
                    continue

                try:
                    transfer_images.apply_image_transfer(to_client, plan)
                except JellyfinError as error:
                    failed += 1
                    logger.error(
                        "[%s] %s: %s image upload failed: %s",
                        target.library, target.display_name, image_type, error,
                    )
                    results.append(
                        ImageTransferResult(
                            library=target.library,
                            display_name=target.display_name,
                            image_type=image_type,
                            status="failed",
                            detail=str(error),
                        )
                    )
                    continue

                transferred += 1
                logger.info("[%s] %s: transferred %s image", target.library, target.display_name, image_type)
                results.append(
                    ImageTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        image_type=image_type,
                        status="transferred",
                    )
                )
    finally:
        for client in server_clients.values():
            client.close()

    logger.info(
        "Image transfer summary: %d transferred, %d already present, %d unavailable, "
        "%d failed (of %d item(s), %d image(s) attempted).",
        transferred, already_present, unavailable, failed, len(targets), len(results),
    )
    exit_code = 1 if failed else 0
    return exit_code, tuple(results)


def _run_bulk_subtitle_transfer(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
    *,
    dry_run: bool,
    assume_yes: bool,
    limit: int | None = None,
) -> tuple[int, tuple[SubtitleTransferResult, ...]]:
    """Transfer the English subtitle track for every subtitle-differing item pair.

    Reuses the same source/destination pairing the comparison report shows,
    so this only ever acts on items the "Subtitle Differences" table would
    also flag. Reads and writes subtitles entirely through the Jellyfin API
    (see transfer_subtitles.plan_subtitle_transfer), so a source subtitle
    stored in Jellyfin's internal metadata cache rather than next to the
    media file transfers the same as one that isn't - the gap a plain rsync
    of the media directories leaves. Skipped with "already_present" when the
    destination already has an English subtitle track (not attempted, not
    duplicated - a safety net for staleness between the audit snapshot and
    the live server, since missing_subtitle_transfer_targets already filters
    to source-has/destination-doesn't pairs), and recorded
    "no_source_subtitle" (not a failure) when the source has none to give.
    Continues past a single item's failure rather than aborting the whole
    batch, logging a summary at the end.

    Args:
        left_result: Completed audit results for the source server.
        right_result: Completed audit results for the destination server.
        dry_run: Preview planned transfers without writing anything.
        assume_yes: Skip the batch confirmation prompt.
        limit: When given, only attempt the first N items found, regardless
            of outcome - for quickly testing bulk-mode changes.

    Returns:
        A tuple of (exit code, per-item results). Exit code is ``0`` when
        every attempted transfer succeeded (or nothing needed transferring),
        ``1`` if any transfer failed.
    """
    logger = transfer_subtitles.LOGGER
    targets = missing_subtitle_transfer_targets(left_result, right_result)
    if limit is not None:
        targets = targets[:limit]
    left_label = left_result.server_name or left_result.server_key or "left"
    right_label = right_result.server_name or right_result.server_key or "right"

    if not targets:
        logger.info("No subtitle differences found between %s and %s.", left_label, right_label)
        return 0, ()

    logger.info(
        "%s subtitles for %d item(s) from %s to %s.",
        "Would transfer" if dry_run else "About to transfer",
        len(targets),
        left_label,
        right_label,
    )
    if not dry_run and not assume_yes:
        response = input(f"Transfer subtitles for {len(targets)} item(s)? [y/N] ").strip().lower()
        if response not in {"y", "yes"}:
            logger.info("Aborted.")
            return 1, ()

    config = get_config()
    server_clients: dict[str, JellyfinClient] = {}
    results: list[SubtitleTransferResult] = []
    transferred = 0
    unavailable = 0
    already_present = 0
    failed = 0

    try:
        for target in targets:
            try:
                from_client = _cached_jellyfin_client(server_clients, config, target.left_server_key)
                to_client = _cached_jellyfin_client(server_clients, config, target.right_server_key)
            except ConfigError as error:
                failed += 1
                logger.error(
                    "[%s] %s: failed to prepare transfer: %s",
                    target.library, target.display_name, error,
                )
                results.append(
                    SubtitleTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="failed",
                        detail=str(error),
                    )
                )
                continue

            try:
                destination_item = to_client.get_item(target.right_item_id)
            except JellyfinError as error:
                failed += 1
                logger.error(
                    "[%s] %s: failed to read destination item %s: %s",
                    target.library, target.display_name, target.right_item_id, error,
                )
                results.append(
                    SubtitleTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="failed",
                        detail=str(error),
                    )
                )
                continue

            if _has_english_subtitle_stream(destination_item):
                already_present += 1
                results.append(
                    SubtitleTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="already_present",
                    )
                )
                continue

            try:
                plan = transfer_subtitles.plan_subtitle_transfer(
                    from_client, to_client, target.left_item_id, target.right_item_id
                )
            except JellyfinError as error:
                failed += 1
                logger.error(
                    "[%s] %s: failed to read source subtitle: %s",
                    target.library, target.display_name, error,
                )
                results.append(
                    SubtitleTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="failed",
                        detail=str(error),
                    )
                )
                continue

            if not plan.has_subtitle:
                unavailable += 1
                results.append(
                    SubtitleTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="no_source_subtitle",
                        detail=plan.track_description,
                    )
                )
                continue

            if dry_run:
                transferred += 1
                logger.info(
                    "[%s] %s: would transfer subtitle (%s)",
                    target.library, target.display_name, plan.track_description,
                )
                results.append(
                    SubtitleTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="would_transfer",
                    )
                )
                continue

            try:
                transfer_subtitles.apply_subtitle_transfer(to_client, plan)
            except JellyfinError as error:
                failed += 1
                logger.error(
                    "[%s] %s: subtitle upload failed: %s",
                    target.library, target.display_name, error,
                )
                results.append(
                    SubtitleTransferResult(
                        library=target.library,
                        display_name=target.display_name,
                        status="failed",
                        detail=str(error),
                    )
                )
                continue

            transferred += 1
            logger.info(
                "[%s] %s: transferred subtitle (%s)",
                target.library, target.display_name, plan.track_description,
            )
            results.append(
                SubtitleTransferResult(
                    library=target.library,
                    display_name=target.display_name,
                    status="transferred",
                )
            )
    finally:
        for client in server_clients.values():
            client.close()

    logger.info(
        "Subtitle transfer summary: %d transferred, %d already present, %d unavailable, "
        "%d failed (of %d total).",
        transferred, already_present, unavailable, failed, len(targets),
    )
    exit_code = 1 if failed else 0
    return exit_code, tuple(results)


def _has_english_subtitle_stream(item_dto: dict) -> bool:
    """Return whether a Jellyfin item document already has an English subtitle track.

    Mirrors the language criterion has_english_subtitles() applies to a
    normalized MediaItem, but reads directly from a raw item document's
    MediaStreams so the bulk --transfer-subtitles run can check a freshly
    fetched destination item without re-normalizing it into a MediaItem.
    """
    english_codes = configured_english_language_codes()
    media_streams = item_dto.get("MediaStreams")
    if not isinstance(media_streams, list):
        return False

    for stream in media_streams:
        if not isinstance(stream, dict):
            continue
        if str(stream.get("Type", "")).strip().lower() != "subtitle":
            continue
        language = str(stream.get("Language") or "").strip().lower()
        if language in english_codes:
            return True

    return False


def _has_image_of_type(item_dto: dict, image_type: str) -> bool:
    """Return whether a Jellyfin item document already has an image of this type.

    Most image types are keyed by a single tag in the ``ImageTags`` dict, but
    ``Backdrop`` supports multiple images and is reported as a separate
    ``BackdropImageTags`` list instead - checked here too so a destination
    item that already has a backdrop isn't mistaken for one that doesn't.
    """
    image_tags = item_dto.get("ImageTags")
    if isinstance(image_tags, dict) and image_tags.get(image_type):
        return True
    plural_tags = item_dto.get(f"{image_type}ImageTags")
    return isinstance(plural_tags, list) and bool(plural_tags)


def _cached_jellyfin_client(
    server_clients: dict[str, JellyfinClient],
    config,
    server_key: str,
) -> JellyfinClient:
    """Return a cached Jellyfin client for a server key, creating one if needed."""
    client = server_clients.get(server_key)
    if client is None:
        client = JellyfinClient(config.servers.get(server_key))
        server_clients[server_key] = client
    return client


def _resolve_run_targets(options: AuditRunOptions) -> tuple[tuple[str | None, ...], str | None]:
    """Resolve requested audit targets and optional comparison pairing."""
    config = get_config()
    if options.audit_all:
        if options.server_key is not None:
            raise CommandLineUsageError("--all cannot be used with --server.")
        if options.compare_server_key is not None:
            raise CommandLineUsageError("--all cannot be used with --compare.")
        return tuple(server.key for server in config.servers.ordered()), None
    if options.compare_server_key != AUTO_COMPARE_SENTINEL:
        if options.compare_server_key is None:
            return (options.server_key,), None
        return (options.server_key, options.compare_server_key), options.compare_server_key
    if options.server_key is not None:
        raise CommandLineUsageError(
            "--compare without a server name can only be used when --server is not specified."
        )
    left_server, right_server = config.servers.first_two()
    return (left_server.key, right_server.key), right_server.key


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
