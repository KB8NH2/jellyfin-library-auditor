"""Audit check page generation for static audit reports."""

from __future__ import annotations

from . import templates


TABLE_HEADERS = (
    "Library",
    "Title",
    "Series",
    "Season",
    "Episode",
)


def render_check_page(check_name: str, findings: tuple, *, site_links: templates.SiteLinks) -> str:
    """Return one audit check page."""
    media_groups = templates.group_findings_by_media(findings)
    cards = (
        templates.SummaryCard("Audit Check", templates.check_display_label(check_name), "check", subtitle=check_name),
        templates.SummaryCard("Media Items", str(len(media_groups)), "media"),
        templates.SummaryCard("Findings", str(len(findings)), "findings"),
    )
    content = "\n".join(
        (
            templates.render_summary_cards(cards),
            '  <section class="section-card">',
            f"    <h2>{templates.check_display_label(check_name)}</h2>",
            "    <p class=\"muted-text\">Rows represent media items requiring this exact fix.</p>",
            templates.render_sortable_table(TABLE_HEADERS, _check_rows(findings, site_links=site_links)),
            "  </section>",
        )
    )
    return templates.render_page(
        title=f"{templates.check_display_label(check_name)} Findings",
        current_nav="Checks",
        relative_prefix="../",
        heading=templates.check_display_label(check_name),
        intro="Sortable list of media items for this audit check.",
        breadcrumbs=(
            templates.Breadcrumb("Dashboard", "../index.html"),
            templates.Breadcrumb("Audit Checks", "../index.html#checks-overview"),
            templates.Breadcrumb(templates.check_display_label(check_name)),
        ),
        include_search=True,
        include_expand_controls=False,
        content=content,
    )


def _check_rows(findings: tuple, *, site_links: templates.SiteLinks) -> tuple[str, ...]:
    """Return one table row per media item on a check page."""
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
                    f'          <tr data-search-row data-search="{templates.row_search_text(media_findings)}">',
                    f'            <td><a href="{templates.library_page_href(item.library, site_links=site_links, relative_prefix="../")}">{templates.escape(item.library)}</a></td>',
                    f'            <td><a href="{templates.library_row_href(item, site_links=site_links, relative_prefix="../")}">{templates.escape(item.title)}</a></td>',
                    f"            <td>{templates.escape(item.series_name or '')}</td>",
                    f'            <td data-sort-value="{templates.escape(templates.season_sort_value(item))}">{templates.escape(item.season_name or "")}</td>',
                    f'            <td data-sort-value="{templates.escape(templates.episode_sort_value(item))}">{"" if item.episode_number is None else item.episode_number}</td>',
                    "          </tr>",
                )
            )
        )
    return tuple(rows)
