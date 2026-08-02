"""Generate static comparison reports from completed audit results."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
import shutil

from config import get_config
from media import get_primary_audio_codec
from media import get_video_codec
from media import has_english_subtitles
from media import has_jellyfin_logo
from media import has_jellyfin_primary_image
from media import local_backdrop_exists
from media import local_poster_exists
from models import MediaItem
from output_layout import audit_results_root
from output_layout import comparison_output_dir
from output_layout import shared_css_path
from output_layout import shared_js_path
from reports.css import write_css
from reports.javascript import write_javascript
from results import AuditServerResult


DEFAULT_COMPARISON_OUTPUT_DIR = Path("audit_results") / "comparison_results"


@dataclass(frozen=True, slots=True)
class MatchedPair:
    """Represents one matched media item pair across two servers."""

    left: MediaItem
    right: MediaItem
    library: str


def write_comparison_reports(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
    output_dir: Path | None = None,
) -> Path:
    """Write a static comparison site for two completed audit results."""
    root_dir = _default_output_dir() if output_dir is None else output_dir
    output_root = root_dir.parent
    if root_dir.exists():
        shutil.rmtree(root_dir)
    root_dir.mkdir(parents=True, exist_ok=True)

    write_css(shared_css_path(output_root))
    write_javascript(shared_js_path(output_root))

    comparison = _build_comparison(left_result, right_result)
    (root_dir / "index.html").write_text(
        _page_document(
            title="Server Comparison",
            body=_index_page(left_result, right_result, comparison),
            asset_prefix="../",
        ),
        encoding="utf-8",
    )
    (root_dir / "libraries.html").write_text(
        _page_document(
            title="Library Comparison",
            body=_libraries_page(left_result, right_result, comparison),
            asset_prefix="../",
        ),
        encoding="utf-8",
    )
    (root_dir / "artwork.html").write_text(
        _page_document(
            title="Artwork Comparison",
            body=_artwork_page(left_result, right_result, comparison),
            asset_prefix="../",
        ),
        encoding="utf-8",
    )
    (root_dir / "subtitles.html").write_text(
        _page_document(
            title="Subtitle Comparison",
            body=_subtitles_page(left_result, right_result, comparison),
            asset_prefix="../",
        ),
        encoding="utf-8",
    )
    (root_dir / "configuration.html").write_text(
        _page_document(
            title="Configuration Comparison",
            body=_configuration_page(left_result, right_result, comparison),
            asset_prefix="../",
        ),
        encoding="utf-8",
    )

    return root_dir / "index.html"


def _build_comparison(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
) -> dict[str, object]:
    """Return all computed comparison sections."""
    left_libraries = {item.library.name: item.library for item in left_result.library_results}
    right_libraries = {item.library.name: item.library for item in right_result.library_results}
    missing_left_libraries = tuple(
        sorted(set(right_libraries) - set(left_libraries), key=str.casefold)
    )
    missing_right_libraries = tuple(
        sorted(set(left_libraries) - set(right_libraries), key=str.casefold)
    )

    matched_pairs: list[MatchedPair] = []
    missing_left_media: list[tuple[str, object]] = []
    missing_right_media: list[tuple[str, object]] = []
    artwork_differences: list[MatchedPair] = []
    subtitle_differences: list[MatchedPair] = []
    metadata_differences: list[tuple[str, str, str, str, str]] = []
    library_configuration_differences: list[tuple[str, str, str]] = []

    common_libraries = sorted(set(left_libraries) & set(right_libraries), key=str.casefold)
    for library_name in common_libraries:
        left_library = left_libraries[library_name]
        right_library = right_libraries[library_name]
        if left_library.collection_type != right_library.collection_type:
            library_configuration_differences.append(
                (
                    library_name,
                    left_library.collection_type or "",
                    right_library.collection_type or "",
                )
            )

        left_items = _library_items(left_result, library_name)
        right_items = _library_items(right_result, library_name)
        library_pairs, library_missing_left, library_missing_right = _pair_library_items(
            library_name,
            left_items,
            right_items,
        )
        matched_pairs.extend(library_pairs)
        missing_left_media.extend(library_missing_left)
        missing_right_media.extend(library_missing_right)

    for pair in matched_pairs:
        if _artwork_differs(pair.left, pair.right):
            artwork_differences.append(pair)
        if has_english_subtitles(pair.left) != has_english_subtitles(pair.right):
            subtitle_differences.append(pair)
        metadata_differences.extend(_metadata_differences(pair))

    return {
        "missing_left_libraries": missing_left_libraries,
        "missing_right_libraries": missing_right_libraries,
        "missing_left_media": tuple(missing_left_media),
        "missing_right_media": tuple(missing_right_media),
        "artwork_differences": tuple(artwork_differences),
        "subtitle_differences": tuple(subtitle_differences),
        "metadata_differences": tuple(metadata_differences),
        "library_configuration_differences": tuple(library_configuration_differences),
    }


def _library_items(result: AuditServerResult, library_name: str) -> tuple:
    """Return audited items for one library name."""
    for library_result in result.library_results:
        if library_result.library.name == library_name:
            return library_result.audited_items
    return ()


def _pair_library_items(
    library_name: str,
    left_items: tuple,
    right_items: tuple,
) -> tuple[list[MatchedPair], list[tuple[str, object]], list[tuple[str, object]]]:
    """Pair media items across two libraries."""
    left_groups = _group_items_by_identity(left_items)
    right_groups = _group_items_by_identity(right_items)
    matched_pairs: list[MatchedPair] = []
    missing_left_media: list[tuple[str, object]] = []
    missing_right_media: list[tuple[str, object]] = []

    for identity in sorted(set(left_groups) | set(right_groups), key=str):
        left_group = left_groups.get(identity, [])
        right_group = right_groups.get(identity, [])
        matched, missing_left, missing_right = _pair_item_group(left_group, right_group)
        matched_pairs.extend(
            MatchedPair(left=left_item, right=right_item, library=library_name)
            for left_item, right_item in matched
        )
        missing_left_media.extend((library_name, item) for item in missing_left)
        missing_right_media.extend((library_name, item) for item in missing_right)

    return matched_pairs, missing_left_media, missing_right_media


def _group_items_by_identity(items: tuple) -> dict[tuple, list]:
    """Group items by a comparison identity."""
    grouped: dict[tuple, list] = {}
    for item in items:
        grouped.setdefault(_comparison_identity(item), []).append(item)
    return grouped


def _pair_item_group(left_items: list, right_items: list) -> tuple[list[tuple[object, object]], list, list]:
    """Pair items within one identity group."""
    left_by_id = {item.id: item for item in left_items}
    right_by_id = {item.id: item for item in right_items}

    matched: list[tuple[object, object]] = []
    matched_ids = sorted(set(left_by_id) & set(right_by_id))
    for item_id in matched_ids:
        matched.append((left_by_id[item_id], right_by_id[item_id]))

    remaining_left = [
        item
        for item in sorted(left_items, key=lambda candidate: candidate.display_name.casefold())
        if item.id not in set(matched_ids)
    ]
    remaining_right = [
        item
        for item in sorted(right_items, key=lambda candidate: candidate.display_name.casefold())
        if item.id not in set(matched_ids)
    ]

    pair_count = min(len(remaining_left), len(remaining_right))
    for index in range(pair_count):
        matched.append((remaining_left[index], remaining_right[index]))

    return (
        matched,
        remaining_left[pair_count:],
        remaining_right[pair_count:],
    )


def _comparison_identity(item) -> tuple:
    """Return a best-effort cross-server identity for a media item."""
    if item.is_episode:
        return (
            "episode",
            (item.series_name or "").casefold(),
            item.season_number,
            item.episode_number,
            item.title.casefold(),
        )
    return (
        "movie",
        item.title.casefold(),
    )


def _artwork_differs(left_item, right_item) -> bool:
    """Return whether artwork presence differs between two matched items."""
    return any(
        (
            local_poster_exists(left_item) != local_poster_exists(right_item),
            local_backdrop_exists(left_item) != local_backdrop_exists(right_item),
            has_jellyfin_primary_image(left_item) != has_jellyfin_primary_image(right_item),
            has_jellyfin_logo(left_item) != has_jellyfin_logo(right_item),
        )
    )


def _metadata_differences(pair: MatchedPair) -> tuple[tuple[str, str, str, str, str], ...]:
    """Return metadata difference rows for one matched pair."""
    rows: list[tuple[str, str, str, str, str]] = []
    comparisons = (
        ("Year", _display_value(pair.left.year), _display_value(pair.right.year)),
        ("Resolution", _display_value(pair.left.resolution), _display_value(pair.right.resolution)),
        ("Video Codec", _display_value(get_video_codec(pair.left)), _display_value(get_video_codec(pair.right))),
        ("Audio Codec", _display_value(get_primary_audio_codec(pair.left)), _display_value(get_primary_audio_codec(pair.right))),
    )
    for field_name, left_value, right_value in comparisons:
        if left_value == right_value:
            continue
        rows.append(
            (
                pair.library,
                pair.left.display_name,
                field_name,
                left_value,
                right_value,
            )
        )
    return tuple(rows)


def _index_page(left_result: AuditServerResult, right_result: AuditServerResult, comparison: dict[str, object]) -> str:
    """Return comparison overview page body."""
    cards = "\n".join(
        (
            _summary_card("Left Server", left_result.server_name or left_result.server_key or "Left"),
            _summary_card("Right Server", right_result.server_name or right_result.server_key or "Right"),
            _summary_card("Missing Libraries", str(len(comparison["missing_left_libraries"]) + len(comparison["missing_right_libraries"]))),
            _summary_card("Missing Media", str(len(comparison["missing_left_media"]) + len(comparison["missing_right_media"]))),
            _summary_card("Artwork Differences", str(len(comparison["artwork_differences"]))),
            _summary_card("Subtitle Differences", str(len(comparison["subtitle_differences"]))),
        )
    )
    links = "\n".join(
        (
            _nav_cards(("libraries.html", "Libraries"), ("artwork.html", "Artwork"), ("subtitles.html", "Subtitles"), ("configuration.html", "Configuration")),
        )
    )
    return _page_shell(
        "Comparison Overview",
        "Compare two completed audit results.",
        "\n".join(
            (
                '  <section class="summary-card-grid">',
                cards,
                "  </section>",
                '  <section class="section-card">',
                "    <h2>Comparison Pages</h2>",
                "    <p class=\"muted-text\">Open the focused comparison views below.</p>",
                links,
                "  </section>",
            )
        ),
        current_nav="Overview",
    )


def _libraries_page(left_result: AuditServerResult, right_result: AuditServerResult, comparison: dict[str, object]) -> str:
    """Return libraries comparison page body."""
    return _page_shell(
        "Libraries Comparison",
        "Missing libraries and media items between both servers.",
        "\n".join(
            (
                _simple_table_section(
                    "Libraries Missing From Left",
                    ("Library",),
                    tuple(
                        f"<tr><td>{escape(name)}</td></tr>"
                        for name in comparison["missing_left_libraries"]
                    ),
                ),
                _simple_table_section(
                    "Libraries Missing From Right",
                    ("Library",),
                    tuple(
                        f"<tr><td>{escape(name)}</td></tr>"
                        for name in comparison["missing_right_libraries"]
                    ),
                ),
                _simple_table_section(
                    f"Media Missing From {escape(left_result.server_name or left_result.server_key or 'Left')}",
                    ("Library", "Title", "Series", "Season", "Episode"),
                    tuple(
                        _media_missing_row(library_name, item)
                        for library_name, item in comparison["missing_left_media"]
                    ),
                ),
                _simple_table_section(
                    f"Media Missing From {escape(right_result.server_name or right_result.server_key or 'Right')}",
                    ("Library", "Title", "Series", "Season", "Episode"),
                    tuple(
                        _media_missing_row(library_name, item)
                        for library_name, item in comparison["missing_right_media"]
                    ),
                ),
            )
        ),
        current_nav="Libraries",
        include_search=True,
    )


def _artwork_page(left_result: AuditServerResult, right_result: AuditServerResult, comparison: dict[str, object]) -> str:
    """Return artwork comparison page body."""
    rows = tuple(
        _artwork_row(left_result, right_result, pair)
        for pair in comparison["artwork_differences"]
    )
    return _page_shell(
        "Artwork Comparison",
        "Differences in local and Jellyfin artwork presence.",
        _simple_table_section(
            "Artwork Differences",
            (
                "Library",
                "Title",
                f"{left_result.server_name or left_result.server_key or 'Left'} Poster",
                f"{right_result.server_name or right_result.server_key or 'Right'} Poster",
                f"{left_result.server_name or left_result.server_key or 'Left'} Backdrop",
                f"{right_result.server_name or right_result.server_key or 'Right'} Backdrop",
                f"{left_result.server_name or left_result.server_key or 'Left'} Primary",
                f"{right_result.server_name or right_result.server_key or 'Right'} Primary",
                f"{left_result.server_name or left_result.server_key or 'Left'} Logo",
                f"{right_result.server_name or right_result.server_key or 'Right'} Logo",
            ),
            rows,
        ),
        current_nav="Artwork",
        include_search=True,
    )


def _subtitles_page(left_result: AuditServerResult, right_result: AuditServerResult, comparison: dict[str, object]) -> str:
    """Return subtitles comparison page body."""
    rows = tuple(
        _subtitle_row(left_result, right_result, pair)
        for pair in comparison["subtitle_differences"]
    )
    return _page_shell(
        "Subtitle Comparison",
        "Differences in English subtitle availability.",
        _simple_table_section(
            "Subtitle Differences",
            (
                "Library",
                "Title",
                "Series",
                "Season",
                "Episode",
                f"{left_result.server_name or left_result.server_key or 'Left'} English Subtitles",
                f"{right_result.server_name or right_result.server_key or 'Right'} English Subtitles",
            ),
            rows,
        ),
        current_nav="Subtitles",
        include_search=True,
    )


def _configuration_page(left_result: AuditServerResult, right_result: AuditServerResult, comparison: dict[str, object]) -> str:
    """Return configuration and metadata comparison page body."""
    library_rows = tuple(
        "\n".join(
            (
                f'<tr data-search-row data-search="{escape((library_name + " " + left_value + " " + right_value).lower())}">',
                f"  <td>{escape(library_name)}</td>",
                f"  {_diff_cell(left_value, is_different=True)}",
                f"  {_diff_cell(right_value, is_different=True)}",
                "</tr>",
            )
        )
        for library_name, left_value, right_value in comparison["library_configuration_differences"]
    )
    metadata_rows = tuple(
        "\n".join(
            (
                f'<tr data-search-row data-search="{escape((library + " " + title + " " + field_name + " " + left_value + " " + right_value).lower())}">',
                f"  <td>{escape(library)}</td>",
                f"  <td>{escape(title)}</td>",
                f"  <td>{escape(field_name)}</td>",
                f"  {_diff_cell(left_value, is_different=True)}",
                f"  {_diff_cell(right_value, is_different=True)}",
                "</tr>",
            )
        )
        for library, title, field_name, left_value, right_value in comparison["metadata_differences"]
    )
    return _page_shell(
        "Configuration Comparison",
        "Library configuration and media metadata differences.",
        "\n".join(
            (
                _simple_table_section(
                    "Library Configuration Differences",
                    (
                        "Library",
                        f"{left_result.server_name or left_result.server_key or 'Left'} Collection Type",
                        f"{right_result.server_name or right_result.server_key or 'Right'} Collection Type",
                    ),
                    library_rows,
                ),
                _simple_table_section(
                    "Metadata Differences",
                    (
                        "Library",
                        "Title",
                        "Field",
                        f"{left_result.server_name or left_result.server_key or 'Left'}",
                        f"{right_result.server_name or right_result.server_key or 'Right'}",
                    ),
                    metadata_rows,
                ),
            )
        ),
        current_nav="Configuration",
        include_search=True,
    )


def _default_output_dir() -> Path:
    """Return the configured default comparison output directory."""
    return comparison_output_dir(audit_results_root(get_config().reporting.output.audit_html))


def _page_document(*, title: str, body: str, asset_prefix: str) -> str:
    """Return a full standalone HTML document."""
    return "\n".join(
        (
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{escape(title)}</title>",
            f'  <link rel="stylesheet" href="{escape(f"{asset_prefix}css/style.css")}">',
            "</head>",
            "<body>",
            body,
            f'  <script src="{escape(f"{asset_prefix}js/report.js")}"></script>',
            "</body>",
            "</html>",
        )
    )


def _page_shell(
    heading: str,
    intro: str,
    content: str,
    *,
    current_nav: str,
    include_search: bool = False,
) -> str:
    """Return one comparison page body."""
    return "\n".join(
        (
            f'<main class="page-shell" data-nav-current="{escape(current_nav)}">',
            _comparison_nav(current_nav),
            '  <section class="page-header-card">',
            f"    <h1>{escape(heading)}</h1>",
            f'    <p class="page-intro">{escape(intro)}</p>',
            "  </section>",
            _search_toolbar() if include_search else "",
            '  <section class="page-content">',
            content,
            "  </section>",
            "</main>",
        )
    )


def _comparison_nav(current_nav: str) -> str:
    """Return comparison site navigation."""
    pages = (
        ("Overview", "index.html"),
        ("Libraries", "libraries.html"),
        ("Artwork", "artwork.html"),
        ("Subtitles", "subtitles.html"),
        ("Configuration", "configuration.html"),
    )
    links = []
    for label, href in pages:
        active_class = " is-active" if label == current_nav else ""
        links.append(
            f'<a class="nav-link{active_class}" href="{escape(href)}">{escape(label)}</a>'
        )
    return "\n".join(
        (
            '  <nav class="site-nav" aria-label="Comparison navigation">',
            *[f"    {link}" for link in links],
            "  </nav>",
        )
    )


def _search_toolbar() -> str:
    """Return the shared comparison search toolbar."""
    return "\n".join(
        (
            '  <section class="toolbar-card" id="page-search">',
            "    <div>",
            "      <h2>Search</h2>",
            "      <p class=\"muted-text\">Filter visible rows instantly.</p>",
            "    </div>",
            '    <div class="toolbar-controls">',
            '      <label class="search-field">',
            "        <span>Search rows</span>",
            '        <input id="report-search" type="search" placeholder="Search rows..." autocomplete="off">',
            "      </label>",
            "    </div>",
            "  </section>",
        )
    )


def _simple_table_section(title: str, headers: tuple[str, ...], rows: tuple[str, ...]) -> str:
    """Return one simple table section."""
    header_html = "".join(
        f'<th><button type="button" class="sort-button" data-column="{index}" onclick="sortReportTable(this)">{escape(label)}</button></th>'
        for index, label in enumerate(headers)
    )
    body_rows = rows or ('<tr class="empty-row"><td colspan="99">No differences found.</td></tr>',)
    return "\n".join(
        (
            '  <section class="section-card">',
            f"    <h2>{escape(title)}</h2>",
            '    <div class="table-shell">',
            '      <table class="data-table">',
            f"        <thead><tr>{header_html}</tr></thead>",
            "        <tbody>",
            *body_rows,
            "        </tbody>",
            "      </table>",
            "    </div>",
            "  </section>",
        )
    )


def _summary_card(title: str, value: str) -> str:
    """Return one comparison summary card."""
    return "\n".join(
        (
            '    <article class="summary-card summary-card-findings">',
            f"      <h3>{escape(title)}</h3>",
            f'      <p class="summary-card-value">{escape(value)}</p>',
            "    </article>",
        )
    )


def _nav_cards(*cards: tuple[str, str]) -> str:
    """Return a grid of navigation cards."""
    rendered_cards = [
        "\n".join(
            (
                f'      <a class="summary-card summary-card-check" href="{escape(href)}">',
                f"        <h3>{escape(label)}</h3>",
                "      </a>",
            )
        )
        for href, label in cards
    ]
    return "\n".join(
        (
            '    <section class="summary-card-grid">',
            *rendered_cards,
            "    </section>",
        )
    )


def _media_missing_row(library_name: str, item) -> str:
    """Return one missing-media row."""
    search_text = " ".join(
        part for part in (
            library_name,
            item.title,
            item.series_name or "",
            item.season_name or "",
            str(item.episode_number or ""),
        ) if part
    ).lower()
    return (
        f'<tr data-search-row data-search="{escape(search_text)}"><td>{escape(library_name)}</td>'
        f'<td>{escape(item.title)}</td><td>{escape(item.series_name or "")}</td>'
        f'<td>{escape(item.season_name or "")}</td><td>{"" if item.episode_number is None else item.episode_number}</td></tr>'
    )


def _artwork_row(left_result: AuditServerResult, right_result: AuditServerResult, pair: MatchedPair) -> str:
    """Return one artwork difference row."""
    search_text = f"{pair.library} {pair.left.display_name}".lower()
    left_poster = _yes_no(local_poster_exists(pair.left))
    right_poster = _yes_no(local_poster_exists(pair.right))
    left_backdrop = _yes_no(local_backdrop_exists(pair.left))
    right_backdrop = _yes_no(local_backdrop_exists(pair.right))
    left_primary = _yes_no(has_jellyfin_primary_image(pair.left))
    right_primary = _yes_no(has_jellyfin_primary_image(pair.right))
    left_logo = _yes_no(has_jellyfin_logo(pair.left))
    right_logo = _yes_no(has_jellyfin_logo(pair.right))
    return (
        f'<tr data-search-row data-search="{escape(search_text)}"><td>{escape(pair.library)}</td>'
        f'<td>{escape(pair.left.display_name)}</td>'
        f'{_diff_cell(left_poster, is_different=left_poster != right_poster)}{_diff_cell(right_poster, is_different=left_poster != right_poster)}'
        f'{_diff_cell(left_backdrop, is_different=left_backdrop != right_backdrop)}{_diff_cell(right_backdrop, is_different=left_backdrop != right_backdrop)}'
        f'{_diff_cell(left_primary, is_different=left_primary != right_primary)}{_diff_cell(right_primary, is_different=left_primary != right_primary)}'
        f'{_diff_cell(left_logo, is_different=left_logo != right_logo)}{_diff_cell(right_logo, is_different=left_logo != right_logo)}</tr>'
    )


def _subtitle_row(left_result: AuditServerResult, right_result: AuditServerResult, pair: MatchedPair) -> str:
    """Return one subtitle difference row."""
    search_text = f"{pair.library} {pair.left.display_name} subtitles".lower()
    left_subtitles = _yes_no(has_english_subtitles(pair.left))
    right_subtitles = _yes_no(has_english_subtitles(pair.right))
    return (
        f'<tr data-search-row data-search="{escape(search_text)}"><td>{escape(pair.library)}</td>'
        f'<td>{escape(pair.left.title)}</td><td>{escape(pair.left.series_name or "")}</td>'
        f'<td>{escape(pair.left.season_name or "")}</td><td>{"" if pair.left.episode_number is None else pair.left.episode_number}</td>'
        f'{_diff_cell(left_subtitles, is_different=left_subtitles != right_subtitles)}{_diff_cell(right_subtitles, is_different=left_subtitles != right_subtitles)}</tr>'
    )


def _display_value(value: object) -> str:
    """Return a display-friendly value for comparison output."""
    if value is None:
        return ""
    return str(value)


def _yes_no(value: bool) -> str:
    """Return Yes or No for comparison output."""
    return "Yes" if value else "No"


def _diff_cell(value: object, *, is_different: bool) -> str:
    """Return one table cell, highlighting it when the compared values differ."""
    class_attribute = ' class="comparison-diff"' if is_different else ""
    return f"<td{class_attribute}>{escape(str(value))}</td>"
