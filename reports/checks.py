"""Audit check page generation for static audit reports."""

from __future__ import annotations

from media import expected_episode_title_from_filename
from media import expected_episode_title_from_stream_titles
from media import expected_movie_title_from_filename
from media import get_display_episode_number

from . import templates


DEFAULT_TABLE_HEADERS = (
    "Library",
    "Series",
    "Season",
    "Episode",
    "Title",
    "Details",
)


def render_check_page(
    check_name: str,
    findings: tuple,
    *,
    site_links: templates.SiteLinks,
    server_display_name: str = "",
    generated_at_text: str = "",
) -> str:
    """Return one audit check page."""
    media_groups = templates.group_findings_by_media(findings)
    check_rows = _check_rows(findings, site_links=site_links, check_name=check_name)
    cards = (
        templates.SummaryCard("Audit Check", templates.check_display_label(check_name), "check", subtitle=check_name),
        templates.SummaryCard("Media Items", str(len(media_groups)), "media"),
        templates.SummaryCard("Findings", str(len(findings)), "findings"),
    )
    content = "\n".join(
        (
            templates.render_summary_cards(cards),
            '  <section class="section-card">',
            f"    <h2>{templates.check_display_label(check_name)}{templates.render_row_count(len(check_rows))}</h2>",
            "    <p class=\"muted-text\">Rows represent media items requiring this exact fix.</p>",
            templates.render_sortable_table(_table_headers(check_name), check_rows),
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
        server_display_name=server_display_name,
        generated_at_text=generated_at_text,
        content=content,
    )


def _table_headers(check_name: str) -> tuple[str, ...]:
    """Return the table headers for one check page."""
    if check_name == "missing_episodes":
        return ("Library", "Series", "Season", "Title", "Details")
    if check_name == "missing_seasons":
        return ("Library", "Series", "Title", "Details")
    if check_name == "mismatched_tvdb_series":
        return ("Library", "Series", "Title", "Details")
    if check_name == "mismatched_episode_filename_title":
        return ("Library", "Series", "Season", "Episode", "Title", "Suggested Title (Filename)")
    if check_name == "mismatched_episode_stream_title":
        return ("Library", "Series", "Season", "Episode", "Title", "Suggested Title (Stream)")
    if check_name == "mismatched_movie_filename_title":
        return ("Library", "Title", "Suggested Title (Filename)")
    return DEFAULT_TABLE_HEADERS


def _check_rows(
    findings: tuple,
    *,
    site_links: templates.SiteLinks,
    check_name: str = "",
) -> tuple[str, ...]:
    """Return one table row per media item on a check page."""
    grouped = templates.group_findings_by_media(findings)
    rows: list[str] = []
    for _, media_findings in sorted(
        grouped.items(),
        key=lambda entry: templates.check_row_sort_key(
            templates.media_item_from_findings(entry[1])
        ),
    ):
        item = templates.media_item_from_findings(media_findings)
        if check_name == "mismatched_episode_filename_title":
            rows.append(_mismatched_episode_filename_title_row(item, media_findings, site_links=site_links))
            continue
        if check_name == "mismatched_episode_stream_title":
            rows.append(_mismatched_episode_stream_title_row(item, media_findings, site_links=site_links))
            continue
        if check_name == "mismatched_movie_filename_title":
            rows.append(_mismatched_movie_filename_title_row(item, media_findings, site_links=site_links))
            continue
        rows.append(
            "\n".join(
                (
                    f'          <tr data-search-row data-search="{templates.row_search_text(media_findings)}">',
                    f'            <td data-sort-value="{templates.escape(templates.check_row_library_sort_value(item))}"><a href="{templates.library_page_href(item.library, site_links=site_links, relative_prefix="../")}">{templates.escape(item.library)}</a></td>',
                    f"            <td>{templates.escape(item.series_name or '')}</td>",
                    *_optional_row_cells(check_name, item),
                    f'            <td><a href="{templates.library_row_href(item, site_links=site_links, relative_prefix="../")}"{templates.filename_title_attribute(item)}>{templates.escape(item.title)}</a></td>',
                    f"            <td>{_finding_messages(media_findings)}</td>",
                    "          </tr>",
                )
            )
        )
    return tuple(rows)


def _mismatched_episode_filename_title_row(
    item: templates.MediaItem,
    media_findings: tuple,
    *,
    site_links: templates.SiteLinks,
) -> str:
    """Return one table row for the mismatched episode filename title check."""
    suggested_title = expected_episode_title_from_filename(item) or ""
    return "\n".join(
        (
            f'          <tr data-search-row data-search="{templates.row_search_text(media_findings)}">',
            f'            <td data-sort-value="{templates.escape(templates.check_row_library_sort_value(item))}"><a href="{templates.library_page_href(item.library, site_links=site_links, relative_prefix="../")}">{templates.escape(item.library)}</a></td>',
            f"            <td>{templates.escape(item.series_name or '')}</td>",
            f'            <td data-sort-value="{templates.escape(templates.season_sort_value(item))}">{templates.escape(item.season_name or "")}</td>',
            f'            <td data-sort-value="{templates.escape(templates.episode_sort_value(item))}">{templates.escape(get_display_episode_number(item))}</td>',
            f'            <td><a href="{templates.library_row_href(item, site_links=site_links, relative_prefix="../")}"{templates.filename_title_attribute(item)}>{templates.escape(item.title)}</a></td>',
            f"            <td>{templates.escape(suggested_title)}</td>",
            "          </tr>",
        )
    )


def _mismatched_episode_stream_title_row(
    item: templates.MediaItem,
    media_findings: tuple,
    *,
    site_links: templates.SiteLinks,
) -> str:
    """Return one table row for the mismatched episode stream title check."""
    suggested_title = expected_episode_title_from_stream_titles(item) or ""
    return "\n".join(
        (
            f'          <tr data-search-row data-search="{templates.row_search_text(media_findings)}">',
            f'            <td data-sort-value="{templates.escape(templates.check_row_library_sort_value(item))}"><a href="{templates.library_page_href(item.library, site_links=site_links, relative_prefix="../")}">{templates.escape(item.library)}</a></td>',
            f"            <td>{templates.escape(item.series_name or '')}</td>",
            f'            <td data-sort-value="{templates.escape(templates.season_sort_value(item))}">{templates.escape(item.season_name or "")}</td>',
            f'            <td data-sort-value="{templates.escape(templates.episode_sort_value(item))}">{templates.escape(get_display_episode_number(item))}</td>',
            f'            <td><a href="{templates.library_row_href(item, site_links=site_links, relative_prefix="../")}"{templates.filename_title_attribute(item)}>{templates.escape(item.title)}</a></td>',
            f"            <td>{templates.escape(suggested_title)}</td>",
            "          </tr>",
        )
    )


def _mismatched_movie_filename_title_row(
    item: templates.MediaItem,
    media_findings: tuple,
    *,
    site_links: templates.SiteLinks,
) -> str:
    """Return one table row for the mismatched movie filename title check."""
    suggested_title = expected_movie_title_from_filename(item) or ""
    return "\n".join(
        (
            f'          <tr data-search-row data-search="{templates.row_search_text(media_findings)}">',
            f'            <td data-sort-value="{templates.escape(templates.check_row_library_sort_value(item))}"><a href="{templates.library_page_href(item.library, site_links=site_links, relative_prefix="../")}">{templates.escape(item.library)}</a></td>',
            f'            <td><a href="{templates.library_row_href(item, site_links=site_links, relative_prefix="../")}"{templates.filename_title_attribute(item)}>{templates.escape(item.title)}</a></td>',
            f"            <td>{templates.escape(suggested_title)}</td>",
            "          </tr>",
        )
    )


def _optional_row_cells(check_name: str, item: templates.MediaItem) -> tuple[str, ...]:
    """Return any season and episode cells needed for the current check page."""
    cells: list[str] = []
    if check_name not in {"missing_seasons", "mismatched_tvdb_series"}:
        cells.append(
            f'            <td data-sort-value="{templates.escape(templates.season_sort_value(item))}">{templates.escape(item.season_name or "")}</td>'
        )
    if check_name not in {"missing_episodes", "missing_seasons", "mismatched_tvdb_series"}:
        cells.append(
            f'            <td data-sort-value="{templates.escape(templates.episode_sort_value(item))}">{templates.escape(get_display_episode_number(item))}</td>'
        )
    return tuple(cells)


def _finding_messages(findings: tuple) -> str:
    """Return unique finding messages formatted for one table cell."""
    messages: list[str] = []
    seen: set[str] = set()
    for finding in findings:
        if finding.message in seen:
            continue
        seen.add(finding.message)
        messages.append(templates.escape(finding.message))
    return "<br>".join(messages)
