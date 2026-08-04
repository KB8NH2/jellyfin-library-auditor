"""Generate static comparison reports from completed audit results."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
import re
import shutil

from config import get_config
from media import get_primary_audio_codec
from media import get_video_codec
from media import has_english_subtitles
from media import has_jellyfin_logo
from media import has_jellyfin_primary_image
from media import local_poster_exists
from models import MediaItem
from models import MediaLibrary
from output_layout import audit_results_root
from output_layout import comparison_output_dir
from output_layout import shared_css_path
from output_layout import shared_js_path
from report_theme import render_theme_bootstrap_script
from report_theme import render_theme_toggle
from reports.css import write_css
from reports.javascript import write_javascript
from results import AuditServerResult
from results import LibraryComparisonSettings


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
    left_library_settings = {
        item.library_name: item for item in left_result.library_settings
    }
    right_library_settings = {
        item.library_name: item for item in right_result.library_settings
    }
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
    server_settings: list[tuple[str, str, str]] = _server_settings_rows(
        left_result,
        right_result,
    )
    library_settings: list[tuple[str, str, str, str]] = []

    all_libraries = sorted(set(left_libraries) | set(right_libraries), key=str.casefold)
    for library_name in all_libraries:
        left_library = left_libraries.get(library_name)
        right_library = right_libraries.get(library_name)
        library_settings.extend(
            _library_settings_rows(
                library_name,
                left_library,
                right_library,
                left_library_settings.get(library_name),
                right_library_settings.get(library_name),
            )
        )
        if left_library is None or right_library is None:
            continue

        left_items = _library_items(left_result, library_name)
        right_items = _library_items(right_result, library_name)
        library_pairs, library_only_left, library_only_right = _pair_library_items(
            library_name,
            left_items,
            right_items,
        )
        matched_pairs.extend(library_pairs)
        missing_left_media.extend(library_only_right)
        missing_right_media.extend(library_only_left)

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
        "server_settings": tuple(server_settings),
        "library_settings": tuple(library_settings),
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
    unmatched_left_media: list[MediaItem] = []
    unmatched_right_media: list[MediaItem] = []

    for identity in sorted(set(left_groups) | set(right_groups), key=str):
        left_group = left_groups.get(identity, [])
        right_group = right_groups.get(identity, [])
        matched, missing_left, missing_right = _pair_item_group(left_group, right_group)
        matched_pairs.extend(
            MatchedPair(left=left_item, right=right_item, library=library_name)
            for left_item, right_item in matched
        )
        unmatched_left_media.extend(missing_left)
        unmatched_right_media.extend(missing_right)

    fallback_matches, unmatched_left_media, unmatched_right_media = (
        _pair_by_loose_identity(unmatched_left_media, unmatched_right_media)
    )
    matched_pairs.extend(
        MatchedPair(left=left_item, right=right_item, library=library_name)
        for left_item, right_item in fallback_matches
    )

    return (
        matched_pairs,
        [(library_name, item) for item in unmatched_left_media],
        [(library_name, item) for item in unmatched_right_media],
    )


def _group_items_by_identity(items: tuple) -> dict[tuple, list]:
    """Group items by a comparison identity."""
    grouped: dict[tuple, list] = {}
    for item in items:
        grouped.setdefault(_comparison_identity(item), []).append(item)
    return grouped


def _group_items_by_loose_identity(items: list[MediaItem]) -> dict[tuple, list[MediaItem]]:
    """Group items by a looser user-visible identity for fallback pairing."""
    grouped: dict[tuple, list[MediaItem]] = {}
    for item in items:
        grouped.setdefault(_loose_comparison_identity(item), []).append(item)
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

    signature_matched, remaining_left, remaining_right = _pair_by_version_signature(
        remaining_left,
        remaining_right,
    )
    matched.extend(signature_matched)

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
            _normalized_comparison_text(item.series_name),
            item.season_number,
            item.episode_number,
            _normalized_comparison_text(item.title),
        )
    return (
        "movie",
        _normalized_comparison_text(item.title),
    )


def _loose_comparison_identity(item: MediaItem) -> tuple:
    """Return a user-visible fallback identity for matching near-equal media."""
    if item.is_episode:
        return (
            "episode-fallback",
            _normalized_comparison_text(item.series_name),
            _normalized_comparison_text(item.season_name),
            item.episode_number,
            _normalized_comparison_text(item.title),
        )
    return (
        "movie-fallback",
        _normalized_comparison_text(item.title),
    )


def _pair_by_version_signature(
    left_items: list[MediaItem],
    right_items: list[MediaItem],
) -> tuple[list[tuple[MediaItem, MediaItem]], list[MediaItem], list[MediaItem]]:
    """Pair duplicate variants using resolution and codec metadata before fallback."""
    left_groups = _group_items_by_version_signature(left_items)
    right_groups = _group_items_by_version_signature(right_items)

    matched: list[tuple[MediaItem, MediaItem]] = []
    remaining_left: list[MediaItem] = []
    remaining_right: list[MediaItem] = []

    for signature in sorted(set(left_groups) | set(right_groups), key=str):
        left_group = left_groups.get(signature, [])
        right_group = right_groups.get(signature, [])
        pair_count = min(len(left_group), len(right_group))
        for index in range(pair_count):
            matched.append((left_group[index], right_group[index]))
        remaining_left.extend(left_group[pair_count:])
        remaining_right.extend(right_group[pair_count:])

    return matched, remaining_left, remaining_right


def _pair_by_loose_identity(
    left_items: list[MediaItem],
    right_items: list[MediaItem],
) -> tuple[list[tuple[MediaItem, MediaItem]], list[MediaItem], list[MediaItem]]:
    """Pair remaining items using a looser identity based on visible metadata."""
    left_groups = _group_items_by_loose_identity(left_items)
    right_groups = _group_items_by_loose_identity(right_items)

    matched: list[tuple[MediaItem, MediaItem]] = []
    remaining_left: list[MediaItem] = []
    remaining_right: list[MediaItem] = []

    for identity in sorted(set(left_groups) | set(right_groups), key=str):
        left_group = left_groups.get(identity, [])
        right_group = right_groups.get(identity, [])
        loose_matched, group_left, group_right = _pair_item_group(left_group, right_group)
        matched.extend(loose_matched)
        remaining_left.extend(group_left)
        remaining_right.extend(group_right)

    return matched, remaining_left, remaining_right


def _group_items_by_version_signature(
    items: list[MediaItem],
) -> dict[tuple[str, str, str], list[MediaItem]]:
    """Group same-identity items by a version signature used to align duplicates."""
    grouped: dict[tuple[str, str, str], list[MediaItem]] = defaultdict(list)
    for item in items:
        grouped[_version_signature(item)].append(item)
    return dict(grouped)


def _version_signature(item: MediaItem) -> tuple[str, str, str]:
    """Return the duplicate-version signature used for stable pairing."""
    return (
        item.resolution or "",
        get_video_codec(item) or "",
        get_primary_audio_codec(item) or "",
    )


def _artwork_differs(left_item, right_item) -> bool:
    """Return whether artwork presence differs between two matched items."""
    return any(
        (
            local_poster_exists(left_item) != local_poster_exists(right_item),
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


def _server_settings_rows(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
) -> list[tuple[str, str, str]]:
    """Return all available server-level settings in row form."""
    left_settings = {
        "Configured Server Key": _display_value(left_result.server_key),
        "Reported Server Name": _display_value(left_result.server_name),
        "Server URL": _display_value(left_result.server_url),
        "Libraries Audited": _display_value(left_result.libraries_audited),
        "Media Items Processed": _display_value(left_result.media_items_processed),
        "Findings Count": _display_value(left_result.findings_count),
    }
    right_settings = {
        "Configured Server Key": _display_value(right_result.server_key),
        "Reported Server Name": _display_value(right_result.server_name),
        "Server URL": _display_value(right_result.server_url),
        "Libraries Audited": _display_value(right_result.libraries_audited),
        "Media Items Processed": _display_value(right_result.media_items_processed),
        "Findings Count": _display_value(right_result.findings_count),
    }
    left_settings.update(
        {setting.label: setting.value for setting in left_result.server_settings}
    )
    right_settings.update(
        {setting.label: setting.value for setting in right_result.server_settings}
    )
    labels = sorted(
        set(left_settings) | set(right_settings),
        key=str.casefold,
    )
    return [
        (
            label,
            left_settings.get(label, ""),
            right_settings.get(label, ""),
        )
        for label in labels
    ]


def _library_settings_rows(
    library_name: str,
    left_library: MediaLibrary | None,
    right_library: MediaLibrary | None,
    left_settings: LibraryComparisonSettings | None = None,
    right_settings: LibraryComparisonSettings | None = None,
) -> tuple[tuple[str, str, str, str], ...]:
    """Return all available library-level settings for one library."""
    left_settings_by_label = {
        "Present": _yes_no(left_library is not None),
        "Library ID": _display_value(
            left_library.id if left_library is not None else None
        ),
        "Collection Type": _display_value(
            left_library.collection_type if left_library is not None else None
        ),
        "Locations": _display_locations(left_library),
    }
    right_settings_by_label = {
        "Present": _yes_no(right_library is not None),
        "Library ID": _display_value(
            right_library.id if right_library is not None else None
        ),
        "Collection Type": _display_value(
            right_library.collection_type if right_library is not None else None
        ),
        "Locations": _display_locations(right_library),
    }
    if left_settings is not None:
        left_settings_by_label.update(
            {setting.label: setting.value for setting in left_settings.settings}
        )
    if right_settings is not None:
        right_settings_by_label.update(
            {setting.label: setting.value for setting in right_settings.settings}
        )
    labels = sorted(
        set(left_settings_by_label) | set(right_settings_by_label),
        key=str.casefold,
    )
    return tuple(
        (
            library_name,
            label,
            left_settings_by_label.get(label, ""),
            right_settings_by_label.get(label, ""),
        )
        for label in labels
    )


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
    left_server_name = left_result.server_name or left_result.server_key or "Left"
    right_server_name = right_result.server_name or right_result.server_key or "Right"
    missing_left_rows = tuple(
        _media_missing_row(library_name, item)
        for library_name, item in sorted(
            comparison["missing_left_media"],
            key=lambda entry: _missing_media_sort_key(entry[0], entry[1]),
        )
    )
    missing_right_rows = tuple(
        _media_missing_row(library_name, item)
        for library_name, item in sorted(
            comparison["missing_right_media"],
            key=lambda entry: _missing_media_sort_key(entry[0], entry[1]),
        )
    )
    return _page_shell(
        "Libraries Comparison",
        "Library lists and missing media items between both servers.",
        "\n".join(
            (
                _simple_table_section(
                    "Libraries By Server",
                    (left_server_name, right_server_name),
                    _library_list_rows(left_result, right_result),
                ),
                _simple_table_section(
                    f"Media Missing From {escape(left_server_name)}",
                    ("Library", "Title", "Series", "Season", "Episode"),
                    missing_left_rows,
                    scrollable=True,
                    include_hide_same=False,
                ),
                _simple_table_section(
                    f"Media Missing From {escape(right_server_name)}",
                    ("Library", "Title", "Series", "Season", "Episode"),
                    missing_right_rows,
                    scrollable=True,
                    include_hide_same=False,
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
    left_server_name = left_result.server_name or left_result.server_key or "Left"
    right_server_name = right_result.server_name or right_result.server_key or "Right"
    server_rows = tuple(
        "\n".join(
            (
                f'<tr{" data-diff-row" if left_value != right_value else ""} data-search-row data-search="{escape((setting_name + " " + left_value + " " + right_value).lower())}">',
                f"  <td>{escape(setting_name)}</td>",
                f"  {_diff_cell(left_value, is_different=left_value != right_value)}",
                f"  {_diff_cell(right_value, is_different=left_value != right_value)}",
                "</tr>",
            )
        )
        for setting_name, left_value, right_value in comparison["server_settings"]
    )
    library_rows = tuple(
        "\n".join(
            (
                f'<tr{" data-diff-row" if left_value != right_value else ""} data-search-row data-search="{escape((library_name + " " + setting_name + " " + left_value + " " + right_value).lower())}">',
                f"  <td>{escape(library_name)}</td>",
                f"  <td>{escape(setting_name)}</td>",
                f"  {_diff_cell(left_value, is_different=left_value != right_value)}",
                f"  {_diff_cell(right_value, is_different=left_value != right_value)}",
                "</tr>",
            )
        )
        for library_name, setting_name, left_value, right_value in comparison["library_settings"]
    )
    metadata_rows = tuple(
        "\n".join(
            (
                f'<tr data-diff-row data-search-row data-search="{escape((library + " " + title + " " + field_name + " " + left_value + " " + right_value).lower())}">',
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
        "Side-by-side server and library settings, with differences highlighted.",
        "\n".join(
            (
                _simple_table_section(
                    "Server Settings",
                    (
                        "Setting",
                        left_server_name,
                        right_server_name,
                    ),
                    server_rows,
                    scrollable=True,
                ),
                _simple_table_section(
                    "Library Settings",
                    (
                        "Library",
                        "Setting",
                        left_server_name,
                        right_server_name,
                    ),
                    library_rows,
                    scrollable=True,
                ),
                _simple_table_section(
                    "Metadata Differences",
                    (
                        "Library",
                        "Title",
                        "Field",
                        left_server_name,
                        right_server_name,
                    ),
                    metadata_rows,
                    scrollable=True,
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
    asset_version = datetime.now().strftime("%Y%m%d%H%M%S")
    return "\n".join(
        (
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{escape(title)}</title>",
            f"  {render_theme_bootstrap_script()}",
            f'  <link rel="stylesheet" href="{escape(f"{asset_prefix}css/style.css?v={asset_version}")}">',
            "</head>",
            "<body>",
            body,
            f'  <script src="{escape(f"{asset_prefix}js/report.js?v={asset_version}")}"></script>',
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
            f"    {render_theme_toggle()}",
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


def _simple_table_section(
    title: str,
    headers: tuple[str, ...],
    rows: tuple[str, ...],
    *,
    scrollable: bool = False,
    include_hide_same: bool = True,
) -> str:
    """Return one simple table section."""
    header_html = "".join(
        f'<th><button type="button" class="sort-button" data-column="{index}" onclick="sortReportTable(this)">{escape(label)}</button></th>'
        for index, label in enumerate(headers)
    )
    body_rows = rows or (
        '<tr class="empty-row" data-static-row><td colspan="99">No differences found.</td></tr>',
    )
    table_shell_class = "table-shell comparison-scroll-shell" if scrollable else "table-shell"
    hide_same_button = (
        '      <button type="button" class="toolbar-button table-filter-button" onclick="toggleSameRows(this)" aria-pressed="false">Hide same</button>'
        if include_hide_same
        else ""
    )
    table_attributes = ' class="data-table comparison-table"'
    if include_hide_same:
        table_attributes += ' data-hide-same="false"'
    return "\n".join(
        (
            '  <section class="section-card">',
            '    <div class="table-section-header">',
            f"      <h2>{escape(title)}</h2>",
            hide_same_button,
            "    </div>",
            f'    <div class="{table_shell_class}">',
            f"      <table{table_attributes}>",
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
        f'<tr data-diff-row data-search-row data-search="{escape(search_text)}"><td>{escape(library_name)}</td>'
        f'<td>{escape(item.title)}</td><td>{escape(item.series_name or "")}</td>'
        f'{_table_cell(_display_season(item), sort_value=_season_sort_value(item))}'
        f'{_table_cell("" if item.episode_number is None else item.episode_number, sort_value=_episode_sort_value(item))}</tr>'
    )


def _missing_media_sort_key(library_name: str, item: MediaItem) -> tuple:
    """Return a stable sort key for missing-media rows."""
    return (
        library_name.casefold(),
        (item.series_name or "").casefold(),
        (item.season_name or "").casefold(),
        item.episode_number if item.episode_number is not None else -1,
        item.title.casefold(),
    )


def _library_list_rows(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
) -> tuple[str, ...]:
    """Return rows listing libraries side-by-side for both servers."""
    left_libraries = {
        library_result.library.name
        for library_result in left_result.library_results
    }
    right_libraries = {
        library_result.library.name
        for library_result in right_result.library_results
    }
    all_libraries = sorted(left_libraries | right_libraries, key=str.casefold)
    rows = tuple(
        (
            f'<tr{" data-diff-row" if library_name not in left_libraries or library_name not in right_libraries else ""}>'
            f'<td>{escape(library_name if library_name in left_libraries else "")}</td>'
            f'<td>{escape(library_name if library_name in right_libraries else "")}</td></tr>'
        )
        for library_name in all_libraries
    )
    return rows or ('<tr class="empty-row" data-static-row><td colspan="99">No libraries found.</td></tr>',)


def _artwork_row(left_result: AuditServerResult, right_result: AuditServerResult, pair: MatchedPair) -> str:
    """Return one artwork difference row."""
    search_text = f"{pair.library} {pair.left.display_name}".lower()
    left_poster = _yes_no(local_poster_exists(pair.left))
    right_poster = _yes_no(local_poster_exists(pair.right))
    left_primary = _yes_no(has_jellyfin_primary_image(pair.left))
    right_primary = _yes_no(has_jellyfin_primary_image(pair.right))
    left_logo = _yes_no(has_jellyfin_logo(pair.left))
    right_logo = _yes_no(has_jellyfin_logo(pair.right))
    return (
        f'<tr class="comparison-diff-row" data-diff-row data-search-row data-search="{escape(search_text)}"><td>{escape(pair.library)}</td>'
        f'<td>{escape(pair.left.display_name)}</td>'
        f'{_diff_cell(left_poster, is_different=left_poster != right_poster)}{_diff_cell(right_poster, is_different=left_poster != right_poster)}'
        f'{_diff_cell(left_primary, is_different=left_primary != right_primary)}{_diff_cell(right_primary, is_different=left_primary != right_primary)}'
        f'{_diff_cell(left_logo, is_different=left_logo != right_logo)}{_diff_cell(right_logo, is_different=left_logo != right_logo)}</tr>'
    )


def _subtitle_row(left_result: AuditServerResult, right_result: AuditServerResult, pair: MatchedPair) -> str:
    """Return one subtitle difference row."""
    search_text = f"{pair.library} {pair.left.display_name} subtitles".lower()
    left_subtitles = _yes_no(has_english_subtitles(pair.left))
    right_subtitles = _yes_no(has_english_subtitles(pair.right))
    return (
        f'<tr data-diff-row data-search-row data-search="{escape(search_text)}"><td>{escape(pair.library)}</td>'
        f'<td>{escape(pair.left.title)}</td><td>{escape(pair.left.series_name or "")}</td>'
        f'{_table_cell(_display_season(pair.left), sort_value=_season_sort_value(pair.left))}'
        f'{_table_cell("" if pair.left.episode_number is None else pair.left.episode_number, sort_value=_episode_sort_value(pair.left))}'
        f'{_diff_cell(left_subtitles, is_different=left_subtitles != right_subtitles)}{_diff_cell(right_subtitles, is_different=left_subtitles != right_subtitles)}</tr>'
    )


def _display_value(value: object) -> str:
    """Return a display-friendly value for comparison output."""
    if value is None:
        return ""
    return str(value)


def _display_locations(library: MediaLibrary | None) -> str:
    """Return a compact display value for library locations."""
    if library is None:
        return ""
    return ", ".join(str(location) for location in library.locations)


def _display_season(item: MediaItem) -> str:
    """Return a compact season label without the word 'Season'."""
    if item.season_number is not None:
        return str(item.season_number)
    if item.season_name is None:
        return ""
    normalized = re.sub(r"^\s*season\s+", "", item.season_name, flags=re.IGNORECASE)
    return normalized.strip()


def _season_sort_value(item: MediaItem) -> str:
    """Return a numeric-first season sort value."""
    if item.season_number is not None:
        return str(item.season_number)
    season_text = _display_season(item)
    if season_text.isdigit():
        return season_text
    return season_text.casefold()


def _episode_sort_value(item: MediaItem) -> str:
    """Return the episode number as a sortable value."""
    if item.episode_number is None:
        return ""
    return str(item.episode_number)


def _table_cell(value: object, *, sort_value: str | None = None) -> str:
    """Return one table cell with an optional explicit sort value."""
    sort_attribute = (
        f' data-sort-value="{escape(sort_value)}"' if sort_value is not None else ""
    )
    return f"<td{sort_attribute}>{escape(str(value))}</td>"


def _yes_no(value: bool) -> str:
    """Return Yes or No for comparison output."""
    return "Yes" if value else "No"


def _diff_cell(value: object, *, is_different: bool) -> str:
    """Return one table cell, highlighting it when the compared values differ."""
    class_attribute = ' class="comparison-diff"' if is_different else ""
    return f"<td{class_attribute}>{escape(str(value))}</td>"


def _normalized_comparison_text(value: str | None) -> str:
    """Return a normalized comparison key for media titles and series names."""
    normalized_value = (value or "").strip().casefold()
    normalized_value = re.sub(r"\s*\(\d{4}\)\s*$", "", normalized_value)
    normalized_value = re.sub(r"""[\.,:;!\?'\"·-]""", "", normalized_value)
    normalized_value = re.sub(r"\s+", " ", normalized_value)
    return normalized_value.strip()
