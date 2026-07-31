"""Shared HTML fragments and helpers for report generation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from pathlib import Path

import audit_types


SEVERITY_SORT_ORDER = {
    audit_types.AuditSeverity.ERROR: 0,
    audit_types.AuditSeverity.WARNING: 1,
    audit_types.AuditSeverity.INFO: 2,
}
CHECK_DISPLAY_LABELS = {
    "missing_english_subtitles": "Missing English Subtitles",
    "missing_poster": "Missing Posters",
    "missing_backdrop": "Missing Backdrops",
    "missing_nfo": "Missing NFO",
    "unknown_video_codec": "Unknown Video Codec",
    "unknown_audio_codec": "Unknown Audio Codec",
    "hdr_video": "HDR Video",
}


@dataclass(frozen=True, slots=True)
class SummaryCard:
    """Represents one summary card on a report page."""

    title: str
    value: str
    accent: str
    href: str | None = None
    subtitle: str | None = None
    search_text: str = ""


@dataclass(frozen=True, slots=True)
class SitePaths:
    """Represents generated site output paths."""

    root_dir: Path
    index_path: Path
    css_path: Path
    js_path: Path
    categories_dir: Path
    libraries_dir: Path


def sort_findings(
    findings: tuple[audit_types.AuditFinding, ...],
) -> tuple[audit_types.AuditFinding, ...]:
    """Sort findings by severity, category, and title."""
    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                SEVERITY_SORT_ORDER[finding.severity],
                finding.category.value,
                finding.media_item.display_name.casefold(),
            ),
        )
    )


def sort_findings_by_title(
    findings: tuple[audit_types.AuditFinding, ...],
) -> tuple[audit_types.AuditFinding, ...]:
    """Sort findings alphabetically by display title."""
    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                finding.media_item.display_name.casefold(),
                finding.check_name.casefold(),
            ),
        )
    )


def group_findings_by_category(
    findings: tuple[audit_types.AuditFinding, ...],
) -> dict[audit_types.AuditCategory, tuple[audit_types.AuditFinding, ...]]:
    """Group findings by category."""
    grouped: dict[audit_types.AuditCategory, list[audit_types.AuditFinding]] = {}
    for finding in findings:
        grouped.setdefault(finding.category, []).append(finding)
    return {key: tuple(items) for key, items in grouped.items()}


def group_findings_by_library(
    findings: tuple[audit_types.AuditFinding, ...],
) -> dict[str, tuple[audit_types.AuditFinding, ...]]:
    """Group findings by library name."""
    grouped: dict[str, list[audit_types.AuditFinding]] = {}
    for finding in findings:
        grouped.setdefault(finding.media_item.library, []).append(finding)
    return {key: tuple(items) for key, items in grouped.items()}


def group_findings_by_severity(
    findings: tuple[audit_types.AuditFinding, ...],
) -> dict[audit_types.AuditSeverity, tuple[audit_types.AuditFinding, ...]]:
    """Group findings by severity."""
    grouped: dict[audit_types.AuditSeverity, list[audit_types.AuditFinding]] = {}
    for finding in findings:
        grouped.setdefault(finding.severity, []).append(finding)
    return {key: tuple(items) for key, items in grouped.items()}


def group_findings_by_series(
    findings: tuple[audit_types.AuditFinding, ...],
) -> dict[str, tuple[audit_types.AuditFinding, ...]]:
    """Group findings by series name."""
    grouped: dict[str, list[audit_types.AuditFinding]] = {}
    for finding in findings:
        series_name = finding.media_item.series_name or "Unknown Series"
        grouped.setdefault(series_name, []).append(finding)
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
    """Sort string-keyed groups by count and name."""
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
    """Return unique slugs for names."""
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


def search_text_for_finding(finding: audit_types.AuditFinding) -> str:
    """Return normalized search text for a finding."""
    parts = (
        finding.media_item.display_name,
        finding.media_item.series_name or "",
        check_display_label(finding.check_name),
        finding.message,
    )
    return " ".join(part.strip().lower() for part in parts if part.strip())


def finding_row_id(finding_id_map: dict[int, str], finding: audit_types.AuditFinding) -> str:
    """Return the stable row id for a finding."""
    return finding_id_map[id(finding)]


def finding_link(
    finding: audit_types.AuditFinding,
    *,
    library_slug_map: dict[str, str],
    finding_id_map: dict[int, str],
    relative_prefix: str,
    current_library_name: str | None = None,
) -> str:
    """Return the href for a finding title link."""
    row_id = finding_row_id(finding_id_map, finding)
    if current_library_name == finding.media_item.library:
        return f"#{row_id}"

    library_slug = library_slug_map[finding.media_item.library]
    return f"{relative_prefix}libraries/{library_slug}.html#{row_id}"


def render_page(
    *,
    title: str,
    current_nav: str,
    relative_prefix: str,
    heading: str,
    intro: str,
    content: str,
) -> str:
    """Return a full HTML page document body."""
    return "\n".join(
        (
            f'<main class="page-shell" data-nav-current="{escape(current_nav)}">',
            render_navigation(relative_prefix),
            "  <header class=\"page-header\">",
            f"    <h1>{escape(heading)}</h1>",
            f"    <p class=\"page-intro\">{escape(intro)}</p>",
            "  </header>",
            render_toolbar(),
            content,
            render_about_section(title),
            "</main>",
        )
    )


def render_navigation(relative_prefix: str) -> str:
    """Return the shared navigation bar."""
    dashboard_href = f"{relative_prefix}index.html"
    libraries_href = f"{relative_prefix}index.html#libraries-overview"
    categories_href = f"{relative_prefix}index.html#categories-overview"
    return "\n".join(
        (
            '  <nav class="site-nav">',
            f'    <a class="nav-link" data-nav="Dashboard" href="{escape(dashboard_href)}">Dashboard</a>',
            f'    <a class="nav-link" data-nav="Libraries" href="{escape(libraries_href)}">Libraries</a>',
            f'    <a class="nav-link" data-nav="Categories" href="{escape(categories_href)}">Categories</a>',
            '    <a class="nav-link" data-nav="Search" href="#page-search">Search</a>',
            '    <a class="nav-link" data-nav="About" href="#about">About</a>',
            "  </nav>",
        )
    )


def render_toolbar() -> str:
    """Return the shared search and control toolbar."""
    return "\n".join(
        (
            '  <section class="toolbar-card" id="page-search">',
            "    <div>",
            "      <h2>Search</h2>",
            "      <p class=\"muted-text\">Filter visible findings instantly.</p>",
            "    </div>",
            "    <div class=\"toolbar-controls\">",
            '      <label class="search-field">',
            "        <span>Search findings</span>",
            '        <input id="report-search" type="search" placeholder="Search title, series, finding, or message..." autocomplete="off">',
            "      </label>",
            '      <button type="button" class="toolbar-button" id="expand-all-button">Expand All</button>',
            '      <button type="button" class="toolbar-button" id="collapse-all-button">Collapse All</button>',
            "    </div>",
            "  </section>",
        )
    )


def render_summary_cards(cards: tuple[SummaryCard, ...]) -> str:
    """Return a responsive summary card grid."""
    card_html = [render_summary_card(card) for card in cards]
    return "\n".join(
        (
            '  <section class="summary-card-grid">',
            *card_html,
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
    search_attribute = escape(card.search_text or f"{card.title} {card.value}")
    return "\n".join(
        (
            f'    <{tag_name} class="summary-card summary-card-{escape(card.accent)}" '
            f'data-search-card data-search="{search_attribute}"{href_attribute}>',
            f'      <h3>{escape(card.title)}</h3>',
            f'      <p class="summary-card-value">{escape(card.value)}</p>',
            subtitle_html,
            f"    </{tag_name}>",
        )
    )


def render_details_group(
    *,
    title: str,
    count: int,
    body: str,
    search_text: str,
) -> str:
    """Return a collapsible details group."""
    return "\n".join(
        (
            f'  <details class="details-group" data-group data-search-container data-search="{escape(search_text.casefold())}">',
            "    <summary>",
            f'      <span class="details-title">{escape(title)}</span>',
            f'      <span class="details-count">({count})</span>',
            "    </summary>",
            '    <div class="details-body">',
            body,
            "    </div>",
            "  </details>",
        )
    )


def render_findings_table(
    findings: tuple[audit_types.AuditFinding, ...],
    *,
    library_slug_map: dict[str, str],
    finding_id_map: dict[int, str],
    relative_prefix: str,
    current_library_name: str | None = None,
) -> str:
    """Return one compact findings table."""
    header_html = "".join(
        (
            _table_header(0, "Severity"),
            _table_header(1, "Title"),
            _table_header(2, "Finding"),
            _table_header(3, "Message"),
        )
    )
    row_html = [render_finding_row(
        finding,
        library_slug_map=library_slug_map,
        finding_id_map=finding_id_map,
        relative_prefix=relative_prefix,
        current_library_name=current_library_name,
    ) for finding in findings]
    return "\n".join(
        (
            '    <div class="table-shell" data-table-wrapper>',
            '      <table class="findings-table">',
            f"        <thead><tr>{header_html}</tr></thead>",
            "        <tbody>",
            *row_html,
            "        </tbody>",
            "      </table>",
            "    </div>",
        )
    )


def _table_header(index: int, label: str) -> str:
    """Return one sortable table header cell."""
    return (
        f'<th><button type="button" class="sort-button" '
        f'data-column="{index}" onclick="sortReportTable(this)">'
        f"{escape(label)}</button></th>"
    )


def render_finding_row(
    finding: audit_types.AuditFinding,
    *,
    library_slug_map: dict[str, str],
    finding_id_map: dict[int, str],
    relative_prefix: str,
    current_library_name: str | None = None,
) -> str:
    """Return one findings table row."""
    row_id = finding_row_id(finding_id_map, finding)
    title_href = finding_link(
        finding,
        library_slug_map=library_slug_map,
        finding_id_map=finding_id_map,
        relative_prefix=relative_prefix,
        current_library_name=current_library_name,
    )
    return "\n".join(
        (
            f'          <tr id="{escape(row_id)}" data-finding-row data-search="{escape(search_text_for_finding(finding))}">',
            f'            <td>{render_severity_badge(finding.severity)}</td>',
            f'            <td><a class="finding-link" href="{escape(title_href)}">{escape(finding.media_item.display_name)}</a></td>',
            f'            <td>{escape(check_display_label(finding.check_name))}</td>',
            f'            <td>{escape(finding.message)}</td>',
            "          </tr>",
        )
    )


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
    css_href = f"{relative_prefix}css/style.css"
    js_href = f"{relative_prefix}js/report.js"
    return "\n".join(
        (
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{escape(title)}</title>",
            f'  <link rel="stylesheet" href="{escape(css_href)}">',
            "</head>",
            "<body>",
            body,
            f'  <script src="{escape(js_href)}"></script>',
            "</body>",
            "</html>",
        )
    )


def relative_prefix_for(page_path: Path, root_dir: Path) -> str:
    """Return the relative prefix from a page back to the site root."""
    relative_path = page_path.relative_to(root_dir)
    depth = len(relative_path.parents) - 1
    if depth <= 0:
        return ""
    return "../" * depth


def finding_count_summary(findings: tuple[audit_types.AuditFinding, ...]) -> tuple[str, str, str]:
    """Return counts for errors, warnings, and info findings."""
    by_severity = group_findings_by_severity(findings)
    return (
        str(len(by_severity.get(audit_types.AuditSeverity.ERROR, ()))),
        str(len(by_severity.get(audit_types.AuditSeverity.WARNING, ()))),
        str(len(by_severity.get(audit_types.AuditSeverity.INFO, ()))),
    )
