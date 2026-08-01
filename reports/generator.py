"""Top-level static site generation orchestration."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from config import get_config
from media import get_display_path
from media import has_jellyfin_backdrop
from media import has_jellyfin_logo
from media import has_jellyfin_primary_image
from media import has_jellyfin_thumb
from media import local_backdrop_exists
from media import local_poster_exists
from . import templates
from .checks import render_check_page
from .css import write_css
from .dashboard import render_dashboard_page
from .javascript import write_javascript
from .library import render_library_page
from results import AuditServerResult


CSV_HEADER = (
    "Category",
    "Severity",
    "Check",
    "Title",
    "Path",
    "Local Poster",
    "Local Backdrop",
    "Jellyfin Primary",
    "Jellyfin Backdrop",
    "Jellyfin Logo",
    "Jellyfin Thumb",
    "Artwork Source",
    "Message",
)
NON_ACTIONABLE_CHECKS = frozenset({"hdr_video", "missing_nfo"})


def write_csv_report(result: AuditServerResult) -> Path:
    """Write a CSV report containing one row per finding."""
    output_path = get_config().reporting.output.audit_csv
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
    config = get_config()
    site_paths = _site_paths(config.reporting.output.audit_html)
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
    )
    _write_check_pages(
        actionable_findings=actionable_findings,
        site_links=site_links,
        site_paths=site_paths,
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
) -> None:
    """Write one page per audited library."""
    grouped = templates.group_findings_by_library(actionable_findings)
    for library_name in _library_names(result):
        page_path = site_paths.libraries_dir / f"{site_links.library_slug_map[library_name]}.html"
        body = render_library_page(
            library_name,
            grouped.get(library_name, ()),
            site_links=site_links,
        )
        page_path.write_text(
            templates.page_document(
                title=f"{library_name} Findings",
                relative_prefix="../",
                body=body,
            ),
            encoding="utf-8",
        )


def _write_check_pages(
    *,
    actionable_findings: tuple,
    site_links: templates.SiteLinks,
    site_paths: templates.SitePaths,
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
        )
        page_path.write_text(
            templates.page_document(
                title=f"{templates.check_display_label(check_name)} Findings",
                relative_prefix="../",
                body=body,
            ),
            encoding="utf-8",
        )


def _csv_rows(result: AuditServerResult) -> tuple[tuple[str, ...], ...]:
    """Return CSV rows for the supplied audit results."""
    rows = []
    for finding in templates.sort_findings(result.findings):
        item = finding.media_item
        local_poster = local_poster_exists(item)
        local_backdrop = local_backdrop_exists(item)
        jellyfin_primary = has_jellyfin_primary_image(item)
        jellyfin_backdrop = has_jellyfin_backdrop(item)
        rows.append(
            (
                finding.category.value.title(),
                finding.severity.value.title(),
                finding.check_name,
                item.display_name,
                get_display_path(item),
                _yes_no(local_poster),
                _yes_no(local_backdrop),
                _yes_no(jellyfin_primary),
                _yes_no(jellyfin_backdrop),
                _yes_no(has_jellyfin_logo(item)),
                _yes_no(has_jellyfin_thumb(item)),
                _artwork_source(
                    has_local_artwork=local_poster or local_backdrop,
                    has_jellyfin_artwork=jellyfin_primary or jellyfin_backdrop,
                ),
                finding.message,
            )
        )
    return tuple(rows)


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
        key=lambda entry: (-len(entry[1]), templates.check_display_label(entry[0]).casefold()),
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
    if site_paths.root_dir.exists():
        shutil.rmtree(site_paths.root_dir)
    site_paths.root_dir.mkdir(parents=True, exist_ok=True)
    site_paths.libraries_dir.mkdir(parents=True, exist_ok=True)
    site_paths.checks_dir.mkdir(parents=True, exist_ok=True)
    site_paths.css_path.parent.mkdir(parents=True, exist_ok=True)
    site_paths.js_path.parent.mkdir(parents=True, exist_ok=True)


def _site_paths(configured_path: Path) -> templates.SitePaths:
    """Return normalized site output paths from the configured HTML path."""
    root_dir = configured_path.with_suffix("") if configured_path.suffix else configured_path
    return templates.SitePaths(
        root_dir=root_dir,
        index_path=root_dir / "index.html",
        libraries_dir=root_dir / "libraries",
        checks_dir=root_dir / "checks",
        css_path=root_dir / "css" / "style.css",
        js_path=root_dir / "js" / "report.js",
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


def _artwork_source(has_local_artwork: bool, has_jellyfin_artwork: bool) -> str:
    """Return the compact artwork source label for CSV output."""
    if has_local_artwork and has_jellyfin_artwork:
        return "Local + Jellyfin"
    if has_local_artwork:
        return "Local only"
    if has_jellyfin_artwork:
        return "Jellyfin metadata"
    return "Missing"
