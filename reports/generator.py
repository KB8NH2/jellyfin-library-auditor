"""Top-level static site generation orchestration."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from config import get_config
from media import get_display_episode_number
from media import get_display_path
from models import MediaItem
from output_layout import audit_results_root
from output_layout import comparison_output_dir
from output_layout import server_csv_filename
from output_layout import server_csv_path
from output_layout import server_output_dir
from output_layout import shared_css_path
from output_layout import shared_js_path
from output_layout import write_server_report_metadata
from output_layout import write_audit_results_index
from report_filters import filter_report_output
from . import templates
from .checks import render_check_page
from .css import write_css
from .dashboard import render_dashboard_page
from .javascript import write_javascript
from .library import render_library_page
from results import AuditServerResult


CSV_HEADER = (
    "Library",
    "Path",
    "Series",
    "Title",
    "Season",
    "Episode",
    "Missing Subtitles",
    "Missing Primary",
    "Mismatched Filename Title",
    "Mismatched Stream Title",
    "Unknown Audio Codec",
    "Unknown Video Codec",
    "Mismatched TheTVDB Series",
    "Aired/DVD Order Mismatch",
)
MISMATCHED_FILENAME_TITLE_CHECKS = frozenset(
    {"mismatched_episode_filename_title", "mismatched_movie_filename_title"}
)
NON_ACTIONABLE_CHECKS = frozenset({"hdr_video", "missing_nfo", "missing_backdrop"})


def write_csv_report(result: AuditServerResult) -> Path:
    """Write a CSV report containing one row per audited media item."""
    result = filter_report_output(result)
    config = get_config()
    output_root = audit_results_root(config.reporting.output.audit_html)
    output_path = server_csv_path(
        output_root,
        result,
        config.reporting.output.audit_csv,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows(_csv_rows(result))
    return output_path


def write_html_report(result: AuditServerResult) -> Path:
    """Write a static HTML site for the supplied audit results."""
    return write_reports(result)


def write_reports(result: AuditServerResult) -> Path:
    """Generate the full static site and return the dashboard path."""
    result = filter_report_output(result)
    config = get_config()
    output_root = audit_results_root(config.reporting.output.audit_html)
    site_paths = _site_paths(
        server_output_dir(output_root, result),
        output_root,
    )
    _prepare_site_root(site_paths)

    actionable_findings = _actionable_findings(result.findings)
    site_links = _site_links(result, actionable_findings)
    generated_at_text = _generated_at_text()
    server_display_name = result.server_name or _server_display_name(
        result.server_url or ""
    )

    write_css(site_paths.css_path)
    write_javascript(site_paths.js_path)
    _write_dashboard(
        result,
        actionable_findings=actionable_findings,
        site_links=site_links,
        site_paths=site_paths,
        generated_at_text=generated_at_text,
        server_display_name=server_display_name,
    )
    _write_library_pages(
        result,
        actionable_findings=actionable_findings,
        site_links=site_links,
        site_paths=site_paths,
        server_display_name=server_display_name,
        generated_at_text=generated_at_text,
    )
    _write_check_pages(
        actionable_findings=actionable_findings,
        site_links=site_links,
        site_paths=site_paths,
        server_display_name=server_display_name,
        generated_at_text=generated_at_text,
    )
    write_server_report_metadata(
        output_root,
        result,
        generated_at_text=generated_at_text,
    )
    write_audit_results_index(
        output_root,
        (result,),
        include_comparison=(comparison_output_dir(output_root) / "index.html").exists(),
    )
    return site_paths.index_path


def _write_dashboard(
    result: AuditServerResult,
    *,
    actionable_findings: tuple,
    site_links: templates.SiteLinks,
    site_paths: templates.SitePaths,
    generated_at_text: str,
    server_display_name: str,
) -> None:
    """Write the dashboard page."""
    library_cards = _library_cards(result, actionable_findings, site_links)
    check_cards = _check_cards(actionable_findings, site_links)
    body = render_dashboard_page(
        server_display_name=server_display_name,
        generated_at_text=generated_at_text,
        csv_report_href=server_csv_filename(result, get_config().reporting.output.audit_csv),
        libraries_audited=result.libraries_audited,
        media_items_processed=result.media_items_processed,
        actionable_findings_count=len(actionable_findings),
        library_cards=library_cards,
        check_cards=check_cards,
    )
    site_paths.index_path.write_text(
        templates.page_document(
            title="Jellyfin Library Auditor Dashboard",
            relative_prefix="",
            asset_prefix="../",
            body=body,
        ),
        encoding="utf-8",
    )


def _write_library_pages(
    result: AuditServerResult,
    *,
    actionable_findings: tuple,
    site_links: templates.SiteLinks,
    site_paths: templates.SitePaths,
    server_display_name: str,
    generated_at_text: str,
) -> None:
    """Write one page per audited library."""
    grouped = templates.group_findings_by_library(actionable_findings)
    for library_name in _library_names(result):
        page_path = site_paths.libraries_dir / f"{site_links.library_slug_map[library_name]}.html"
        body = render_library_page(
            library_name,
            grouped.get(library_name, ()),
            site_links=site_links,
            server_display_name=server_display_name,
            generated_at_text=generated_at_text,
        )
        page_path.write_text(
            templates.page_document(
                title=f"{library_name} Findings",
                relative_prefix="../",
                asset_prefix="../../",
                body=body,
            ),
            encoding="utf-8",
        )


def _write_check_pages(
    *,
    actionable_findings: tuple,
    site_links: templates.SiteLinks,
    site_paths: templates.SitePaths,
    server_display_name: str,
    generated_at_text: str,
) -> None:
    """Write one page per actionable audit check."""
    grouped = templates.group_findings_by_check(actionable_findings)
    for check_name, findings in sorted(
        grouped.items(),
        key=lambda entry: (-len(entry[1]), templates.check_display_label(entry[0]).casefold()),
    ):
        page_path = site_paths.checks_dir / site_links.check_filename_map[check_name]
        body = render_check_page(
            check_name,
            findings,
            site_links=site_links,
            server_display_name=server_display_name,
            generated_at_text=generated_at_text,
        )
        page_path.write_text(
            templates.page_document(
                title=f"{templates.check_display_label(check_name)} Findings",
                relative_prefix="../",
                asset_prefix="../../",
                body=body,
            ),
            encoding="utf-8",
        )


def _csv_episode_number(item: MediaItem) -> str:
    """Return an item's Episode column value, guarded against Excel misreading it as a date.

    A combined-episode range like "19-20" is exactly the shape Excel's
    automatic type detection likes to reinterpret as a date (e.g. "5-6"
    commonly becomes "Jun-05") when a CSV is opened by double-clicking it -
    the underlying cell value on disk is unaffected, but the column
    displays wrong until the user manually reformats it, and a re-save
    from Excel would bake the wrong value in for good. A leading apostrophe
    is the standard fix: Excel's plain-text CSV import treats it as "force
    this cell to text" and doesn't display the apostrophe itself, while it
    has no such meaning to a plain CSV reader (e.g. compare_csv_files.py
    strips it back off on read - see its _without_excel_text_guard). A
    single episode number (no hyphen) is never ambiguous this way, so it's
    left as plain digits.
    """
    display_value = get_display_episode_number(item)
    return f"'{display_value}" if "-" in display_value else display_value


def _csv_rows(result: AuditServerResult) -> tuple[tuple[str, ...], ...]:
    """Return one CSV row per audited media item."""
    checks_by_item = _check_names_by_item(result.findings)
    rows = []
    for item in sorted(result.audited_items, key=templates.check_row_sort_key):
        check_names = checks_by_item.get(item.id, frozenset())
        rows.append(
            (
                item.library,
                get_display_path(item),
                item.series_name if item.is_episode and item.series_name else "",
                item.title,
                str(item.season_number) if item.is_episode and item.season_number is not None else "",
                _csv_episode_number(item) if item.is_episode else "",
                _yes_no("missing_english_subtitles" in check_names),
                _yes_no("missing_primary_image" in check_names),
                _yes_no(bool(MISMATCHED_FILENAME_TITLE_CHECKS & check_names)),
                _yes_no("mismatched_episode_stream_title" in check_names),
                _yes_no("unknown_audio_codec" in check_names),
                _yes_no("unknown_video_codec" in check_names),
                _yes_no("mismatched_tvdb_series" in check_names),
                _yes_no("aired_dvd_order_mismatch" in check_names),
            )
        )
    return tuple(rows)


def _check_names_by_item(
    findings: tuple,
) -> dict[str, frozenset[str]]:
    """Return the set of check names that fired for each media item id."""
    check_names: dict[str, set[str]] = {}
    for finding in findings:
        check_names.setdefault(finding.media_item.id, set()).add(finding.check_name)
    return {item_id: frozenset(names) for item_id, names in check_names.items()}


def _actionable_findings(findings: tuple) -> tuple:
    """Return only actionable findings for HTML reporting."""
    return tuple(
        finding
        for finding in findings
        if finding.check_name not in NON_ACTIONABLE_CHECKS
    )


def _library_cards(
    result: AuditServerResult,
    actionable_findings: tuple,
    site_links: templates.SiteLinks,
) -> tuple[templates.SummaryCard, ...]:
    """Return dashboard cards for audited libraries."""
    grouped = templates.group_findings_by_library(actionable_findings)
    cards: list[templates.SummaryCard] = []
    for library_result in sorted(
        result.library_results,
        key=lambda item: item.library.name.casefold(),
    ):
        library_name = library_result.library.name
        findings = grouped.get(library_name, ())
        media_items = len(templates.group_findings_by_media(findings))
        cards.append(
            templates.SummaryCard(
                title=library_name,
                value=str(len(findings)),
                accent="library",
                href=templates.library_page_href(
                    library_name,
                    site_links=site_links,
                    relative_prefix="",
                ),
                subtitle=f"{media_items} media items",
            )
        )
    return tuple(cards)


def _check_cards(
    actionable_findings: tuple,
    site_links: templates.SiteLinks,
) -> tuple[templates.SummaryCard, ...]:
    """Return dashboard cards for actionable checks."""
    grouped = templates.group_findings_by_check(actionable_findings)
    cards: list[templates.SummaryCard] = []
    for check_name, findings in sorted(
        grouped.items(),
        key=lambda entry: templates.check_sort_key(entry[0]),
    ):
        cards.append(
            templates.SummaryCard(
                title=templates.check_display_label(check_name),
                value=str(len(findings)),
                accent="check",
                href=templates.check_page_href(
                    check_name,
                    site_links=site_links,
                    relative_prefix="",
                ),
                subtitle=f"{len(templates.group_findings_by_media(findings))} media items",
            )
        )
    return tuple(cards)


def _site_links(result: AuditServerResult, actionable_findings: tuple) -> templates.SiteLinks:
    """Return all filename and anchor mappings used across the site."""
    library_slug_map = templates.build_slug_map(_library_names(result))
    check_filename_map = {
        check_name: f"{templates.slugify(check_name)}.html"
        for check_name in templates.group_findings_by_check(actionable_findings)
    }
    media_anchor_map = _media_anchor_map(actionable_findings)
    return templates.SiteLinks(
        library_slug_map=library_slug_map,
        check_filename_map=check_filename_map,
        media_anchor_map=media_anchor_map,
    )


def _media_anchor_map(actionable_findings: tuple) -> dict[tuple[str, str], str]:
    """Return row anchors for each media item."""
    grouped = templates.group_findings_by_media(actionable_findings)
    anchors: dict[tuple[str, str], str] = {}
    used_anchors: set[str] = set()
    for media_key, media_findings in grouped.items():
        item = templates.media_item_from_findings(media_findings)
        base_anchor = templates.slugify(f"{item.display_name}_{item.id}")
        anchor = f"media-{base_anchor}"
        counter = 2
        while anchor in used_anchors:
            anchor = f"media-{base_anchor}-{counter}"
            counter += 1
        anchors[media_key] = anchor
        used_anchors.add(anchor)
    return anchors


def _library_names(result: AuditServerResult) -> tuple[str, ...]:
    """Return all audited library names."""
    names = {library_result.library.name for library_result in result.library_results}
    names.update(finding.media_item.library for finding in result.findings)
    return tuple(sorted(names, key=str.casefold))


def _prepare_site_root(site_paths: templates.SitePaths) -> None:
    """Create a clean output root for the static site."""
    site_paths.root_dir.mkdir(parents=True, exist_ok=True)
    for stale_dir in (site_paths.libraries_dir, site_paths.checks_dir):
        if stale_dir.exists():
            shutil.rmtree(stale_dir)
    if site_paths.index_path.exists():
        site_paths.index_path.unlink()
    site_paths.libraries_dir.mkdir(parents=True, exist_ok=True)
    site_paths.checks_dir.mkdir(parents=True, exist_ok=True)
    site_paths.css_path.parent.mkdir(parents=True, exist_ok=True)
    site_paths.js_path.parent.mkdir(parents=True, exist_ok=True)


def _site_paths(server_root_dir: Path, output_root: Path) -> templates.SitePaths:
    """Return normalized site output paths for one server report."""
    return templates.SitePaths(
        root_dir=server_root_dir,
        index_path=server_root_dir / "index.html",
        libraries_dir=server_root_dir / "libraries",
        checks_dir=server_root_dir / "checks",
        css_path=shared_css_path(output_root),
        js_path=shared_js_path(output_root),
    )


def _generated_at_text() -> str:
    """Return a display-friendly local timestamp for the dashboard."""
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _server_display_name(server_url: str) -> str:
    """Return a fallback dashboard server name from the configured URL."""
    parsed_url = urlsplit(server_url.strip())
    if parsed_url.hostname:
        return parsed_url.hostname
    if parsed_url.netloc:
        return parsed_url.netloc
    stripped_url = server_url.strip().rstrip("/")
    return stripped_url or "unknown"


def _yes_no(value: bool) -> str:
    """Return Yes or No for CSV fields."""
    return "Yes" if value else "No"
