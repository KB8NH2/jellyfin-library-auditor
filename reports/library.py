"""Library page generation for static audit reports."""

from __future__ import annotations

from media import has_english_subtitles
from media import has_jellyfin_logo
from media import has_jellyfin_primary_image
from media import local_poster_exists
from . import templates


TABLE_HEADERS = (
    "Title",
    "Poster",
    "Logo",
    "Primary Image",
    "English Subtitles",
    "Issues",
)


def render_library_page(library_name: str, findings: tuple, *, site_links: templates.SiteLinks) -> str:
    """Return one library report page."""
    media_groups = templates.group_findings_by_media(findings)
    cards = (
        templates.SummaryCard("Library", library_name, "library", subtitle="Current library"),
        templates.SummaryCard("Media Items With Issues", str(len(media_groups)), "media"),
        templates.SummaryCard("Actionable Findings", str(len(findings)), "findings"),
    )
    content = "\n".join(
        (
            templates.render_summary_cards(cards),
            '  <section class="section-card">',
            f"    <h2>{library_name}</h2>",
            "    <p class=\"muted-text\">Rows represent media items that need attention.</p>",
            templates.render_sortable_table(
                TABLE_HEADERS,
                _media_rows(findings, site_links=site_links, relative_prefix="../"),
            ),
            "  </section>",
        )
    )
    return templates.render_page(
        title=f"{library_name} Findings",
        current_nav="Libraries",
        relative_prefix="../",
        heading=library_name,
        intro="Sortable maintenance checklist for this library.",
        breadcrumbs=(
            templates.Breadcrumb("Dashboard", "../index.html"),
            templates.Breadcrumb("Libraries", "../index.html#libraries-overview"),
            templates.Breadcrumb(library_name),
        ),
        include_search=True,
        include_expand_controls=False,
        content=content,
    )


def _media_rows(findings: tuple, *, site_links: templates.SiteLinks, relative_prefix: str) -> tuple[str, ...]:
    """Return one sortable row per media item."""
    grouped = templates.group_findings_by_media(findings)
    rows: list[str] = []
    for _, media_findings in sorted(
        grouped.items(),
        key=lambda entry: templates.media_item_from_findings(entry[1]).display_name.casefold(),
    ):
        item = templates.media_item_from_findings(media_findings)
        rows.append(
            "\n".join(
                (
                    f'          <tr id="{templates.media_anchor(item, site_links)}" data-search-row data-search="{templates.row_search_text(media_findings)}">',
                    f"            <td>{templates.escape(item.display_name)}</td>",
                    f"            <td>{templates.render_status_label(local_poster_exists(item))}</td>",
                    f"            <td>{templates.render_status_label(has_jellyfin_logo(item))}</td>",
                    f"            <td>{templates.render_status_label(has_jellyfin_primary_image(item))}</td>",
                    f"            <td>{templates.render_status_label(has_english_subtitles(item))}</td>",
                    f"            <td>{_findings_summary(media_findings, site_links=site_links, relative_prefix=relative_prefix)}</td>",
                    "          </tr>",
                )
            )
        )
    return tuple(rows)


def _findings_summary(findings: tuple, *, site_links: templates.SiteLinks, relative_prefix: str) -> str:
    """Return a compact linked findings summary."""
    unique_checks: list[str] = []
    seen: set[str] = set()
    for finding in templates.sort_findings(findings):
        if finding.check_name in seen:
            continue
        seen.add(finding.check_name)
        unique_checks.append(finding.check_name)
    parts = [
        f'<a href="{templates.check_page_href(check_name, site_links=site_links, relative_prefix=relative_prefix)}">{templates.escape(templates.check_summary_label(check_name))}</a>'
        for check_name in unique_checks
    ]
    return ", ".join(parts)
