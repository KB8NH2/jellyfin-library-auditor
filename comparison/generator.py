"""Generate static comparison reports from completed audit results."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
import re
import shutil

from audit_types import AuditFinding
from config import get_config
from media import get_primary_audio_codec
from media import get_video_codec
from media import has_english_subtitles
from media import has_jellyfin_primary_image
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
MISSING_SEASONS_CHECK_NAME = "missing_seasons"
MISSING_EPISODES_CHECK_NAME = "missing_episodes"


@dataclass(frozen=True, slots=True)
class MatchedPair:
    """Represents one matched media item pair across two servers."""

    left: MediaItem
    right: MediaItem
    library: str
    matched_by_filename: bool = False


@dataclass(frozen=True, slots=True)
class MetadataTransferTarget:
    """One item pair whose metadata can be transferred from left to right."""

    library: str
    display_name: str
    left_server_key: str
    left_item_id: str
    right_server_key: str
    right_item_id: str


@dataclass(frozen=True, slots=True)
class ImageTransferTarget:
    """One item pair whose images can be transferred from left to right."""

    library: str
    display_name: str
    left_title: str
    left_server_key: str
    left_item_id: str
    right_server_key: str
    right_item_id: str


@dataclass(frozen=True, slots=True)
class SubtitleTransferTarget:
    """One item pair whose English subtitle track can be transferred from left to right."""

    library: str
    display_name: str
    left_server_key: str
    left_item_id: str
    right_server_key: str
    right_item_id: str


@dataclass(frozen=True, slots=True)
class SubtitleTransferResult:
    """Outcome of one item's subtitle transfer attempt in a --transfer-subtitles run.

    Attributes:
        library: Library the item belongs to.
        display_name: Filename or title shown for the item.
        status: One of ``"transferred"``, ``"would_transfer"`` (--dry-run),
            ``"no_source_subtitle"`` (the source has no matching English
            text subtitle track), ``"already_present"`` (the destination
            already has an English subtitle track, so it was left alone), or
            ``"failed"``.
        detail: Human-readable reason for a failure, empty otherwise.
    """

    library: str
    display_name: str
    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ImageTransferResult:
    """Outcome of one (item, image type) transfer attempt in a --transfer-images run.

    Attributes:
        library: Library the item belongs to.
        display_name: Filename or title shown for the item.
        image_type: Jellyfin image type this result covers (e.g. ``"Primary"``).
        status: One of ``"transferred"``, ``"would_transfer"`` (--dry-run),
            ``"unavailable"`` (the source has no image of this type),
            ``"already_present"`` (the destination already has one, so it
            was left alone), or ``"failed"``.
        detail: Human-readable reason for a failure, empty otherwise.
    """

    library: str
    display_name: str
    image_type: str
    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class MetadataTransferResult:
    """Outcome of one item's metadata transfer attempt in a --transfer-metadata run.

    Attributes:
        library: Library the item belongs to.
        display_name: Filename or title shown for the item.
        status: One of ``"transferred"``, ``"would_transfer"`` (--dry-run),
            ``"unchanged"``, ``"rejected"``, or ``"failed"``.
        changed_fields: Field names that changed or would change.
        detail: Human-readable reason for a rejection or failure, empty otherwise.
    """

    library: str
    display_name: str
    status: str
    changed_fields: tuple[str, ...] = ()
    detail: str = ""


def mismatched_metadata_transfer_targets(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
) -> tuple[MetadataTransferTarget, ...]:
    """Return one transfer target for every item with mismatched metadata.

    Reuses the same comparison used to build the "Mismatched Metadata" report
    table, so this always matches what that report shows. Pairs missing a
    configured server key (only possible when a caller builds
    ``AuditServerResult`` without one, e.g. in ad hoc scripting) are skipped
    since a transfer command can't be built without one.

    Args:
        left_result: Completed audit results for the source server.
        right_result: Completed audit results for the destination server.

    Returns:
        One target per mismatched-metadata item pair, in the same order the
        report displays them.
    """
    comparison = _build_comparison(left_result, right_result)
    targets: list[MetadataTransferTarget] = []
    for entry in comparison["mismatched_metadata"]:
        library_name, filename, left_item_id, right_item_id, left_server_key, right_server_key = entry[:6]
        if not left_server_key or not right_server_key:
            continue
        targets.append(
            MetadataTransferTarget(
                library=library_name,
                display_name=filename,
                left_server_key=left_server_key,
                left_item_id=left_item_id,
                right_server_key=right_server_key,
                right_item_id=right_item_id,
            )
        )
    return tuple(targets)


def missing_image_transfer_targets(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
) -> tuple[ImageTransferTarget, ...]:
    """Return one transfer target for every item pair with an artwork difference.

    Reuses the same comparison used to build the "Artwork Differences" report
    table, so this always matches what that report shows. Returns nothing
    when either server is missing a configured server key, since a transfer
    can't be built without one.

    Args:
        left_result: Completed audit results for the source server.
        right_result: Completed audit results for the destination server.

    Returns:
        One target per artwork-differing item pair, in the same order the
        report displays them.
    """
    left_server_key = left_result.server_key
    right_server_key = right_result.server_key
    if not left_server_key or not right_server_key:
        return ()

    comparison = _build_comparison(left_result, right_result)
    return tuple(
        ImageTransferTarget(
            library=pair.library,
            display_name=pair.left.display_name,
            left_title=pair.left.title,
            left_server_key=left_server_key,
            left_item_id=pair.left.id,
            right_server_key=right_server_key,
            right_item_id=pair.right.id,
        )
        for pair in comparison["artwork_differences"]
    )


def missing_subtitle_transfer_targets(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
) -> tuple[SubtitleTransferTarget, ...]:
    """Return one transfer target for every item pair with a subtitle difference.

    Reuses the same comparison used to build the "Subtitle Differences"
    report table, so this always matches what that report shows. A pair
    lands here regardless of which side actually has the English subtitle -
    the bulk transfer loop itself resolves direction by checking the
    destination before attempting anything, the same way
    ``missing_image_transfer_targets`` does for artwork. Returns nothing
    when either server is missing a configured server key, since a transfer
    can't be built without one.

    Args:
        left_result: Completed audit results for the source server.
        right_result: Completed audit results for the destination server.

    Returns:
        One target per subtitle-differing item pair, in the same order the
        report displays them.
    """
    left_server_key = left_result.server_key
    right_server_key = right_result.server_key
    if not left_server_key or not right_server_key:
        return ()

    comparison = _build_comparison(left_result, right_result)
    return tuple(
        SubtitleTransferTarget(
            library=pair.library,
            display_name=pair.left.display_name,
            left_server_key=left_server_key,
            left_item_id=pair.left.id,
            right_server_key=right_server_key,
            right_item_id=pair.right.id,
        )
        for pair in comparison["subtitle_differences"]
    )


def write_comparison_reports(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
    output_dir: Path | None = None,
    *,
    transfer_results: tuple[MetadataTransferResult, ...] | None = None,
    image_transfer_results: tuple[ImageTransferResult, ...] | None = None,
    subtitle_transfer_results: tuple[SubtitleTransferResult, ...] | None = None,
) -> Path:
    """Write a static comparison site for two completed audit results.

    Args:
        left_result: Completed audit results for the source server.
        right_result: Completed audit results for the destination server.
        output_dir: Optional output directory override.
        transfer_results: Per-item outcomes from a --transfer-metadata run to
            include as a "Transfer Results" table on the libraries page.
            ``None`` omits the table entirely (the flag wasn't used).
        image_transfer_results: Per-(item, image type) outcomes from a
            --transfer-images run to include as an "Image Transfer Results"
            table on the artwork page. ``None`` omits the table entirely.
        subtitle_transfer_results: Per-item outcomes from a
            --transfer-subtitles run to include as a "Subtitle Transfer
            Results" table on the subtitles page. ``None`` omits the table
            entirely.
    """
    root_dir = _default_output_dir() if output_dir is None else output_dir
    output_root = root_dir.parent
    if root_dir.exists():
        shutil.rmtree(root_dir)
    root_dir.mkdir(parents=True, exist_ok=True)

    write_css(shared_css_path(output_root))
    write_javascript(shared_js_path(output_root))

    comparison = _build_comparison(left_result, right_result)
    generated_at_text = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    (root_dir / "index.html").write_text(
        _page_document(
            title="Server Comparison",
            body=_index_page(left_result, right_result, comparison, generated_at_text=generated_at_text),
            asset_prefix="../",
        ),
        encoding="utf-8",
    )
    (root_dir / "libraries.html").write_text(
        _page_document(
            title="Library Comparison",
            body=_libraries_page(
                left_result,
                right_result,
                comparison,
                transfer_results=transfer_results,
                generated_at_text=generated_at_text,
            ),
            asset_prefix="../",
        ),
        encoding="utf-8",
    )
    (root_dir / "artwork.html").write_text(
        _page_document(
            title="Artwork Comparison",
            body=_artwork_page(
                left_result,
                right_result,
                comparison,
                image_transfer_results=image_transfer_results,
                generated_at_text=generated_at_text,
            ),
            asset_prefix="../",
        ),
        encoding="utf-8",
    )
    (root_dir / "subtitles.html").write_text(
        _page_document(
            title="Subtitle Comparison",
            body=_subtitles_page(
                left_result,
                right_result,
                comparison,
                subtitle_transfer_results=subtitle_transfer_results,
                generated_at_text=generated_at_text,
            ),
            asset_prefix="../",
        ),
        encoding="utf-8",
    )
    (root_dir / "configuration.html").write_text(
        _page_document(
            title="Configuration Comparison",
            body=_configuration_page(left_result, right_result, comparison, generated_at_text=generated_at_text),
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
    mismatched_metadata: list[tuple[str, ...]] = []
    left_missing_seasons = _sequence_gap_findings(
        left_result,
        check_name=MISSING_SEASONS_CHECK_NAME,
    )
    right_missing_seasons = _sequence_gap_findings(
        right_result,
        check_name=MISSING_SEASONS_CHECK_NAME,
    )
    left_missing_episodes = _sequence_gap_findings(
        left_result,
        check_name=MISSING_EPISODES_CHECK_NAME,
    )
    right_missing_episodes = _sequence_gap_findings(
        right_result,
        check_name=MISSING_EPISODES_CHECK_NAME,
    )
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
        mismatch_row = _mismatched_metadata_row(
            pair,
            left_result.server_key,
            right_result.server_key,
        )
        if mismatch_row is not None:
            mismatched_metadata.append(mismatch_row)

    return {
        "missing_left_libraries": missing_left_libraries,
        "missing_right_libraries": missing_right_libraries,
        "missing_left_media": tuple(missing_left_media),
        "missing_right_media": tuple(missing_right_media),
        "artwork_differences": tuple(artwork_differences),
        "subtitle_differences": tuple(subtitle_differences),
        "mismatched_metadata": tuple(mismatched_metadata),
        "left_missing_seasons": left_missing_seasons,
        "right_missing_seasons": right_missing_seasons,
        "left_missing_episodes": left_missing_episodes,
        "right_missing_episodes": right_missing_episodes,
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
    """Pair media items across two libraries.

    Items are matched primarily by their media file's base filename, since the
    underlying file is shared or mirrored between servers even when each
    server's metadata agent produces different titles, series names, or
    episode numbers. Items whose filenames don't match (for example, files
    renamed independently on each server) fall back to metadata-based
    matching.
    """
    filename_matches, remaining_left, remaining_right = _pair_by_filename_identity(
        list(left_items),
        list(right_items),
    )
    matched_pairs: list[MatchedPair] = [
        MatchedPair(
            left=left_item,
            right=right_item,
            library=library_name,
            matched_by_filename=True,
        )
        for left_item, right_item in filename_matches
    ]
    unmatched_left_media: list[MediaItem] = []
    unmatched_right_media: list[MediaItem] = []

    left_groups = _group_items_by_identity(remaining_left)
    right_groups = _group_items_by_identity(remaining_right)

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


def _pair_by_filename_identity(
    left_items: list[MediaItem],
    right_items: list[MediaItem],
) -> tuple[list[tuple[MediaItem, MediaItem]], list[MediaItem], list[MediaItem]]:
    """Pair items whose media files share the same base filename."""
    left_groups = _group_items_by_filename_identity(left_items)
    right_groups = _group_items_by_filename_identity(right_items)

    matched: list[tuple[MediaItem, MediaItem]] = []
    remaining_left: list[MediaItem] = []
    remaining_right: list[MediaItem] = []

    for identity in sorted(set(left_groups) | set(right_groups)):
        left_group = left_groups.get(identity, [])
        right_group = right_groups.get(identity, [])
        group_matched, group_left, group_right = _pair_item_group(left_group, right_group)
        matched.extend(group_matched)
        remaining_left.extend(group_left)
        remaining_right.extend(group_right)

    return matched, remaining_left, remaining_right


def _group_items_by_filename_identity(items: list[MediaItem]) -> dict[str, list[MediaItem]]:
    """Group items by their normalized media file base filename."""
    grouped: dict[str, list[MediaItem]] = {}
    for item in items:
        identity = _filename_identity(item)
        if not identity:
            continue
        grouped.setdefault(identity, []).append(item)
    return grouped


def _filename_identity(item: MediaItem) -> str:
    """Return a normalized base filename used to match items across servers."""
    return item.path.stem.strip().casefold()


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
    return has_jellyfin_primary_image(left_item) != has_jellyfin_primary_image(right_item)


def _mismatched_metadata_row(
    pair: MatchedPair,
    left_server_key: str | None,
    right_server_key: str | None,
) -> tuple[str, ...] | None:
    """Return one side-by-side metadata comparison row for a matched pair, if any field differs.

    Covers TV season/episode identification (only meaningful when the pair was
    matched by shared filename rather than normalized identity, since
    identity-matched episodes already share the same season/episode numbers by
    construction) as well as year, resolution, and codec fields that can
    genuinely differ regardless of how the pair was matched.
    """
    left_title = _metadata_title(pair.left)
    right_title = _metadata_title(pair.right)
    left_season = _display_value(pair.left.season_number)
    right_season = _display_value(pair.right.season_number)
    left_episode_number = _display_value(pair.left.episode_number)
    right_episode_number = _display_value(pair.right.episode_number)
    left_episode_name = _metadata_episode_name(pair.left)
    right_episode_name = _metadata_episode_name(pair.right)
    left_year = _display_value(pair.left.year)
    right_year = _display_value(pair.right.year)
    left_resolution = _display_value(pair.left.resolution)
    right_resolution = _display_value(pair.right.resolution)
    left_video_codec = _display_value(get_video_codec(pair.left))
    right_video_codec = _display_value(get_video_codec(pair.right))
    left_audio_codec = _display_value(get_primary_audio_codec(pair.left))
    right_audio_codec = _display_value(get_primary_audio_codec(pair.right))

    if (
        left_title.casefold() == right_title.casefold()
        and left_season == right_season
        and left_episode_number == right_episode_number
        and left_episode_name.casefold() == right_episode_name.casefold()
        and left_year == right_year
        and left_resolution == right_resolution
        and left_video_codec == right_video_codec
        and left_audio_codec == right_audio_codec
    ):
        return None

    return (
        pair.library,
        pair.left.path.stem,
        pair.left.id,
        pair.right.id,
        left_server_key or "",
        right_server_key or "",
        left_title,
        right_title,
        left_season,
        right_season,
        left_episode_number,
        right_episode_number,
        left_episode_name,
        right_episode_name,
        left_year,
        right_year,
        left_resolution,
        right_resolution,
        left_video_codec,
        right_video_codec,
        left_audio_codec,
        right_audio_codec,
        _media_relative_path_sort_key(pair.left.path),
    )


def _metadata_title(item: MediaItem) -> str:
    """Return the display title used for the Mismatched Metadata report."""
    if item.is_episode:
        return item.series_name or ""
    return item.title


def _metadata_episode_name(item: MediaItem) -> str:
    """Return the episode-specific title used for the Mismatched Metadata report."""
    return item.title if item.is_episode else ""


def _media_relative_path_sort_key(path: Path) -> str:
    """Return a sortable path value using only the segment after the last "media" directory."""
    segments = str(path).replace("\\", "/").split("/")
    media_indexes = [
        index for index, segment in enumerate(segments) if segment.casefold() == "media"
    ]
    if media_indexes:
        segments = segments[media_indexes[-1] + 1 :]
    return "/".join(segments).casefold()


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


def _sequence_gap_findings(
    result: AuditServerResult,
    *,
    check_name: str,
) -> tuple[tuple[str, AuditFinding], ...]:
    """Return sorted sequence-gap findings for one server result."""
    rows: list[tuple[str, AuditFinding]] = []
    for library_result in result.library_results:
        for finding in library_result.findings:
            if finding.check_name != check_name:
                continue
            rows.append((library_result.library.name, finding))

    return tuple(
        sorted(
            rows,
            key=lambda entry: _sequence_gap_sort_key(entry[0], entry[1]),
        )
    )


def _index_page(left_result: AuditServerResult, right_result: AuditServerResult, comparison: dict[str, object], *, generated_at_text: str = "") -> str:
    """Return comparison overview page body."""
    cards = "\n".join(
        (
            _summary_card("Left Server", left_result.server_name or left_result.server_key or "Left"),
            _summary_card("Right Server", right_result.server_name or right_result.server_key or "Right"),
            _summary_card("Missing Libraries", str(len(comparison["missing_left_libraries"]) + len(comparison["missing_right_libraries"]))),
            _summary_card("Missing Media", str(len(comparison["missing_left_media"]) + len(comparison["missing_right_media"]))),
            _summary_card("Missing Seasons", str(len(comparison["left_missing_seasons"]) + len(comparison["right_missing_seasons"]))),
            _summary_card("Missing Episodes", str(len(comparison["left_missing_episodes"]) + len(comparison["right_missing_episodes"]))),
            _summary_card("Mismatched Metadata", str(len(comparison["mismatched_metadata"]))),
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
        generated_at_text=generated_at_text,
    )


def _libraries_page(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
    comparison: dict[str, object],
    *,
    transfer_results: tuple[MetadataTransferResult, ...] | None = None,
    generated_at_text: str = "",
) -> str:
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
    missing_seasons_rows = _paired_missing_seasons_rows(
        comparison["left_missing_seasons"],
        comparison["right_missing_seasons"],
    )
    missing_episodes_rows = _paired_missing_episodes_rows(
        comparison["left_missing_episodes"],
        comparison["right_missing_episodes"],
    )
    mismatched_metadata_rows = tuple(
        _mismatched_metadata_row_html(entry)
        for entry in sorted(
            comparison["mismatched_metadata"],
            key=lambda entry: (entry[0].casefold(), entry[-1]),
        )
    )
    sections = [
        _simple_table_section(
            "Libraries By Server",
            (left_server_name, right_server_name),
            _library_list_rows(left_result, right_result),
        ),
        _simple_table_section(
            f"Media Missing From {escape(left_server_name)}",
            ("Library", "Title", "Series", "Season", "Episode"),
            missing_left_rows,
            include_hide_same=False,
        ),
        _simple_table_section(
            f"Media Missing From {escape(right_server_name)}",
            ("Library", "Title", "Series", "Season", "Episode"),
            missing_right_rows,
            include_hide_same=False,
        ),
        _simple_table_section(
            "Missing Seasons",
            ("Library", "Series", left_server_name, right_server_name),
            missing_seasons_rows,
            include_hide_same=True,
        ),
        _simple_table_section(
            "Missing Episodes",
            ("Library", "Series", "Season", left_server_name, right_server_name),
            missing_episodes_rows,
            include_hide_same=True,
        ),
        _grouped_table_section(
            "Mismatched Metadata",
            ("Library", "Base Filename"),
            (
                "Title",
                "Season Number",
                "Episode Number",
                "Episode Name",
                "Year",
                "Resolution",
                "Video Codec",
                "Audio Codec",
            ),
            left_server_name,
            right_server_name,
            mismatched_metadata_rows,
            include_hide_same=False,
            split_field="Episode Name",
        ),
    ]
    if transfer_results is not None:
        sections.append(_transfer_results_section(transfer_results))

    return _page_shell(
        "Libraries Comparison",
        "Library lists, missing media items, and missing TV seasons or episodes between both servers.",
        "\n".join(sections),
        current_nav="Libraries",
        include_search=True,
        generated_at_text=generated_at_text,
    )


def _artwork_page(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
    comparison: dict[str, object],
    *,
    image_transfer_results: tuple[ImageTransferResult, ...] | None = None,
    generated_at_text: str = "",
) -> str:
    """Return artwork comparison page body."""
    left_server_name = left_result.server_name or left_result.server_key or "Left"
    right_server_name = right_result.server_name or right_result.server_key or "Right"
    rows = tuple(
        _artwork_row(left_result, right_result, pair)
        for pair in comparison["artwork_differences"]
    )
    sections = [
        _grouped_table_section(
            "Artwork Differences",
            ("Library", "Title"),
            ("Primary",),
            left_server_name,
            right_server_name,
            rows,
            include_hide_same=False,
        ),
    ]
    if image_transfer_results is not None:
        sections.append(_image_transfer_results_section(image_transfer_results))
    return _page_shell(
        "Artwork Comparison",
        "Differences in Jellyfin primary image presence.",
        "\n".join(sections),
        current_nav="Artwork",
        include_search=True,
        generated_at_text=generated_at_text,
    )


def _subtitles_page(
    left_result: AuditServerResult,
    right_result: AuditServerResult,
    comparison: dict[str, object],
    *,
    subtitle_transfer_results: tuple[SubtitleTransferResult, ...] | None = None,
    generated_at_text: str = "",
) -> str:
    """Return subtitles comparison page body."""
    left_server_name = left_result.server_name or left_result.server_key or "Left"
    right_server_name = right_result.server_name or right_result.server_key or "Right"
    rows = tuple(
        _subtitle_row(left_result, right_result, pair)
        for pair in comparison["subtitle_differences"]
    )
    sections = [
        _grouped_table_section(
            "Subtitle Differences",
            ("Library", "Title", "Series", "Season", "Episode"),
            ("English Subtitles",),
            left_server_name,
            right_server_name,
            rows,
            include_hide_same=False,
        ),
    ]
    if subtitle_transfer_results is not None:
        sections.append(_subtitle_transfer_results_section(subtitle_transfer_results))
    return _page_shell(
        "Subtitle Comparison",
        "Differences in English subtitle availability.",
        "\n".join(sections),
        current_nav="Subtitles",
        include_search=True,
        generated_at_text=generated_at_text,
    )


def _configuration_page(left_result: AuditServerResult, right_result: AuditServerResult, comparison: dict[str, object], *, generated_at_text: str = "") -> str:
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
                ),
            )
        ),
        current_nav="Configuration",
        include_search=True,
        generated_at_text=generated_at_text,
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
    generated_at_text: str = "",
) -> str:
    """Return one comparison page body."""
    generated_at_html = (
        f'    <p class="page-generated-at">Generated {escape(generated_at_text)}</p>'
        if generated_at_text
        else ""
    )
    return "\n".join(
        (
            f'<main class="page-shell" data-nav-current="{escape(current_nav)}">',
            _comparison_nav(current_nav),
            '  <section class="page-header-card">',
            f"    <h1>{escape(heading)}</h1>",
            f'    <p class="page-intro">{escape(intro)}</p>',
            generated_at_html,
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
    table_shell_class = "table-shell table-scroll-shell"
    hide_same_button = (
        '      <button type="button" class="toolbar-button table-filter-button" onclick="toggleSameRows(this)" aria-pressed="false">Hide same</button>'
        if include_hide_same
        else ""
    )
    row_count_html = f' <span class="table-row-count" data-row-count>({len(rows)})</span>'
    table_attributes = ' class="data-table comparison-table"'
    if include_hide_same:
        table_attributes += ' data-hide-same="false"'
    return "\n".join(
        (
            '  <section class="section-card">',
            '    <div class="table-section-header">',
            f"      <h2>{escape(title)}{row_count_html}</h2>",
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


_TRANSFER_STATUS_LABELS = {
    "transferred": '<span class="status-label status-present">&#10003; transferred</span>',
    "would_transfer": '<span class="status-label status-planned">&#8594; would transfer</span>',
    "unchanged": '<span class="status-label muted-text">unchanged</span>',
    "unavailable": '<span class="status-label muted-text">no source image</span>',
    "no_source_subtitle": '<span class="status-label muted-text">no source subtitle</span>',
    "already_present": '<span class="status-label muted-text">already present</span>',
    "rejected": '<span class="status-label status-missing">&#10007; rejected</span>',
    "failed": '<span class="status-label status-missing">&#10007; failed</span>',
}


def _transfer_result_row(result: MetadataTransferResult) -> str:
    """Return one row for the Transfer Results table."""
    status_html = _TRANSFER_STATUS_LABELS.get(result.status, escape(result.status))
    changed_fields_text = ", ".join(result.changed_fields)
    search_text = " ".join(
        part
        for part in (result.library, result.display_name, result.status, result.detail)
        if part
    ).lower()
    return (
        f'<tr data-search-row data-search="{escape(search_text)}">'
        f"<td>{escape(result.library)}</td>"
        f"<td>{escape(result.display_name)}</td>"
        f"<td>{status_html}</td>"
        f"<td>{escape(changed_fields_text)}</td>"
        f"<td>{escape(result.detail)}</td>"
        "</tr>"
    )


def _transfer_results_section(results: tuple[MetadataTransferResult, ...]) -> str:
    """Return the Transfer Results table section for a --transfer-metadata run."""
    rows = tuple(_transfer_result_row(result) for result in results)
    return _simple_table_section(
        "Transfer Results",
        ("Library", "Item", "Status", "Changed Fields", "Detail"),
        rows,
        include_hide_same=False,
    )


def _image_transfer_result_row(result: ImageTransferResult) -> str:
    """Return one row for the Image Transfer Results table."""
    status_html = _TRANSFER_STATUS_LABELS.get(result.status, escape(result.status))
    search_text = " ".join(
        part
        for part in (result.library, result.display_name, result.image_type, result.status, result.detail)
        if part
    ).lower()
    return (
        f'<tr data-search-row data-search="{escape(search_text)}">'
        f"<td>{escape(result.library)}</td>"
        f"<td>{escape(result.display_name)}</td>"
        f"<td>{escape(result.image_type)}</td>"
        f"<td>{status_html}</td>"
        f"<td>{escape(result.detail)}</td>"
        "</tr>"
    )


def _image_transfer_results_section(results: tuple[ImageTransferResult, ...]) -> str:
    """Return the Image Transfer Results table section for a --transfer-images run."""
    rows = tuple(_image_transfer_result_row(result) for result in results)
    return _simple_table_section(
        "Image Transfer Results",
        ("Library", "Item", "Image Type", "Status", "Detail"),
        rows,
        include_hide_same=False,
    )


def _subtitle_transfer_result_row(result: SubtitleTransferResult) -> str:
    """Return one row for the Subtitle Transfer Results table."""
    status_html = _TRANSFER_STATUS_LABELS.get(result.status, escape(result.status))
    search_text = " ".join(
        part
        for part in (result.library, result.display_name, result.status, result.detail)
        if part
    ).lower()
    return (
        f'<tr data-search-row data-search="{escape(search_text)}">'
        f"<td>{escape(result.library)}</td>"
        f"<td>{escape(result.display_name)}</td>"
        f"<td>{status_html}</td>"
        f"<td>{escape(result.detail)}</td>"
        "</tr>"
    )


def _subtitle_transfer_results_section(results: tuple[SubtitleTransferResult, ...]) -> str:
    """Return the Subtitle Transfer Results table section for a --transfer-subtitles run."""
    rows = tuple(_subtitle_transfer_result_row(result) for result in results)
    return _simple_table_section(
        "Subtitle Transfer Results",
        ("Library", "Item", "Status", "Detail"),
        rows,
        include_hide_same=False,
    )


def _grouped_table_section(
    title: str,
    solo_columns: tuple[str, ...],
    paired_fields: tuple[str, ...],
    left_server_name: str,
    right_server_name: str,
    rows: tuple[str, ...],
    *,
    include_hide_same: bool = True,
    split_field: str | None = None,
    split_column_header: str = "",
) -> str:
    """Return one table section with a two-row grouped header.

    The header's first row shows each paired field name (e.g. "Title") once,
    spanning both of its left/right server sub-columns; the second row shows
    the server names themselves. This keeps narrow per-server columns (e.g. a
    season number) from being stretched wide by a repeated "<field> on
    <server>" label.

    When ``split_field`` matches one of ``paired_fields``, that field's header
    spans a third column inserted between its left and right sub-columns,
    labeled with ``split_column_header``. This is meant for a single action
    column (e.g. a per-row transfer button) that belongs visually between two
    compared values rather than being its own left/right pair.
    """
    column_index = 0
    solo_header_cells: list[str] = []
    for label in solo_columns:
        solo_header_cells.append(
            f'<th rowspan="2"><button type="button" class="sort-button" data-column="{column_index}" onclick="sortReportTable(this)">{escape(label)}</button></th>'
        )
        column_index += 1

    field_header_cells: list[str] = []
    server_header_cells: list[str] = []
    for field_label in paired_fields:
        is_split = field_label == split_field
        colspan = 3 if is_split else 2
        field_header_cells.append(f'<th colspan="{colspan}">{escape(field_label)}</th>')
        server_header_cells.append(
            f'<th><button type="button" class="sort-button" data-column="{column_index}" onclick="sortReportTable(this)">{escape(left_server_name)}</button></th>'
        )
        column_index += 1
        if is_split:
            server_header_cells.append(
                f'<th class="transfer-column-header">{escape(split_column_header)}</th>'
            )
            column_index += 1
        server_header_cells.append(
            f'<th><button type="button" class="sort-button" data-column="{column_index}" onclick="sortReportTable(this)">{escape(right_server_name)}</button></th>'
        )
        column_index += 1

    body_rows = rows or (
        '<tr class="empty-row" data-static-row><td colspan="99">No differences found.</td></tr>',
    )
    table_shell_class = "table-shell table-scroll-shell"
    hide_same_button = (
        '      <button type="button" class="toolbar-button table-filter-button" onclick="toggleSameRows(this)" aria-pressed="false">Hide same</button>'
        if include_hide_same
        else ""
    )
    row_count_html = f' <span class="table-row-count" data-row-count>({len(rows)})</span>'
    table_attributes = ' class="data-table comparison-table has-grouped-header"'
    if include_hide_same:
        table_attributes += ' data-hide-same="false"'
    return "\n".join(
        (
            '  <section class="section-card">',
            '    <div class="table-section-header">',
            f"      <h2>{escape(title)}{row_count_html}</h2>",
            hide_same_button,
            "    </div>",
            f'    <div class="{table_shell_class}">',
            f"      <table{table_attributes}>",
            "        <thead>",
            f"          <tr>{''.join(solo_header_cells)}{''.join(field_header_cells)}</tr>",
            f"          <tr>{''.join(server_header_cells)}</tr>",
            "        </thead>",
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
        f'<td{_filename_title_attribute(item)}>{escape(item.title)}</td><td>{escape(item.series_name or "")}</td>'
        f'{_table_cell(_display_season(item), sort_value=_season_sort_value(item))}'
        f'{_table_cell("" if item.episode_number is None else item.episode_number, sort_value=_episode_sort_value(item))}</tr>'
    )


def _mismatched_metadata_row_html(entry: tuple[str, ...]) -> str:
    """Return one side-by-side mismatched-metadata comparison row."""
    (
        library_name,
        filename,
        left_item_id,
        right_item_id,
        left_server_key,
        right_server_key,
        left_title,
        right_title,
        left_season,
        right_season,
        left_episode_number,
        right_episode_number,
        left_episode_name,
        right_episode_name,
        left_year,
        right_year,
        left_resolution,
        right_resolution,
        left_video_codec,
        right_video_codec,
        left_audio_codec,
        right_audio_codec,
        path_sort_key,
    ) = entry
    search_text = " ".join(
        part
        for part in (
            library_name,
            filename,
            left_title,
            right_title,
            left_episode_name,
            right_episode_name,
        )
        if part
    ).lower()
    return (
        f'<tr data-diff-row data-search-row data-search="{escape(search_text)}">'
        f"<td>{escape(library_name)}</td>"
        f"{_table_cell(filename, sort_value=path_sort_key)}"
        f"{_diff_cell(left_title, is_different=left_title.casefold() != right_title.casefold())}"
        f"{_diff_cell(right_title, is_different=left_title.casefold() != right_title.casefold())}"
        f"{_diff_cell(left_season, is_different=left_season != right_season)}"
        f"{_diff_cell(right_season, is_different=left_season != right_season)}"
        f"{_diff_cell(left_episode_number, is_different=left_episode_number != right_episode_number)}"
        f"{_diff_cell(right_episode_number, is_different=left_episode_number != right_episode_number)}"
        f"{_diff_cell(left_episode_name, is_different=left_episode_name.casefold() != right_episode_name.casefold())}"
        f"{_transfer_cell(left_server_key, left_item_id, right_server_key, right_item_id)}"
        f"{_diff_cell(right_episode_name, is_different=left_episode_name.casefold() != right_episode_name.casefold())}"
        f"{_diff_cell(left_year, is_different=left_year != right_year)}"
        f"{_diff_cell(right_year, is_different=left_year != right_year)}"
        f"{_diff_cell(left_resolution, is_different=left_resolution != right_resolution)}"
        f"{_diff_cell(right_resolution, is_different=left_resolution != right_resolution)}"
        f"{_diff_cell(left_video_codec, is_different=left_video_codec != right_video_codec)}"
        f"{_diff_cell(right_video_codec, is_different=left_video_codec != right_video_codec)}"
        f"{_diff_cell(left_audio_codec, is_different=left_audio_codec != right_audio_codec)}"
        f"{_diff_cell(right_audio_codec, is_different=left_audio_codec != right_audio_codec)}"
        "</tr>"
    )


def _transfer_cell(
    left_server_key: str,
    left_item_id: str,
    right_server_key: str,
    right_item_id: str,
) -> str:
    """Return the transfer-metadata button cell for one mismatched-metadata row.

    The button copies a ready-made ``transfer_metadata.py`` command to the
    clipboard rather than performing the transfer itself, since the static
    report has no live backend to call into. Running the copied command
    performs the actual read/merge/write against both Jellyfin servers and
    prompts for confirmation before overwriting anything.
    """
    if not left_server_key or not right_server_key:
        return '<td class="transfer-cell"></td>'

    command = (
        "python transfer_metadata.py"
        f' --from-server "{left_server_key}" --from-item "{left_item_id}"'
        f' --to-server "{right_server_key}" --to-item "{right_item_id}"'
    )
    return (
        '<td class="transfer-cell">'
        '<button type="button" class="transfer-button" '
        f'data-command="{escape(command)}" '
        'title="Copy command to transfer metadata from the left server to the right server" '
        'onclick="copyTransferCommand(this)">&#8594;</button>'
        "</td>"
    )


def _paired_missing_seasons_rows(
    left_findings: tuple[tuple[str, AuditFinding], ...],
    right_findings: tuple[tuple[str, AuditFinding], ...],
) -> tuple[str, ...]:
    """Return side-by-side missing-seasons rows for both servers."""
    paired_findings = _pair_sequence_gap_findings(
        left_findings,
        right_findings,
        key_builder=_missing_seasons_key,
    )
    return tuple(
        _paired_missing_seasons_row(library_name, left_finding, right_finding)
        for _, library_name, left_finding, right_finding in paired_findings
    )


def _paired_missing_episodes_rows(
    left_findings: tuple[tuple[str, AuditFinding], ...],
    right_findings: tuple[tuple[str, AuditFinding], ...],
) -> tuple[str, ...]:
    """Return side-by-side missing-episodes rows for both servers."""
    paired_findings = _pair_sequence_gap_findings(
        left_findings,
        right_findings,
        key_builder=_missing_episodes_key,
    )
    return tuple(
        _paired_missing_episodes_row(library_name, left_finding, right_finding)
        for _, library_name, left_finding, right_finding in paired_findings
    )


def _pair_sequence_gap_findings(
    left_findings: tuple[tuple[str, AuditFinding], ...],
    right_findings: tuple[tuple[str, AuditFinding], ...],
    *,
    key_builder,
) -> tuple[tuple[tuple, str, AuditFinding | None, AuditFinding | None], ...]:
    """Return aligned sequence-gap findings for side-by-side comparison rows."""
    left_by_key = {
        key_builder(library_name, finding): (library_name, finding)
        for library_name, finding in left_findings
    }
    right_by_key = {
        key_builder(library_name, finding): (library_name, finding)
        for library_name, finding in right_findings
    }
    paired_rows: list[tuple[tuple, str, AuditFinding | None, AuditFinding | None]] = []
    for key in sorted(set(left_by_key) | set(right_by_key), key=str):
        left_entry = left_by_key.get(key)
        right_entry = right_by_key.get(key)
        library_name = left_entry[0] if left_entry is not None else right_entry[0]
        paired_rows.append(
            (
                key,
                library_name,
                None if left_entry is None else left_entry[1],
                None if right_entry is None else right_entry[1],
            )
        )
    return tuple(paired_rows)


def _paired_missing_seasons_row(
    library_name: str,
    left_finding: AuditFinding | None,
    right_finding: AuditFinding | None,
) -> str:
    """Return one aligned missing-seasons comparison row."""
    item = _sequence_gap_item(left_finding, right_finding)
    series_name = "" if item is None else (item.series_name or item.title)
    left_message = "" if left_finding is None else left_finding.message
    right_message = "" if right_finding is None else right_finding.message
    is_different = left_message != right_message
    search_text = " ".join(
        part
        for part in (library_name, series_name, left_message, right_message)
        if part
    ).lower()
    return (
        f'<tr{" data-diff-row" if is_different else ""} data-search-row data-search="{escape(search_text)}">'
        f"<td>{escape(library_name)}</td>"
        f"<td>{escape(series_name)}</td>"
        f"{_diff_cell(left_message, is_different=is_different)}"
        f"{_diff_cell(right_message, is_different=is_different)}"
        "</tr>"
    )


def _paired_missing_episodes_row(
    library_name: str,
    left_finding: AuditFinding | None,
    right_finding: AuditFinding | None,
) -> str:
    """Return one aligned missing-episodes comparison row."""
    item = _sequence_gap_item(left_finding, right_finding)
    series_name = "" if item is None else (item.series_name or item.title)
    left_message = "" if left_finding is None else left_finding.message
    right_message = "" if right_finding is None else right_finding.message
    is_different = left_message != right_message
    season_text = "" if item is None else _display_season(item)
    search_text = " ".join(
        part
        for part in (library_name, series_name, season_text, left_message, right_message)
        if part
    ).lower()
    return (
        f'<tr{" data-diff-row" if is_different else ""} data-search-row data-search="{escape(search_text)}">'
        f"<td>{escape(library_name)}</td>"
        f"<td>{escape(series_name)}</td>"
        f'{_table_cell(season_text, sort_value="" if item is None else _season_sort_value(item))}'
        f"{_diff_cell(left_message, is_different=is_different)}"
        f"{_diff_cell(right_message, is_different=is_different)}"
        "</tr>"
    )


def _sequence_gap_item(
    left_finding: AuditFinding | None,
    right_finding: AuditFinding | None,
) -> MediaItem | None:
    """Return a representative media item from either finding."""
    if left_finding is not None:
        return left_finding.media_item
    if right_finding is not None:
        return right_finding.media_item
    return None


def _missing_media_sort_key(library_name: str, item: MediaItem) -> tuple:
    """Return a stable sort key for missing-media rows."""
    return (
        library_name.casefold(),
        (item.series_name or "").casefold(),
        (item.season_name or "").casefold(),
        item.episode_number if item.episode_number is not None else -1,
        item.title.casefold(),
    )


def _sequence_gap_sort_key(
    library_name: str,
    finding: AuditFinding,
) -> tuple[str, str, int, int, str, str]:
    """Return a stable sort key for missing season or episode rows."""
    item = finding.media_item
    return (
        library_name.casefold(),
        (item.series_name or "").casefold(),
        item.season_number if item.season_number is not None else -1,
        item.episode_number if item.episode_number is not None else -1,
        item.title.casefold(),
        finding.message.casefold(),
    )


def _missing_seasons_key(library_name: str, finding: AuditFinding) -> tuple[str, str]:
    """Return the alignment key for missing-seasons findings."""
    item = finding.media_item
    return (
        library_name.casefold(),
        _normalized_comparison_text(item.series_name or item.title),
    )


def _missing_episodes_key(
    library_name: str,
    finding: AuditFinding,
) -> tuple[str, str, int | str]:
    """Return the alignment key for missing-episodes findings."""
    item = finding.media_item
    season_value: int | str
    if item.season_number is not None:
        season_value = item.season_number
    else:
        season_value = _display_season(item).casefold()
    return (
        library_name.casefold(),
        _normalized_comparison_text(item.series_name or item.title),
        season_value,
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
    left_primary = _yes_no(has_jellyfin_primary_image(pair.left))
    right_primary = _yes_no(has_jellyfin_primary_image(pair.right))
    return (
        f'<tr data-diff-row data-search-row data-search="{escape(search_text)}"><td>{escape(pair.library)}</td>'
        f'<td{_filename_title_attribute(pair.left)}>{escape(pair.left.display_name)}</td>'
        f'{_diff_cell(left_primary, is_different=left_primary != right_primary)}{_diff_cell(right_primary, is_different=left_primary != right_primary)}'
        "</tr>"
    )


def _subtitle_row(left_result: AuditServerResult, right_result: AuditServerResult, pair: MatchedPair) -> str:
    """Return one subtitle difference row."""
    search_text = f"{pair.library} {pair.left.display_name} subtitles".lower()
    left_subtitles = _yes_no(has_english_subtitles(pair.left))
    right_subtitles = _yes_no(has_english_subtitles(pair.right))
    return (
        f'<tr data-diff-row data-search-row data-search="{escape(search_text)}"><td>{escape(pair.library)}</td>'
        f'<td{_filename_title_attribute(pair.left)}>{escape(pair.left.title)}</td><td>{escape(pair.left.series_name or "")}</td>'
        f'{_table_cell(_display_season(pair.left), sort_value=_season_sort_value(pair.left))}'
        f'{_table_cell("" if pair.left.episode_number is None else pair.left.episode_number, sort_value=_episode_sort_value(pair.left))}'
        f'{_diff_cell(left_subtitles, is_different=left_subtitles != right_subtitles)}{_diff_cell(right_subtitles, is_different=left_subtitles != right_subtitles)}</tr>'
    )


def _filename_title_attribute(item: MediaItem) -> str:
    """Return a title="" attribute showing an item's filename as a hover tooltip."""
    return f' title="{escape(item.path.name)}"'


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
