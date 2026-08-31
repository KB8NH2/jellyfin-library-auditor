"""Shared HTML fragments and grouping helpers for report generation."""

from __future__ import annotations

from datetime import datetime
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path

import audit_types
from models import MediaItem
from report_theme import render_theme_bootstrap_script
from report_theme import render_theme_toggle


SEVERITY_SORT_ORDER = {
    audit_types.AuditSeverity.ERROR: 0,
    audit_types.AuditSeverity.WARNING: 1,
    audit_types.AuditSeverity.INFO: 2,
}
CHECK_DISPLAY_LABELS = {
    "missing_english_subtitles": "Missing English Subtitles",
    "missing_backdrop": "Missing Backdrop",
    "missing_primary_image": "Missing Primary Image",
    "missing_seasons": "Missing Seasons",
    "missing_episodes": "Missing Episodes",
    "missing_episode_number": "Missing Episode Number",
    "unknown_video_codec": "Unknown Video Codec",
    "unknown_audio_codec": "Unknown Audio Codec",
    "mismatched_episode_filename_title": "Mismatched Episode Filename Title",
    "mismatched_movie_filename_title": "Mismatched Movie Filename Title",
    "mismatched_tvdb_series": "Mismatched TheTVDB Series",
    "mismatched_tvdb_title": "Mismatched TheTVDB Title",
    "tvdb_title_not_english": "TheTVDB Title Not in English",
    "aired_dvd_order_mismatch": "Aired/DVD Order Mismatch",
}
CHECK_DISPLAY_ORDER = (
    "missing_primary_image",
    "missing_english_subtitles",
    "mismatched_tvdb_series",
    "missing_seasons",
    "missing_episodes",
    "missing_episode_number",
    "unknown_audio_codec",
    "unknown_video_codec",
)
CHECK_SUMMARY_LABELS = {
    "missing_english_subtitles": "English Subtitles",
    "missing_backdrop": "Backdrop",
    "missing_primary_image": "Primary Image",
    "missing_seasons": "Seasons",
    "missing_episodes": "Episodes",
    "missing_episode_number": "Episode Number",
    "unknown_video_codec": "Video Codec",
    "unknown_audio_codec": "Audio Codec",
    "aired_dvd_order_mismatch": "Episode Order",
    "mismatched_tvdb_series": "TheTVDB Match",
    "mismatched_tvdb_title": "TheTVDB Title",
    "tvdb_title_not_english": "TheTVDB Title Language",
}


@dataclass(frozen=True, slots=True)
class SummaryCard:
    title: str
    value: str
    accent: str
    href: str | None = None
    subtitle: str | None = None


@dataclass(frozen=True, slots=True)
class Breadcrumb:
    label: str
    href: str | None = None


@dataclass(frozen=True, slots=True)
class SitePaths:
    root_dir: Path
    index_path: Path
    libraries_dir: Path
    checks_dir: Path
    css_path: Path
    js_path: Path


@dataclass(frozen=True, slots=True)
class SiteLinks:
    library_slug_map: dict[str, str]
    check_filename_map: dict[str, str]
    media_anchor_map: dict[tuple[str, str], str]


def sort_findings(findings: tuple[audit_types.AuditFinding, ...]) -> tuple[audit_types.AuditFinding, ...]:
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


def group_findings_by_library(findings: tuple[audit_types.AuditFinding, ...]) -> dict[str, tuple[audit_types.AuditFinding, ...]]:
    grouped: dict[str, list[audit_types.AuditFinding]] = {}
    for finding in findings:
        grouped.setdefault(finding.media_item.library, []).append(finding)
    return {key: tuple(items) for key, items in grouped.items()}


def group_findings_by_check(findings: tuple[audit_types.AuditFinding, ...]) -> dict[str, tuple[audit_types.AuditFinding, ...]]:
    grouped: dict[str, list[audit_types.AuditFinding]] = {}
    for finding in findings:
        grouped.setdefault(finding.check_name, []).append(finding)
    return {key: tuple(items) for key, items in grouped.items()}


def group_findings_by_media(findings: tuple[audit_types.AuditFinding, ...]) -> dict[tuple[str, str], tuple[audit_types.AuditFinding, ...]]:
    grouped: dict[tuple[str, str], list[audit_types.AuditFinding]] = {}
    for finding in findings:
        grouped.setdefault(media_key(finding.media_item), []).append(finding)
    return {key: tuple(items) for key, items in grouped.items()}


