"""Shared HTML fragments and grouping helpers for report generation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from pathlib import Path

import audit_types
from models import MediaItem


SEVERITY_SORT_ORDER = {
    audit_types.AuditSeverity.ERROR: 0,
    audit_types.AuditSeverity.WARNING: 1,
    audit_types.AuditSeverity.INFO: 2,
}
CHECK_DISPLAY_LABELS = {
    "missing_english_subtitles": "Missing English Subtitles",
    "missing_poster": "Missing Poster",
    "missing_backdrop": "Missing Backdrop",
    "missing_primary_image": "Missing Primary Image",
    "missing_nfo": "Missing NFO",
    "unknown_video_codec": "Unknown Video Codec",
    "unknown_audio_codec": "Unknown Audio Codec",
    "hdr_video": "HDR Video",
}
CHECK_SUMMARY_LABELS = {
    "missing_english_subtitles": "English Subtitles",
    "missing_poster": "Poster",
    "missing_backdrop": "Backdrop",
    "missing_primary_image": "Primary Image",
    "missing_nfo": "NFO",
    "unknown_video_codec": "Video Codec",
    "unknown_audio_codec": "Audio Codec",
}


@dataclass(frozen=True, slots=True)
class SummaryCard:
    """Represents one summary card on a report page."""

    title: str
    value: str
    accent: str
    href: str | None = None
    subtitle: str | None = None


@dataclass(frozen=True, slots=True)
class Breadcrumb:
    """Represents one breadcrumb item."""

    label: str
    href: str | None = None


@dataclass(frozen=True, slots=True)
class SitePaths:
    """Represents generated site output paths."""

    root_dir: Path
    index_path: Path
    libraries_dir: Path
    checks_dir: Path
    css_path: Path
    js_path: Path


@dataclass(frozen=True, slots=True)
class SiteLinks:
    """Holds filename and anchor mappings for generated pages."""

    library_slug_map: dict[str, str]
    check_filename_map: dict[str, str]
    media_anchor_map: dict[tuple[str, str], str]


def sort_findings(
    findings: tuple[audit_types.AuditFinding, ...],
) -> tuple[audit_types.AuditFinding, ...]:
    """Sort findings by severity, title, and check name."""
    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                SEVERITY_SORT_ORDER[finding.severity],
                finding.media_item.display_name.casefold(),
                check_display_label(finding.check_name).casefold(),
            ),
        )
    )


def group_findings_by_library(
    findings: tuple[audit_types.AuditFinding, ...],
) -> dict[str, tuple[audit_types.AuditFinding, ...]]:
    """Group findings by library name."""
    grouped: dict[str, list[audit_types.AuditFinding]] = {}
    for finding in findings:
        grouped.setdefault(finding.media_item.library, []).append(finding)
    return {key: tuple(items) for key, items in grouped.items()}


def group_findings_by_check(
    findings: tuple[audit_types.AuditFinding, ...],
) -> dict[str, tuple[audit_types.AuditFinding, ...]]:
    """Group findings by check name."""
    grouped: dict[str, list[audit_types.AuditFinding]] = {}
    for finding in findings:
        grouped.setdefault(finding.check_name, []).append(finding)
    return {key: tuple(items) for key, items in grouped.items()}


def group_findings_by_media(
    findings: tuple[audit_types.AuditFinding, ...],
) -> dict[tuple[str, str], tuple[audit_types.AuditFinding, ...]]:
    """Group findings by unique media item."""
    grouped: dict[tuple[str, str], list[audit_types.AuditFinding]] = {}
    for finding in findings:
        grouped.setdefault(media_key(finding.media_item), []).append(finding)
    return {key: tuple(items) for key, items in grouped.items()}


def group_findings_by_series(
    findings: tuple[audit_types.AuditFinding, ...],
) -> dict[str, tuple[audit_types.AuditFinding, ...]]:
    """Group findings by series name."""
    grouped: dict[str, list[audit_types.AuditFinding]] = {}
    for finding in findings:
        grouped.setdefault(finding.media_item.series_name or "Unknown Series", []).append(
            finding
        )
    return {key: tuple(items) for key, items in grouped.items()}


def group_findings_by_season(
    findings: tuple[audit_types.AuditFinding, ...],
) -> dict[str, tuple[audit_types.AuditFinding, ...]]:
    """Group episode findings by season label."""
    grouped: dict[str, list[audit_types.AuditFinding]] = {}
    for finding in findings:
        item = finding.media_item
        if item.season_number is not None:
            label = f"Season {item.season_number}"
        elif item.season_name:
            label = item.season_name
        else:
            label = "Unknown Season"
        grouped.setdefault(label, []).append(finding)
    return {key: tuple(items) for key, items in grouped.items()}


def sort_named_groups(
    grouped: dict[str, tuple[audit_types.AuditFinding, ...]],
) -> tuple[tuple[str, tuple[audit_types.AuditFinding, ...]], ...]:
    """Sort named groups by finding count and name."""
    return tuple(
        sorted(
            grouped.items(),
            key=lambda entry: (-len(entry[1]), entry[0].casefold()),
        )
    )


def sort_season_groups(
    grouped: dict[str, tuple[audit_types.AuditFinding, ...]],
) -> tuple[tuple[str, tuple[audit_types.AuditFinding, ...]], ...]:
    """Sort season groups numerically when possible."""

    def sort_key(entry: tuple[str, tuple[audit_types.AuditFinding, ...]]) -> tuple[int, str]:
        label = entry[0]
        match = re.search(r"(\d+)$", label)
        if match:
            return (int(match.group(1)), label.casefold())
        return (9999, label.casefold())

    return tuple(sorted(grouped.items(), key=sort_key))


def build_slug_map(names: tuple[str, ...]) -> dict[str, str]:
    """Return unique slugs for the supplied names."""
    slug_map: dict[str, str] = {}
    used_slugs: set[str] = set()

    for name in sorted(names, key=str.casefold):
        base_slug = slugify(name)
        slug = base_slug
        counter = 2
        while slug in used_slugs:
            slug = f"{base_slug}_{counter}"
            counter += 1
        slug_map[name] = slug
        used_slugs.add(slug)

    return slug_map


def slugify(value: str) -> str:
    """Return a filesystem-friendly slug."""
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().casefold())
    collapsed = re.sub(r"_+", "_", normalized).strip("_")
    return collapsed or "item"


def check_display_label(check_name: str) -> str:
    """Return a display-friendly label for a check name."""
    return CHECK_DISPLAY_LABELS.get(check_name, check_name.replace("_", " ").title())


def check_summary_label(check_name: str) -> str:
    """Return a concise check label for the findings summary column."""
    return CHECK_SUMMARY_LABELS.get(check_name, check_display_label(check_name))


def media_key(item: MediaItem) -> tuple[str, str]:
    """Return a stable media item key."""
    return (item.library, item.id)


def media_item_from_findings(
    findings: tuple[audit_types.AuditFinding, ...],
) -> MediaItem:
    """Return the media item shared by a grouped set of findings."""
    return findings[0].media_item


def media_anchor(item: MediaItem, site_links: SiteLinks) -> str:
    """Return the table row anchor for a media item."""
    return site_links.media_anchor_map[media_key(item)]


def library_page_href(
    library_name: str,
    *,
    site_links: SiteLinks,
    relative_prefix: str,
) -> str:
    """Return the relative href for a library page."""
    return f"{relative_prefix}libraries/{site_links.library_slug_map[library_name]}.html"


def check_page_href(
    check_name: str,
    *,
    site_links: SiteLinks,
    relative_prefix: str,
) -> str:
    """Return the relative href for a check page."""
    return f"{relative_prefix}checks/{site_links.check_filename_map[check_name]}"


def library_row_href(
    item: MediaItem,
    *,
    site_links: SiteLinks,
    relative_prefix: str,
) -> str:
    """Return the relative href to the media row on its library page."""
    return (
        f"{library_page_href(item.library, site_links=site_links, relative_prefix=relative_prefix)}"
        f"#{media_anchor(item, site_links)}"
    )


def row_search_text(findings: tuple[audit_types.AuditFinding, ...]) -> str:
    """Return normalized search text for a media row."""
    item = media_item_from_findings(findings)
    parts = [
        item.display_name,
        item.title,
        item.library,
        item.series_name or "",
        item.season_name or "",
        str(item.year) if item.year is not None else "",
    ]
    for finding in findings:
        parts.append(check_display_label(finding.check_name))
        parts.append(check_summary_label(finding.check_name))
        parts.append(finding.message)

    return " ".join(part.strip().lower() for part in parts if part.strip())


def media_sort_key(item: MediaItem) -> tuple[int, int, str]:
    """Return a stable sort key for grouped media items."""
    return (
        item.season_number or 0,
        item.episode_number or 0,
        item.display_name.casefold(),
    )


def render_page(
    *,
    title: str,
    current_nav: str,
    relative_prefix: str,
    heading: str,
    intro: str,
    content: str,
    breadcrumbs: tuple[Breadcrumb, ...],
    include_search: bool,
    include_expand_controls: bool,
) -> str:
    """Return a full HTML page body."""
    toolbar_html = render_toolbar(include_expand_controls) if include_search else ""
    return "\n".join(
        (
            f'<main class="page-shell" data-nav-current="{escape(current_nav)}">',
            '  <header class="sticky-header">',
            render_navigation(relative_prefix),
            render_breadcrumbs(breadcrumbs),
            "    <section class=\"page-header-card\">",
            f"      <h1>{escape(heading)}</h1>",
            f"      <p class=\"page-intro\">{escape(intro)}</p>",
            "    </section>",
            toolbar_html,
            "  </header>",
            '  <section class="page-content">',
            content,
            "  </section>",
            render_about_section(title),
            "</main>",
        )
    )


def render_navigation(relative_prefix: str) -> str:
    """Return the shared primary navigation."""
    return "\n".join(
        (
            '    <nav class="site-nav" aria-label="Primary">',
            f'      <a class="nav-link" data-nav="Dashboard" href="{escape(f"{relative_prefix}index.html")}">Dashboard</a>',
            f'      <a class="nav-link" data-nav="Libraries" href="{escape(f"{relative_prefix}index.html#libraries-overview")}">Libraries</a>',
            f'      <a class="nav-link" data-nav="Checks" href="{escape(f"{relative_prefix}index.html#checks-overview")}">Audit Checks</a>',
            '      <a class="nav-link" data-nav="About" href="#about">About</a>',
            "    </nav>",
        )
    )


def render_breadcrumbs(breadcrumbs: tuple[Breadcrumb, ...]) -> str:
    """Return breadcrumb navigation."""
    items: list[str] = []
    for breadcrumb in breadcrumbs:
        if breadcrumb.href:
            items.append(
                "        <li>"
                f'<a href="{escape(breadcrumb.href)}">{escape(breadcrumb.label)}</a>'
                "</li>"
            )
        else:
            items.append(
                f'        <li aria-current="page">{escape(breadcrumb.label)}</li>'
            )

    return "\n".join(
        (
            '    <nav class="breadcrumbs" aria-label="Breadcrumb">',
            "      <ol>",
            *items,
            "      </ol>",
            "    </nav>",
        )
    )


def render_toolbar(include_expand_controls: bool) -> str:
    """Return the shared search toolbar."""
    buttons = ()
    if include_expand_controls:
        buttons = (
            '        <button type="button" class="toolbar-button" id="expand-all-button">Expand All</button>',
            '        <button type="button" class="toolbar-button" id="collapse-all-button">Collapse All</button>',
        )

    return "\n".join(
        (
            '    <section class="toolbar-card" id="page-search">',
            "      <div>",
            "        <h2>Search</h2>",
            "        <p class=\"muted-text\">Filter visible media rows instantly.</p>",
            "      </div>",
            '      <div class="toolbar-controls">',
            '        <label class="search-field">',
            "          <span>Search items</span>",
            '          <input id="report-search" type="search" placeholder="Search title, series, finding, or message..." autocomplete="off">',
            "        </label>",
            *buttons,
            "      </div>",
            "    </section>",
        )
    )


def render_summary_cards(cards: tuple[SummaryCard, ...]) -> str:
    """Return a grid of summary cards."""
    rendered_cards = [render_summary_card(card) for card in cards]
    return "\n".join(
        (
            '  <section class="summary-card-grid">',
            *rendered_cards,
            "  </section>",
        )
    )


def render_summary_card(card: SummaryCard) -> str:
    """Return one summary card."""
    tag_name = "a" if card.href else "article"
    href_attribute = f' href="{escape(card.href)}"' if card.href else ""
    subtitle_html = (
        f'<p class="summary-card-subtitle">{escape(card.subtitle)}</p>'
        if card.subtitle
        else ""
    )
    return "\n".join(
        (
            f'    <{tag_name} class="summary-card summary-card-{escape(card.accent)}"{href_attribute}>',
            f"      <h3>{escape(card.title)}</h3>",
            f'      <p class="summary-card-value">{escape(card.value)}</p>',
            subtitle_html,
            f"    </{tag_name}>",
        )
    )


def render_details_group(
    *,
    title_html: str,
    count: int,
    body: str,
    search_text: str,
) -> str:
    """Return a collapsible details section."""
    return "\n".join(
        (
            f'    <details class="details-group" data-group data-search="{escape(search_text.casefold())}">',
            "      <summary>",
            f'        <span class="details-title">{title_html}</span>',
            f'        <span class="details-count">({count} findings)</span>',
            "      </summary>",
            '      <div class="details-body">',
            body,
            "      </div>",
            "    </details>",
        )
    )


def render_sortable_table(headers: tuple[str, ...], rows: tuple[str, ...]) -> str:
    """Return a sortable table wrapper."""
    header_html = "".join(
        _table_header(index, label)
        for index, label in enumerate(headers)
    )
    body_rows = rows or (
        '<tr class="empty-row"><td colspan="99">No actionable items.</td></tr>',
    )
    return "\n".join(
        (
            '    <div class="table-shell">',
            '      <table class="data-table">',
            f"        <thead><tr>{header_html}</tr></thead>",
            "        <tbody>",
            *body_rows,
            "        </tbody>",
            "      </table>",
            "    </div>",
        )
    )


def render_status_label(present: bool) -> str:
    """Return a compact present/missing status label."""
    if present:
        return '<span class="status-label status-present">✓ present</span>'
    return '<span class="status-label status-missing">✗ missing</span>'


def render_severity_badge(severity: audit_types.AuditSeverity) -> str:
    """Return one severity badge."""
    return (
        f'<span class="severity-badge severity-{escape(severity.value)}">'
        f"{escape(severity.value.upper())}</span>"
    )


def render_about_section(page_title: str) -> str:
    """Return the shared about section."""
    return "\n".join(
        (
            '  <section class="about-card" id="about">',
            "    <h2>About</h2>",
            f"    <p>This static report page was generated for {escape(page_title)}.</p>",
            "    <p class=\"muted-text\">Open any page directly in a modern browser. No internet connection is required.</p>",
            "  </section>",
        )
    )


def page_document(
    *,
    title: str,
    relative_prefix: str,
    body: str,
) -> str:
    """Return a full standalone HTML document."""
    return "\n".join(
        (
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{escape(title)}</title>",
            f'  <link rel="stylesheet" href="{escape(f"{relative_prefix}css/style.css")}">',
            "</head>",
            "<body>",
            body,
            f'  <script src="{escape(f"{relative_prefix}js/report.js")}"></script>',
            "</body>",
            "</html>",
        )
    )


def finding_count_summary(
    findings: tuple[audit_types.AuditFinding, ...],
) -> tuple[str, str, str]:
    """Return counts for errors, warnings, and info findings."""
    error_count = 0
    warning_count = 0
    info_count = 0
    for finding in findings:
        if finding.severity == audit_types.AuditSeverity.ERROR:
            error_count += 1
        elif finding.severity == audit_types.AuditSeverity.WARNING:
            warning_count += 1
        else:
            info_count += 1
    return str(error_count), str(warning_count), str(info_count)


def _table_header(index: int, label: str) -> str:
    """Return one sortable table header cell."""
    return (
        f'<th><button type="button" class="sort-button" '
        f'data-column="{index}" onclick="sortReportTable(this)">{escape(label)}</button></th>'
    )