def build_slug_map(names: tuple[str, ...]) -> dict[str, str]:
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
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().casefold())
    collapsed = re.sub(r"_+", "_", normalized).strip("_")
    return collapsed or "item"


def check_display_label(check_name: str) -> str:
    return CHECK_DISPLAY_LABELS.get(check_name, check_name.replace("_", " ").title())


def check_summary_label(check_name: str) -> str:
    return CHECK_SUMMARY_LABELS.get(check_name, check_display_label(check_name))


def check_sort_key(check_name: str) -> tuple[int, str]:
    try:
        return (CHECK_DISPLAY_ORDER.index(check_name), "")
    except ValueError:
        return (len(CHECK_DISPLAY_ORDER), check_display_label(check_name).casefold())


def media_key(item: MediaItem) -> tuple[str, str]:
    return (item.library, item.id)


def filename_title_attribute(item: MediaItem) -> str:
    """Return a title="" attribute showing an item's filename as a hover tooltip."""
    return f' title="{escape(item.path.name)}"'


def media_item_from_findings(findings: tuple[audit_types.AuditFinding, ...]) -> MediaItem:
    return findings[0].media_item


def media_anchor(item: MediaItem, site_links: SiteLinks) -> str:
    return site_links.media_anchor_map[media_key(item)]


def library_page_href(library_name: str, *, site_links: SiteLinks, relative_prefix: str) -> str:
    return f"{relative_prefix}libraries/{site_links.library_slug_map[library_name]}.html"


def check_page_href(check_name: str, *, site_links: SiteLinks, relative_prefix: str) -> str:
    return f"{relative_prefix}checks/{site_links.check_filename_map[check_name]}"


def library_row_href(item: MediaItem, *, site_links: SiteLinks, relative_prefix: str) -> str:
    return f"{library_page_href(item.library, site_links=site_links, relative_prefix=relative_prefix)}#{media_anchor(item, site_links)}"


def row_search_text(findings: tuple[audit_types.AuditFinding, ...]) -> str:
    item = media_item_from_findings(findings)
    parts = [item.display_name, item.title, item.library, item.series_name or "", item.season_name or ""]
    for finding in findings:
        parts.append(check_display_label(finding.check_name))
        parts.append(check_summary_label(finding.check_name))
        parts.append(finding.message)
    return " ".join(part.strip().lower() for part in parts if part.strip())


def render_page(*, title: str, current_nav: str, relative_prefix: str, heading: str, intro: str, content: str, breadcrumbs: tuple[Breadcrumb, ...], include_search: bool, include_expand_controls: bool, server_display_name: str = "", generated_at_text: str = "") -> str:
    toolbar_html = render_toolbar(include_expand_controls) if include_search else ""
    generated_at_html = (
        f'      <p class="page-generated-at">Generated {escape(generated_at_text)}</p>'
        if generated_at_text
        else ""
    )
    return "\n".join(
        (
            f'<main class="page-shell" data-nav-current="{escape(current_nav)}">',
            '  <header class="sticky-header">',
            render_navigation(relative_prefix, server_display_name=server_display_name),
            render_breadcrumbs(breadcrumbs),
            "    <section class=\"page-header-card\">",
            f"      <h1>{escape(heading)}</h1>",
            f"      <p class=\"page-intro\">{escape(intro)}</p>",
            generated_at_html,
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


def render_navigation(relative_prefix: str, *, server_display_name: str = "") -> str:
    server_name_html = (
        f'      <span class="nav-server-name">{escape(server_display_name)}</span>'
        if server_display_name
        else ""
    )
    return "\n".join(
        (
            '    <nav class="site-nav" aria-label="Primary">',
            *((server_name_html,) if server_name_html else ()),
            f'      <a class="nav-link" data-nav="Dashboard" href="{escape(f"{relative_prefix}index.html")}">Dashboard</a>',
            f'      <a class="nav-link" data-nav="Libraries" href="{escape(f"{relative_prefix}index.html#libraries-overview")}">Libraries</a>',
            f'      <a class="nav-link" data-nav="Checks" href="{escape(f"{relative_prefix}index.html#checks-overview")}">Audit Checks</a>',
            f"      {render_theme_toggle()}",
            "    </nav>",
        )
    )


def render_breadcrumbs(breadcrumbs: tuple[Breadcrumb, ...]) -> str:
    items: list[str] = []
    for breadcrumb in breadcrumbs:
        if breadcrumb.href:
            items.append(f'        <li><a href="{escape(breadcrumb.href)}">{escape(breadcrumb.label)}</a></li>')
        else:
            items.append(f'        <li aria-current="page">{escape(breadcrumb.label)}</li>')
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
    return "\n".join(
        (
            '  <section class="summary-card-grid">',
            *[render_summary_card(card) for card in cards],
            "  </section>",
        )
    )


def render_summary_card(card: SummaryCard) -> str:
    tag_name = "a" if card.href else "article"
    href_attribute = f' href="{escape(card.href)}"' if card.href else ""
    subtitle_html = f'<p class="summary-card-subtitle">{escape(card.subtitle)}</p>' if card.subtitle else ""
    return "\n".join(
        (
            f'    <{tag_name} class="summary-card summary-card-{escape(card.accent)}"{href_attribute}>',
            f"      <h3>{escape(card.title)}</h3>",
            f'      <p class="summary-card-value">{escape(card.value)}</p>',
            subtitle_html,
            f"    </{tag_name}>",
        )
    )


def render_row_count(count: int) -> str:
    """Return a row-count badge for placement next to a table heading."""
    return f' <span class="table-row-count" data-row-count>({count})</span>'


def render_sortable_table(headers: tuple[str, ...], rows: tuple[str, ...]) -> str:
    header_html = "".join(
        f'<th><button type="button" class="sort-button" data-column="{index}" onclick="sortReportTable(this)">{escape(label)}</button></th>'
        for index, label in enumerate(headers)
    )
    body_rows = rows or ('<tr class="empty-row" data-static-row><td colspan="99">No actionable items.</td></tr>',)
    return "\n".join(
        (
            '    <div class="table-shell table-scroll-shell">',
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
    return '<span class="status-label status-present">✓ present</span>' if present else '<span class="status-label status-missing">✗ missing</span>'


def status_sort_value(present: bool) -> str:
    """Return a stable sort value for present/missing cells."""
    return "1" if present else "0"


def season_sort_value(item: MediaItem) -> str:
    """Return a numeric-first season sort value."""
    if item.season_number is not None:
        return str(item.season_number)
    if item.season_name is None:
        return ""
    normalized = re.sub(r"^\s*season\s+", "", item.season_name, flags=re.IGNORECASE)
    season_text = normalized.strip()
    return season_text if season_text.isdigit() else season_text.casefold()


def episode_sort_value(item: MediaItem) -> str:
    """Return the episode number as a sortable value."""
    if item.episode_number is None:
        return ""
    return str(item.episode_number)


def check_row_sort_key(item: MediaItem) -> tuple[str, str, str, str, str]:
    """Return the default ordering key for multi-library check tables."""
    library_key = item.library.casefold()
    if item.is_episode:
        primary_key = (item.series_name or item.title).casefold()
        season_key = _sortable_text_token(season_sort_value(item))
        episode_key = _sortable_text_token(episode_sort_value(item))
    else:
        primary_key = item.title.casefold()
        season_key = ""
        episode_key = ""
    title_key = item.title.casefold()
    return (
        library_key,
        primary_key,
        season_key,
        episode_key,
        title_key,
    )


def check_row_library_sort_value(item: MediaItem) -> str:
    """Return a library-column sort value with full row tie-breakers."""
    return "|".join(check_row_sort_key(item))


def _sortable_text_token(value: str) -> str:
    """Return a lexically sortable token for numeric and text values."""
    if not value:
        return ""
    if value.isdigit():
        return f"0:{int(value):08d}"
    return f"1:{value.casefold()}"


def render_about_section(page_title: str) -> str:
    return "\n"


def page_document(
    *,
    title: str,
    relative_prefix: str,
    body: str,
    asset_prefix: str | None = None,
) -> str:
    resolved_asset_prefix = relative_prefix if asset_prefix is None else asset_prefix
    asset_version = datetime.now().strftime("%Y%m%d%H%M%S")
    return "\n".join(
        (
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            '  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">',
            '  <meta http-equiv="Pragma" content="no-cache">',
            '  <meta http-equiv="Expires" content="0">',
            f"  <title>{escape(title)}</title>",
            f"  {render_theme_bootstrap_script()}",
            f'  <link rel="stylesheet" href="{escape(f"{resolved_asset_prefix}css/style.css?v={asset_version}")}">',
            "</head>",
            "<body>",
            body,
            f'  <script src="{escape(f"{resolved_asset_prefix}js/report.js?v={asset_version}")}"></script>',
            "</body>",
            "</html>",
        )
    )


def finding_count_summary(findings: tuple[audit_types.AuditFinding, ...]) -> tuple[str, str, str]:
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
